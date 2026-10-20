#!/usr/bin/env bash
# Run every test (Python with Pillow via uv; the bridge tests need node, else they skip)
# and a syntax check of the JavaScript. Never touches the TV or ~/.config/tv-home.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
if command -v uv >/dev/null; then
    uv run -q --no-project --with pytest --with pillow pytest -q -p no:cacheprovider tests
else
    python3 -m pytest -q -p no:cacheprovider tests
fi
if command -v node >/dev/null; then
    for f in ui/js/*.js tv/app/*.js; do node --check "$f"; done
    echo "check.sh: JavaScript syntax ok"
fi
