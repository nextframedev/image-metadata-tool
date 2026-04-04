#!/usr/bin/env python3
"""
Image Metadata Viewer & Remover
Upload images, inspect EXIF/metadata, and selectively strip fields like GPS location.
"""

from flask import Flask, render_template, request, jsonify, send_file
from werkzeug.utils import secure_filename
from pathlib import Path
import os
import io
import base64
import uuid
import json
import secrets

# Image / EXIF libraries
try:
    from PIL import Image as PILImage
    from PIL.ExifTags import TAGS, GPSTAGS
    PIL_SUPPORT = True
except ImportError:
    PIL_SUPPORT = False

try:
    import piexif
    PIEXIF_SUPPORT = True
except ImportError:
    PIEXIF_SUPPORT = False

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024  # 25 MB

BASE_DIR = Path(__file__).parent
UPLOAD_FOLDER = BASE_DIR / 'uploads'
UPLOAD_FOLDER.mkdir(mode=0o700, exist_ok=True)

ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'tiff', 'tif', 'webp'}
MAX_FILE_SIZE = 25 * 1024 * 1024  # 25 MB

# Human-readable group labels for EXIF IFDs
IFD_LABELS = {
    '0th': 'Image',
    'Exif': 'Exif',
    'GPS': 'GPS / Location',
    '1st': 'Thumbnail',
    'Interop': 'Interoperability',
}

# Tags commonly considered sensitive / privacy-related
SENSITIVE_TAGS = {
    'GPS': True,  # entire GPS IFD
    'Exif': {
        piexif.ExifIFD.DateTimeOriginal if PIEXIF_SUPPORT else 36867,
        piexif.ExifIFD.DateTimeDigitized if PIEXIF_SUPPORT else 36868,
        piexif.ExifIFD.SubSecTimeOriginal if PIEXIF_SUPPORT else 37521,
        piexif.ExifIFD.SubSecTimeDigitized if PIEXIF_SUPPORT else 37522,
    } if PIEXIF_SUPPORT else set(),
    '0th': {
        piexif.ImageIFD.DateTime if PIEXIF_SUPPORT else 306,
        piexif.ImageIFD.Artist if PIEXIF_SUPPORT else 315,
        piexif.ImageIFD.Copyright if PIEXIF_SUPPORT else 33432,
    } if PIEXIF_SUPPORT else set(),
}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def _convert_value(val):
    """Convert EXIF value to JSON-serialisable form."""
    if isinstance(val, bytes):
        try:
            return val.decode('utf-8', errors='replace').strip('\x00')
        except Exception:
            return f'<{len(val)} bytes>'
    if isinstance(val, tuple) and len(val) == 2 and all(isinstance(x, int) for x in val):
        # Rational number
        if val[1] == 0:
            return str(val)
        return round(val[0] / val[1], 6)
    if isinstance(val, (list, tuple)):
        return [_convert_value(v) for v in val]
    return val


def _tag_name(ifd_key, tag_id):
    """Return a human-readable tag name."""
    if ifd_key == 'GPS':
        return piexif.GPSIFD.get(tag_id, {}).get('name', f'Tag-{tag_id}') \
            if hasattr(piexif, 'GPSIFD') else GPSTAGS.get(tag_id, f'Tag-{tag_id}')
    return TAGS.get(tag_id, f'Tag-{tag_id}')


def _gps_tag_name(tag_id):
    """Look up GPS tag name from piexif."""
    mapping = {v: k for k, v in piexif.GPSIFD.__dict__.items()
               if isinstance(v, int)} if PIEXIF_SUPPORT else {}
    return mapping.get(tag_id, GPSTAGS.get(tag_id, f'GPSTag-{tag_id}'))


def _exif_tag_name(ifd_key, tag_id):
    """Look up tag name via piexif IFD dicts."""
    ifd_map = {
        '0th': piexif.ImageIFD,
        'Exif': piexif.ExifIFD,
        'GPS': None,  # handled separately
        '1st': piexif.ImageIFD,
        'Interop': piexif.InteropIFD if hasattr(piexif, 'InteropIFD') else None,
    }
    if ifd_key == 'GPS':
        return _gps_tag_name(tag_id)
    ifd_obj = ifd_map.get(ifd_key)
    if ifd_obj:
        reverse = {v: k for k, v in ifd_obj.__dict__.items() if isinstance(v, int)}
        name = reverse.get(tag_id)
        if name:
            return name
    return TAGS.get(tag_id, f'Tag-{tag_id}')


