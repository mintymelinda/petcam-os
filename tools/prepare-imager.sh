#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob
images=( *.img.xz )
if (( ${#images[@]} != 1 )); then
    echo "Expected exactly one .img.xz image in this folder; found ${#images[@]}." >&2
    exit 1
fi
python3 "$(dirname "$0")/create_imager_manifest.py" "${images[0]}"
if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$(dirname "$0")/petcam-os.rpi-imager-manifest"
else
    echo "Created petcam-os.rpi-imager-manifest. Open it with Raspberry Pi Imager 2."
fi
