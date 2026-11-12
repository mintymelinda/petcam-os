#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob
images=( *.img.xz )
if (( ${#images[@]} != 1 )); then
    echo "Expected exactly one .img.xz image in this folder; found ${#images[@]}." >&2
    exit 1
fi
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
image="$(realpath "${images[0]}")"
manifest="$PWD/petcam-os.rpi-imager-manifest"
python3 "$script_dir/create_imager_manifest.py" "$image" --output "$manifest"

if command -v rpi-imager >/dev/null 2>&1; then
    exec rpi-imager --repo "$manifest"
elif command -v raspberry-pi-imager >/dev/null 2>&1; then
    exec raspberry-pi-imager --repo "$manifest"
else
    echo "PetCam manifest created: $manifest"
    echo "Install Raspberry Pi Imager 2, then open it and select:"
    echo "App Options > Content Repository > Edit > Use custom file."
fi
