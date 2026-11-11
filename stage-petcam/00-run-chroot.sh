#!/bin/bash -e
# This script runs inside the image root filesystem.
apt-get update
apt-get install -y --no-install-recommends avahi-daemon avahi-utils git build-essential pkg-config libevent-dev libjpeg-dev libbsd-dev libv4l-dev v4l-utils python3-minimal

if ! id petcam >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/petcam --create-home --shell /usr/sbin/nologin petcam
fi
usermod -a -G video petcam

install -d -m 0755 /opt
git clone --depth 1 https://github.com/pikvm/ustreamer.git /opt/ustreamer
make -C /opt/ustreamer -j1 WITH_SYSTEMD=0 WITH_GPIO=0
make -C /opt/ustreamer install PREFIX=/usr/local
ldconfig

# The pi-gen stage's files/ tree is copied into the target root filesystem.
chmod 0755 /usr/local/lib/petcam/camera.py
cat > /usr/local/bin/petcam-camera <<'SCRIPT'
#!/bin/sh
exec python3 /usr/local/lib/petcam/camera.py "$@"
SCRIPT
cat > /usr/local/bin/petcam-status <<'SCRIPT'
#!/bin/sh
exec python3 /usr/local/lib/petcam/camera.py status
SCRIPT
chmod 0755 /usr/local/bin/petcam-camera /usr/local/bin/petcam-status

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
RESOLUTION="${RESOLUTION:-auto}"
FPS="${FPS:-auto}"
DEVICE="${DEVICE:-}"

args=(select --resolution "$RESOLUTION" --fps "$FPS")
if [[ -n "$DEVICE" ]]; then
    args+=(--device "$DEVICE")
fi
selection="$(/usr/local/bin/petcam-camera "${args[@]}")"
json_value() {
    python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1" <<< "$selection"
}
camera="$(json_value device)"
resolution="$(json_value resolution)"
fps="$(json_value fps)"
format="$(json_value format)"
encoder="$(json_value encoder)"
native_mjpeg="$(json_value native_mjpeg)"
tier="$(json_value tier)"

if [[ "$native_mjpeg" != "True" && "$native_mjpeg" != "true" ]]; then
    echo "WARNING: camera has no native MJPEG mode at the selected resolution; CPU JPEG encoding will be used." >&2
fi
echo "PetCam hardware tier: $tier"
echo "Starting camera $camera at $resolution, format=$format, requested_fps=$fps, encoder=$encoder"
if [[ "$fps" == "0" ]]; then
    exec /usr/local/bin/ustreamer --device="$camera" --host=0.0.0.0 --port="$PORT" --format="$format" --encoder="$encoder" --resolution="$resolution" --desired-fps=0 --perf
else
    exec /usr/local/bin/ustreamer --device="$camera" --host=0.0.0.0 --port="$PORT" --format="$format" --encoder="$encoder" --resolution="$resolution" --desired-fps="$fps" --perf
fi
SCRIPT
chmod 0755 /usr/local/lib/petcam/start-stream.sh

cat > /etc/petcam/petcam.conf <<'CONF'
# Automatic mode selection prefers native MJPEG and chooses a conservative
# starting mode for the detected Raspberry Pi. No camera-advertised mode is
# hidden or hard-capped: use explicit values to request another supported mode.
# DEVICE=/dev/video0
RESOLUTION=auto
FPS=auto
PORT=8080
CONF
chmod 0644 /etc/petcam/petcam.conf

cat > /etc/systemd/system/petcam-stream.service <<'UNIT'
[Unit]
Description=PetCam camera-agnostic UVC streamer
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
