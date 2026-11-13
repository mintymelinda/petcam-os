#!/bin/bash -e
# Build the pinned uStreamer binary once in the reusable PetCam base rootfs.
# The resulting /opt/petcam/ustreamer tree is captured by the base-rootfs cache.
set -euo pipefail

USTREAMER_COMMIT="11fd828ab00c01a39bd0843ef442ef0b6d3cac41"
SOURCE_DIR="/tmp/petcam-ustreamer"

rm -rf "$SOURCE_DIR"
git init "$SOURCE_DIR"
git -C "$SOURCE_DIR" fetch --depth 1 https://github.com/pikvm/ustreamer.git "$USTREAMER_COMMIT"
git -C "$SOURCE_DIR" checkout --detach FETCH_HEAD
make -C "$SOURCE_DIR" -j"$(nproc)" WITH_SYSTEMD=0 WITH_GPIO=0
make -C "$SOURCE_DIR" install PREFIX="/opt/petcam/ustreamer"
rm -rf "$SOURCE_DIR"