def _dms_to_decimal(dms, ref):
    """Convert GPS DMS tuple to decimal degrees."""
    try:
        d = dms[0][0] / dms[0][1]
        m = dms[1][0] / dms[1][1]
        s = dms[2][0] / dms[2][1]
        decimal = d + m / 60 + s / 3600
        if ref in (b'S', b'W', 'S', 'W'):
            decimal = -decimal
        return round(decimal, 7)
    except Exception:
        return None


def extract_metadata(image_bytes, filename):
    """Extract EXIF metadata from image bytes using piexif.

    Returns dict with keys: groups (list of tag groups), gps_decimal (lat/lon), has_exif.
    """
    result = {'groups': [], 'gps_decimal': None, 'has_exif': False}

    if not PIEXIF_SUPPORT:
        return result

    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if ext not in ('jpg', 'jpeg', 'tiff', 'tif'):
        # piexif only supports JPEG/TIFF natively
        # Try to re-encode as JPEG to read EXIF
        try:
            img = PILImage.open(io.BytesIO(image_bytes))
            info = img.info
            exif_bytes = info.get('exif', b'')
            if not exif_bytes:
                return result
        except Exception:
            return result
    else:
        try:
            exif_bytes = image_bytes
        except Exception:
            return result

    try:
        if isinstance(exif_bytes, bytes) and ext in ('jpg', 'jpeg', 'tiff', 'tif'):
            exif_dict = piexif.load(exif_bytes)
        else:
            exif_dict = piexif.load(exif_bytes)
    except Exception:
        return result

    for ifd_key in ('0th', 'Exif', 'GPS', '1st', 'Interop'):
        ifd_data = exif_dict.get(ifd_key, {})
        if not ifd_data:
            continue
        result['has_exif'] = True
        tags = []
        for tag_id, raw_val in ifd_data.items():
            name = _exif_tag_name(ifd_key, tag_id)
            value = _convert_value(raw_val)
            is_sensitive = (ifd_key == 'GPS') or \
                           (ifd_key in SENSITIVE_TAGS and
                            isinstance(SENSITIVE_TAGS[ifd_key], set) and
                            tag_id in SENSITIVE_TAGS[ifd_key])
            tags.append({
                'id': tag_id,
                'ifd': ifd_key,
                'name': name,
                'value': value,
                'sensitive': is_sensitive,
            })
        if tags:
            result['groups'].append({
                'ifd': ifd_key,
                'label': IFD_LABELS.get(ifd_key, ifd_key),
                'tags': tags,
            })

    # Decode GPS to decimal
    gps = exif_dict.get('GPS', {})
    if gps:
        lat = gps.get(piexif.GPSIFD.GPSLatitude)
        lat_ref = gps.get(piexif.GPSIFD.GPSLatitudeRef)
        lon = gps.get(piexif.GPSIFD.GPSLongitude)
        lon_ref = gps.get(piexif.GPSIFD.GPSLongitudeRef)
        if lat and lon and lat_ref and lon_ref:
            dec_lat = _dms_to_decimal(lat, lat_ref)
            dec_lon = _dms_to_decimal(lon, lon_ref)
            if dec_lat is not None and dec_lon is not None:
                result['gps_decimal'] = {'lat': dec_lat, 'lon': dec_lon}

    return result


