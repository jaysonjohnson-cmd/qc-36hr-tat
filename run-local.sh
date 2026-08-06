#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d venv ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

source venv/bin/activate

echo "Installing dependencies..."
pip install -q -r requirements.txt

export LOCAL_DEV=1
export FLASK_DEBUG=1
export TOOL_SLUG="${TOOL_SLUG:-$(basename "$(pwd)")}"

echo "Starting app at http://localhost:8080 (TOOL_SLUG=$TOOL_SLUG)"
python3 main.py
