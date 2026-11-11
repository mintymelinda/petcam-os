#!/bin/bash -e
# This script runs inside the image root filesystem.
apt-get update
apt-get install -y --no-install-recommends avahi-daemon avahi-utils git build-essential pkg-config libevent-dev libjpeg-dev libbsd-dev libv4l-dev v4l-utils

if ! id petcam >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/petcam --create-home --shell /usr/sbin/nologin petcam
fi
usermod -a -G video petcam

install -d -m 0755 /opt
git clone --depth 1 https://github.com/pikvm/ustreamer.git /opt/ustreamer
make -C /opt/ustreamer -j1 WITH_SYSTEMD=0 WITH_GPIO=0
make -C /opt/ustreamer install PREFIX=/usr/local
ldconfig

install -d -m 0755 /usr/local/lib/petcam /etc/petcam
cat > /usr/local/lib/petcam/start-stream.sh <<'SCRIPT'
#!/bin/bash
set -euo pipefail
CONFIG=/etc/petcam/petcam.conf
if [[ -r "$CONFIG" ]]; then
    # shellcheck disable=SC1090
    source "$CONFIG"
fi
PORT="${PORT:-8080}"
RESOLUTION="${RESOLUTION:-1280x720}"
FPS="${FPS:-30}"
DEVICE="${DEVICE:-}"

find_camera() {
    local candidate
    if [[ -n "$DEVICE" ]]; then
        [[ -e "$DEVICE" ]] || { echo "Configured camera does not exist: $DEVICE" >&2; return 1; }
        printf '%s\n' "$DEVICE"
        return 0
    fi
    for candidate in /dev/v4l/by-id/* /dev/video*; do
        [[ -e "$candidate" ]] || continue
        if v4l2-ctl --device="$candidate" --list-formats-ext 2>/dev/null | grep -Eqi 'MJPG|MJPEG'; then
            readlink -f "$candidate"
            return 0
        fi
    done
    echo "No V4L2 camera advertising native MJPEG was found." >&2
    echo "Connect the webcam and inspect modes with: v4l2-ctl --list-formats-ext" >&2
    return 1
}
camera="$(find_camera)"
echo "Starting PetCam on $camera at $RESOLUTION / $FPS FPS"
exec /usr/local/bin/ustreamer --device="$camera" --host=0.0.0.0 --port="$PORT" --format=MJPEG --resolution="$RESOLUTION" --desired-fps="$FPS"
SCRIPT
chmod 0755 /usr/local/lib/petcam/start-stream.sh

cat > /etc/petcam/petcam.conf <<'CONF'
# Override these defaults after boot if required.
# DEVICE=/dev/video0
RESOLUTION=1280x720
FPS=30
PORT=8080
CONF
chmod 0644 /etc/petcam/petcam.conf

cat > /etc/systemd/system/petcam-stream.service <<'UNIT'
[Unit]
Description=PetCam native MJPEG camera streamer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=petcam
Group=video
SupplementaryGroups=video
ExecStart=/usr/local/lib/petcam/start-stream.sh
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/petcam

[Install]
WantedBy=multi-user.target
UNIT

systemctl enable avahi-daemon.service
systemctl enable petcam-stream.service