def strip_metadata(image_bytes, filename, remove_groups, remove_tags):
    """Remove selected metadata from image.

    Args:
        image_bytes: original file bytes
        filename: original filename
        remove_groups: list of IFD keys to remove entirely (e.g. ['GPS'])
        remove_tags: list of dicts {ifd, id} for individual tags

    Returns: (cleaned_bytes, removed_count)
    """
    if not PIEXIF_SUPPORT:
        raise RuntimeError('piexif not installed')

    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''

    def _strip_all_exif(image_bytes, ext):
        """Fallback: strip ALL metadata by saving pixel data only."""
        img = PILImage.open(io.BytesIO(image_bytes))
        out = io.BytesIO()
        fmt = 'JPEG' if ext in ('jpg', 'jpeg') else 'TIFF' if ext in ('tiff', 'tif') else (img.format or 'JPEG')
        if img.mode in ('RGBA', 'P') and fmt == 'JPEG':
            img = img.convert('RGB')
        save_kwargs = {'quality': 95} if fmt == 'JPEG' else {}
        # Save without exif= parameter to strip all metadata
        img.save(out, format=fmt, **save_kwargs)
        out.seek(0)
        return out.read()

    # Load EXIF
    try:
        exif_dict = piexif.load(image_bytes)
    except Exception:
        # piexif can't parse (e.g. embedded null bytes)
        return _strip_all_exif(image_bytes, ext), -1

    removed = 0

    # Remove entire groups
    for grp in remove_groups:
        if grp in exif_dict and exif_dict[grp]:
            removed += len(exif_dict[grp])
            exif_dict[grp] = {}

    # Remove individual tags
    for tag_info in remove_tags:
        ifd = tag_info.get('ifd')
        tag_id = int(tag_info.get('id', -1))
        if ifd in exif_dict and tag_id in exif_dict[ifd]:
            del exif_dict[ifd][tag_id]
            removed += 1

    # Re-encode — if dump fails, fall back to stripping all metadata
    try:
        exif_bytes = piexif.dump(exif_dict)
    except Exception:
        try:
            # Retry without thumbnail
            exif_dict.pop('thumbnail', None)
            exif_dict['1st'] = {}
            exif_bytes = piexif.dump(exif_dict)
        except Exception:
            # piexif can't re-encode (null bytes, corrupt data)
            return _strip_all_exif(image_bytes, ext), -1

    # Write back to image
    img = PILImage.open(io.BytesIO(image_bytes))
    out = io.BytesIO()
    if ext in ('jpg', 'jpeg'):
        img.save(out, format='JPEG', exif=exif_bytes, quality=95)
    elif ext in ('tiff', 'tif'):
        img.save(out, format='TIFF', exif=exif_bytes)
    else:
        # For PNG/WebP: save as-is (these formats don't use standard EXIF)
        img.save(out, format=img.format or 'JPEG', exif=exif_bytes, quality=95)

    out.seek(0)
    return out.read(), removed


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html',
                           pil_support=PIL_SUPPORT,
                           piexif_support=PIEXIF_SUPPORT)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.route('/upload', methods=['POST'])
def upload_image():
    """Upload an image and return its metadata.

    Accepts multipart form with 'image' file field.
    Returns JSON with metadata groups, GPS decimal coords, and session info.
    """
    if not PIL_SUPPORT:
        return jsonify({'error': 'Pillow not installed'}), 500
    if not PIEXIF_SUPPORT:
        return jsonify({'error': 'piexif not installed — run: pip install piexif'}), 500

    file = request.files.get('image')
    if not file or not file.filename:
        return jsonify({'error': 'No image file provided'}), 400

    filename = secure_filename(file.filename)
    if not allowed_file(filename):
        return jsonify({'error': f'Unsupported format. Allowed: {", ".join(sorted(ALLOWED_EXTENSIONS))}'}), 400

    # Read file bytes (with size check)
    file.seek(0, 2)
    size = file.tell()
    if size > MAX_FILE_SIZE:
        return jsonify({'error': f'File too large (max {MAX_FILE_SIZE // 1024 // 1024} MB)'}), 400
    file.seek(0)
    image_bytes = file.read()

    # Validate it's actually an image
    try:
        img = PILImage.open(io.BytesIO(image_bytes))
        img.verify()
    except Exception:
        return jsonify({'error': 'File is not a valid image'}), 400

    # Re-open once (verify() invalidates the object) for info + thumbnail
    img = PILImage.open(io.BytesIO(image_bytes))
    basic = {
        'filename': filename,
        'size_bytes': size,
        'width': img.width,
        'height': img.height,
        'format': img.format or 'unknown',
    }

    # Save to session folder
    session_id = str(uuid.uuid4())
    session_dir = UPLOAD_FOLDER / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / filename).write_bytes(image_bytes)

    # Generate thumbnail for preview (reuse the already-open img)
    img.thumbnail((400, 400))
    thumb_buf = io.BytesIO()
    thumb_format = 'JPEG' if filename.rsplit('.', 1)[-1].lower() in ('jpg', 'jpeg') else 'PNG'
    if img.mode in ('RGBA', 'P') and thumb_format == 'JPEG':
        img = img.convert('RGB')
    img.save(thumb_buf, format=thumb_format)
    thumb_buf.seek(0)
    thumb_b64 = base64.b64encode(thumb_buf.read()).decode()
    thumb_mime = 'image/jpeg' if thumb_format == 'JPEG' else 'image/png'

    # Extract metadata
    metadata = extract_metadata(image_bytes, filename)

    return jsonify({
        'session_id': session_id,
        'filename': filename,
        'basic': basic,
        'thumbnail': f'data:{thumb_mime};base64,{thumb_b64}',
        'metadata': metadata,
    })


