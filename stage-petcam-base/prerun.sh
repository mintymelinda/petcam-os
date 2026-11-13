#!/bin/bash -e

# Build on the completed upstream Raspberry Pi OS stage.
if [ ! -d "${ROOTFS_DIR}" ]; then
    copy_previous
fi
