#!/bin/bash
# Start the Image Metadata Tool app

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Create venv if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

source venv/bin/activate

# Install/upgrade dependencies
pip install -q -r requirements.txt

PORT=${PORT:-5051}

echo ""
echo "================================"
echo "  Image Metadata Tool"
echo "================================"
echo "  Open: http://localhost:$PORT"
echo "  Press Ctrl+C to stop"
echo "================================"
echo ""

python3 app.py
