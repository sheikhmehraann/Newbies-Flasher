#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d "$HOME/storage" ]; then
    termux-setup-storage || true
fi

missing=""
command -v python >/dev/null 2>&1 || missing="$missing python"
command -v zstd >/dev/null 2>&1 || missing="$missing zstd"

if [ -n "$missing" ]; then
    echo "Installing Termux packages:$missing"
    pkg update -y && pkg install -y $missing
fi

chmod +x bin/device/* 2>/dev/null || true
exec python main.py "$@"
