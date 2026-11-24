#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required (3.8+). Install it from your package manager and try again."
    exit 1
fi

chmod +x bin/linux/* bin/device/* 2>/dev/null || true

if [ "$#" -eq 0 ] && { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ] || [ "$(uname -s)" = "Darwin" ]; }; then
    if python3 -c "import tkinter" >/dev/null 2>&1; then
        exec python3 gui.py
    fi
fi

exec python3 main.py "$@"
