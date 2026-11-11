#!/bin/bash -e
install -d -m 0755 "${ROOTFS_DIR}/usr/local/lib/petcam"
install -m 0755 files/usr/local/lib/petcam/camera.py "${ROOTFS_DIR}/usr/local/lib/petcam/camera.py"
install -m 0755 files/usr/local/lib/petcam/csi_stream.py "${ROOTFS_DIR}/usr/local/lib/petcam/csi_stream.py"
