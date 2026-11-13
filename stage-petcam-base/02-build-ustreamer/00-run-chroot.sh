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

# Add an explicit V4L2 hardware capture-rate option while preserving --desired-fps
# as the independent output pacing control.
cat > "$SOURCE_DIR/petcam-hw-fps.patch" <<'PATCH'
diff --git a/src/libs/capture.h b/src/libs/capture.h
index 0000000..0000000 100644
--- a/src/libs/capture.h
+++ b/src/libs/capture.h
@@ -60,5 +60,6 @@ typedef struct {
 	uint				width;
 	uint				height;
 	uint				format;
+	uint				hw_fps;
 	char				*media_path;
 	char				*media_entity_name;

diff --git a/src/libs/capture.c b/src/libs/capture.c
index 0000000..0000000 100644
--- a/src/libs/capture.c
+++ b/src/libs/capture.c
@@ -1111,8 +1111,9 @@ static void _capture_open_hw_fps(us_capture_s *cap) {
 	US_MEMSET_ZERO(setfps);
 	setfps.type = run->capture_type;
 	TPF(numerator) = 1;
-	TPF(denominator) = -1; // Request maximum possible FPS
+	TPF(denominator) = cap->hw_fps > 0 ? cap->hw_fps : (uint)-1;
+	// A zero hw_fps preserves the upstream maximum-FPS behavior.
 
 	if (us_xioctl(run->fd, VIDIOC_S_PARM, &setfps) < 0) {
 		_LOG_PERROR("Can't set HW FPS");
 		return;

diff --git a/src/ustreamer/options.c b/src/ustreamer/options.c
index 0000000..0000000 100644
--- a/src/ustreamer/options.c
+++ b/src/ustreamer/options.c
@@ -92,7 +92,8 @@ enum _US_OPT_VALUES {
 	_O_OPEN_ON_DEMAND = 10000,
 	_O_DEVICE_TIMEOUT,
 	_O_DEVICE_ERROR_DELAY,
+	_O_HW_FPS,
 	_O_FORMAT_SWAP_RGB,
 	_O_M2M_DEVICE,
 	_O_MEDIA_DEVICE,
 	_O_MEDIA_ENTITY_NAME,
@@ -180,3 +181,4 @@ static const struct option _LONG_OPTS[] = {
 	{"io-method",				required_argument,	NULL,	_O_IO_METHOD},
 	{"desired-fps",				required_argument,	NULL,	_O_DESIRED_FPS},
+	{"hw-fps",				required_argument,	NULL,	_O_HW_FPS},
 	{"min-frame-size",			required_argument,	NULL,	_O_MIN_FRAME_SIZE},
@@ -445,3 +447,4 @@ 			case _O_IO_METHOD:			OPT_PARSE_ENUM("IO method", cap->io_method, us_capture_parse_io_method, US_IO_METHODS_STR);
 			case _O_IO_METHOD:			OPT_PARSE_ENUM("IO method", cap->io_method, us_capture_parse_io_method, US_IO_METHODS_STR);
 			case _O_DESIRED_FPS:		OPT_NUMBER("--desired-fps", stream->desired_fps, 0, US_VIDEO_MAX_FPS, 0);
+			case _O_HW_FPS:			OPT_NUMBER("--hw-fps", cap->hw_fps, 0, US_VIDEO_MAX_FPS, 0);
 			case _O_MIN_FRAME_SIZE:		OPT_NUMBER("--min-frame-size", cap->min_frame_size, 1, 8192, 0);
PATCH
git -C "$SOURCE_DIR" apply --check "$SOURCE_DIR/petcam-hw-fps.patch"
git -C "$SOURCE_DIR" apply "$SOURCE_DIR/petcam-hw-fps.patch"
make -C "$SOURCE_DIR" -j"$(nproc)" WITH_SYSTEMD=0 WITH_GPIO=0
make -C "$SOURCE_DIR" install PREFIX="/opt/petcam/ustreamer"
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
done < <(ldd "$USTREAMER_BIN" | awk '{ for (i = 1; i <= NF; i++) if (index($i, "/") == 1) print $i }' | sort -u)

apt-get purge -y --auto-remove build-essential pkg-config git libevent-dev libjpeg-dev libbsd-dev libv4l-dev

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