@app.route('/remove_metadata', methods=['POST'])
def remove_metadata():
    """Remove selected metadata fields and return cleaned image.

    Accepts JSON:
      { session_id, filename, remove_groups: ['GPS'], remove_tags: [{ifd, id}] }

    Returns JSON with download URL and summary.
    """
    if not PIEXIF_SUPPORT:
        return jsonify({'error': 'piexif not installed'}), 500

    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid JSON'}), 400

    session_id = data.get('session_id', '')
    filename = data.get('filename', '')
    remove_groups = data.get('remove_groups', [])
    remove_tags = data.get('remove_tags', [])

    # Validate session_id
    try:
        uuid.UUID(session_id)
    except ValueError:
        return jsonify({'error': 'Invalid session'}), 400

    filename = secure_filename(filename)
    original_path = UPLOAD_FOLDER / session_id / filename
    if not original_path.resolve().is_relative_to(UPLOAD_FOLDER.resolve()):
        return jsonify({'error': 'Invalid path'}), 400
    if not original_path.exists():
        return jsonify({'error': 'File not found'}), 404

    # Validate remove_groups and remove_tags
    valid_groups = {'0th', 'Exif', 'GPS', '1st', 'Interop'}
    remove_groups = [g for g in remove_groups if g in valid_groups]
    remove_tags = [t for t in remove_tags
                   if isinstance(t, dict) and t.get('ifd') in valid_groups]

    # Always strip from the original file — the client sends cumulative removals
    clean_name = f'cleaned_{filename}'
    image_bytes = original_path.read_bytes()

    try:
        cleaned_bytes, removed_count = strip_metadata(
            image_bytes, filename, remove_groups, remove_tags)
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 400

    # -1 means piexif couldn't handle this image — all metadata was stripped
    warning = None
    if removed_count == -1:
        warning = ('This image has EXIF data that cannot be selectively edited. '
                   'All metadata was stripped instead.')
        removed_count = 0

    # Save cleaned version
    (UPLOAD_FOLDER / session_id / clean_name).write_bytes(cleaned_bytes)

    # Re-extract metadata from cleaned image for updated view
    new_metadata = extract_metadata(cleaned_bytes, filename)

    resp = {
        'removed_count': removed_count,
        'download_url': f'/download/{session_id}/{clean_name}',
        'metadata': new_metadata,
    }
    if warning:
        resp['warning'] = warning
    return jsonify(resp)


@app.route('/download/<session_id>/<filename>')
def download_file(session_id, filename):
    """Download a cleaned image."""
    try:
        uuid.UUID(session_id)
    except ValueError:
        return 'Invalid session', 400

    filename = secure_filename(filename)
    file_path = UPLOAD_FOLDER / session_id / filename
    if not file_path.resolve().is_relative_to(UPLOAD_FOLDER.resolve()):
        return 'Invalid path', 400
    if not file_path.exists():
        return 'File not found', 404

    return send_file(file_path, as_attachment=True, download_name=filename)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    host = os.environ.get('HOST', '127.0.0.1')
    port = int(os.environ.get('PORT', '5051'))
    print('=' * 50)
    print('Image Metadata Tool')
    print('=' * 50)
    print(f'Pillow:  {"✓" if PIL_SUPPORT else "✗  (pip install Pillow)"}')
    print(f'piexif:  {"✓" if PIEXIF_SUPPORT else "✗  (pip install piexif)"}')
    print(f'Open: http://{host}:{port}')
    print('=' * 50)
    app.run(debug=debug, host=host, port=port)
