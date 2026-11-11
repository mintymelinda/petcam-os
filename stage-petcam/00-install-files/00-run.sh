#!/bin/bash -e
mkdir -p "${ROOTFS_DIR}/usr/local/lib/petcam"
install -m 0755 files/usr/local/lib/petcam/camera.py "${ROOTFS_DIR}/usr/local/lib/petcam/camera.py"
