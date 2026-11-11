#!/bin/bash -e

# Start this custom stage from the completed root filesystem of stage2.
if [ ! -d "${ROOTFS_DIR}" ]; then
    copy_previous
fi
