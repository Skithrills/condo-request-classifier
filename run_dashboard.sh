#!/usr/bin/env sh
set -e
cd "$(dirname "$0")"
if ! command -v uv >/dev/null 2>&1; then
    echo "uv is not installed. See https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
fi
echo "Opening the staff dashboard at http://localhost:8502 ..."
exec uv run --locked --extra telegram streamlit run dashboard.py --server.port 8502
