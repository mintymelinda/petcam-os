#!/usr/bin/env python3
"""Serve PetCam camera video over RTSP with GStreamer (no transcoding)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import shlex
import subprocess
import time
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("PETCAM_RTMP_CONFIG", "/etc/petcam/rtmp.json"))
STATUS_PATH = Path(os.environ.get("PETCAM_RTMP_STATUS", "/run/petcam/rtmp-status.json"))
RTSP_URL = "rtsp://127.0.0.1:8554/petcam"
CAMERA_STATUS_PATH = Path("/var/lib/petcam/camera-status.json")
DEFAULTS = {"enabled": False}


class RTMPConfigError(ValueError):
    """Raised when OBS output settings are invalid."""


def validate_config(values):
    if not isinstance(values, dict):
        raise RTMPConfigError("OBS output settings must be an object.")
    config = {**DEFAULTS, **{key: value for key, value in values.items() if key in DEFAULTS}}
    if not isinstance(config["enabled"], bool):
        raise RTMPConfigError("OBS output enabled must be true or false.")
    return config


def _write_config(config, path):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o640)
    try:
        import grp
        import pwd
        os.chown(temporary, pwd.getpwnam("petcam").pw_uid, grp.getgrnam("petcam").gr_gid)
    except (KeyError, PermissionError):
        pass
    os.replace(temporary, target)


def load_config(path=CONFIG_PATH):
    try:
        values = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return validate_config({})
    except (OSError, json.JSONDecodeError) as error:
        raise RTMPConfigError(f"Unable to read OBS output settings: {error}") from error
    config = validate_config(values)
    if isinstance(values, dict) and any(key not in DEFAULTS for key in values):
        _write_config(config, path)
    return config


def save_config(values, path=CONFIG_PATH):
    config = validate_config(values)
    _write_config(config, path)
    return config


def public_config(config):
    return dict(config)


def load_camera_status(path=CAMERA_STATUS_PATH):
    try:
        status = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(status, dict):
        return {}
    pid = status.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return {}
    try:
        os.kill(pid, 0)
    except OSError:
        return {}
    return status


def native_h264_available(status_url=None, timeout=0, marker_path=CAMERA_STATUS_PATH):
    """Detect the native V4L2 H.264 capture selection held by petcam-stream."""
    del status_url, timeout
    status = load_camera_status(marker_path)
    return status.get("native_h264") is True and bool(status.get("device"))


def build_gstreamer_launch(camera_status, mjpeg_url="http://127.0.0.1:8081/stream"):
    """Build a GStreamer RTSP factory pipeline; compressed video is never decoded."""
    if camera_status.get("native_h264") is True:
        device = camera_status.get("device", "")
        resolution = camera_status.get("resolution", "")
        fps = camera_status.get("fps", "")
        if not re.fullmatch(r"/dev/(?:video[0-9]+|v4l/by-id/[A-Za-z0-9._:+_-]+)", device):
            raise RTMPConfigError("Native H.264 camera device path is invalid.")
        if not re.fullmatch(r"[1-9][0-9]{0,4}x[1-9][0-9]{0,4}", str(resolution)):
            raise RTMPConfigError("Native H.264 resolution is invalid.")
        if not re.fullmatch(r"[1-9][0-9]{0,2}", str(fps)):
            raise RTMPConfigError("Native H.264 frame rate is invalid.")
        width, height = resolution.split("x", 1)
        return (
            f"( v4l2src device={shlex.quote(device)} ! "
            f"video/x-h264,width={width},height={height},framerate={fps}/1 ! "
            "h264parse config-interval=-1 ! "
            "rtph264pay name=pay0 pt=96 config-interval=1 )"
        )
    if mjpeg_url != "http://127.0.0.1:8081/stream":
        raise RTMPConfigError("MJPEG source URL must be the local PetCam stream.")
    return (
        '( souphttpsrc location="http://127.0.0.1:8081/stream" is-live=true do-timestamp=true ! '
        "multipartdemux ! jpegparse ! rtpjpegpay name=pay0 pt=26 )"
    )


def _write_status(state, source, path=STATUS_PATH):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "state": state,
        "active": state == "publishing",
        "fps": None,
        "updated_at": time.time(),
        "source": source,
    }
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def _load_gstreamer():
    try:
        import gi
        gi.require_version("Gst", "1.0")
        gi.require_version("GstRtspServer", "1.0")
        from gi.repository import GLib, Gst, GstRtspServer
    except (ImportError, ValueError) as error:
        raise RTMPConfigError(
            "GStreamer RTSP Server bindings are missing. Install python3-gi and gir1.2-gst-rtsp-server-1.0."
        ) from error
    Gst.init(None)
    return GLib, Gst, GstRtspServer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate GStreamer RTSP dependencies and camera selection")
    args = parser.parse_args()
    config = load_config()
    if not config["enabled"]:
        logging.info("OBS RTSP output is disabled; server will remain stopped.")
        _write_status("disabled", None)
        return

    camera_status = load_camera_status()
    native_h264 = camera_status.get("native_h264") is True
    source = "native_h264" if native_h264 else "mjpeg"
    launch = build_gstreamer_launch(camera_status)
    GLib, Gst, GstRtspServer = _load_gstreamer()
    if args.check:
        print(json.dumps({
            **public_config(config),
            "source": source,
            "pipeline": launch,
            "rtsp_url": "rtsp://<petcam-host>:8554/petcam",
        }))
        return

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = GstRtspServer.RTSPServer()
    server.set_address("0.0.0.0")
    server.set_service("8554")
    mounts = server.get_mount_points()
    factory = GstRtspServer.RTSPMediaFactory()
    factory.set_shared(True)
    factory.set_launch(launch)

    def media_configured(_factory, media):
        logging.info("GStreamer RTSP media configured from %s", source)
        _write_status("publishing", source)
        media.connect("unprepared", lambda _media: _write_status("starting", source))

    factory.connect("media-configure", media_configured)
    mounts.add_factory("/petcam", factory)
    if server.attach(None) == 0:
        raise RTMPConfigError("GStreamer RTSP server could not attach to the GLib main context.")
    _write_status("starting", source)
    logging.info("Serving %s directly over RTSP at %s", source, RTSP_URL)
    loop = GLib.MainLoop()

    def stop(_signum, _frame):
        loop.quit()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        loop.run()
    finally:
        _write_status("stopped", source)


if __name__ == "__main__":
    main()
