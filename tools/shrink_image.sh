#!/usr/bin/env bash
set -euo pipefail

# PiShrink commit is pinned so release image builds are reproducible.
readonly PISHRINK_COMMIT="a5f9463c01607ab07402c7e75c9cfd4bb3a0e886"
readonly PISHRINK_URL="https://raw.githubusercontent.com/Drewsif/PiShrink/${PISHRINK_COMMIT}/pishrink.sh"

if [[ $# -ne 1 || ! -f "$1" || "$1" != *.img.xz ]]; then
    echo "Usage: $0 IMAGE.img.xz" >&2
    exit 2
fi

image="$(realpath "$1")"
work_dir="$(mktemp -d)"
cleanup() {
    rm -rf "$work_dir"
}
trap cleanup EXIT

raw_image="$work_dir/petcam.img"
echo "Decompressing image for filesystem and partition shrinking..."
xz --decompress --stdout "$image" > "$raw_image"

echo "Fetching pinned PiShrink revision ${PISHRINK_COMMIT}..."
curl --fail --location --retry 3 --silent --show-error "$PISHRINK_URL" -o "$work_dir/pishrink.sh"
chmod 0755 "$work_dir/pishrink.sh"

# Do not use -s: PiShrink's first-boot root expansion must remain enabled.
# -n disables its non-reproducible online version check; -a -Z uses parallel xz.
sudo bash "$work_dir/pishrink.sh" -n -a -Z "$raw_image"

if [[ ! -s "$raw_image.xz" ]]; then
    echo "PiShrink did not produce a compressed image." >&2
    exit 1
fi

before_bytes="$(stat -c '%s' "$image")"
after_bytes="$(stat -c '%s' "$raw_image.xz")"
if (( after_bytes >= before_bytes )); then
    echo "PiShrink output is not smaller ($before_bytes -> $after_bytes bytes); retaining the original compressed image."
    exit 0
fi

replacement="${image}.shrunk"
cp "$raw_image.xz" "$replacement"
chmod --reference="$image" "$replacement"
mv "$replacement" "$image"
echo "Shrunk image from $before_bytes to $after_bytes bytes."
