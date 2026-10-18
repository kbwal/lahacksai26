#!/bin/bash
set -euo pipefail

if [[ "$(uname -s)" != Darwin || "$(uname -m)" != arm64 ]]; then
    echo "Build this sidecar on an Apple Silicon Mac." >&2
    exit 1
fi

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
# Keep packaging tools isolated from the running development worker's virtualenv.
export UV_PROJECT_ENVIRONMENT="$root/build/macos-venv"
export PYINSTALLER_CONFIG_DIR="$root/build/pyinstaller-cache"
uv sync --locked --no-dev --group packaging --python 3.12
"$UV_PROJECT_ENVIRONMENT/bin/python" scripts/build_macos.py
