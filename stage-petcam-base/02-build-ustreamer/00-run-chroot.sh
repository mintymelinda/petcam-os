#!/bin/bash -e
# Build the pinned uStreamer binary once in the reusable PetCam base rootfs.
# The resulting /opt/petcam/ustreamer tree is captured by the base-rootfs cache.
set -euo pipefail

USTREAMER_COMMIT="11fd828ab00c01a39bd0843ef442ef0b6d3cac41"
SOURCE_DIR="/tmp/petcam-ustreamer"

rm -rf "$SOURCE_DIR"
# Preserve every shared library used by the installed binary before purging build dependencies.
USTREAMER_BIN="/opt/petcam/ustreamer/bin/ustreamer"
while IFS= read -r library; do
    resolved_library="$(readlink -f "$library" 2>/dev/null || true)"
    if [[ -z "$resolved_library" ]]; then
        echo "Unable to resolve runtime library path: $library" >&2
        exit 1
    fi

    package="$(dpkg-query -S "$resolved_library" 2>/dev/null | awk -F ': ' '$1 !~ /^(diversion|local diversion)( |$)/ && NF >= 2 { print $1; exit }')"
    if [[ -z "$package" ]]; then
        echo "Unable to identify the Debian package owning runtime library: $resolved_library" >&2
        exit 1
    fi
    apt-mark manual "$package" >/dev/null
done < <(ldd "$USTREAMER_BIN" | awk '{ for (i = 1; i <= NF; i++) if ($i ~ /^\\//) print $i }' | sort -u)

apt-get purge -y --auto-remove \\
    build-essential pkg-config git \\
    libevent-dev libjpeg-dev libbsd-dev libv4l-dev
# Detach pi-gen's shared archive cache before cleaning the target rootfs.
if [[ -n "${APT_ARCHIVE_CACHE:-}" ]]; then
    umount /var/cache/apt/archives
fi
apt-get clean
rm -rf /var/lib/apt/lists/*
find /usr/share/doc -mindepth 1 -maxdepth 1 ! -name copyright -exec rm -rf {} +
rm -rf /usr/share/man/* /usr/share/info/*
find /var/log -type f -exec truncate -s 0 {} +
rm -rf /tmp/* /var/tmp/*
git init "$SOURCE_DIR"
git -C "$SOURCE_DIR" fetch --depth 1 https://github.com/pikvm/ustreamer.git "$USTREAMER_COMMIT"
git -C "$SOURCE_DIR" checkout --detach FETCH_HEAD
make -C "$SOURCE_DIR" -j"$(nproc)" WITH_SYSTEMD=0 WITH_GPIO=0
make -C "$SOURCE_DIR" install PREFIX="/opt/petcam/ustreamer"
rm -rf "$SOURCE_DIR"
