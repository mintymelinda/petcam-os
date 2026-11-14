#!/usr/bin/env python3
"""Publish PetCam video to its local RTSP endpoint for OBS to consume."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("PETCAM_RTMP_CONFIG", "/etc/petcam/rtmp.json"))
STATUS_PATH = Path(os.environ.get("PETCAM_RTMP_STATUS", "/run/petcam/rtmp-status.json"))
RTSP_URL = "rtsp://127.0.0.1:8554/petcam"
DEFAULTS = {
    "enabled": False,
    "encoder": "libx264",
    "width": 640,
    "height": 360,
    "fps": 15,
    "bitrate_kbps": 750,
}
ENCODERS = {"libx264", "h264_v4l2m2m"}


class RTMPConfigError(ValueError):
    """Raised when OBS output settings are invalid."""


def validate_config(values):
    if not isinstance(values, dict):
        raise RTMPConfigError("OBS output settings must be an object.")
    config = {**DEFAULTS, **{key: value for key, value in values.items() if key in DEFAULTS}}
    if not isinstance(config["enabled"], bool):
        raise RTMPConfigError("OBS output enabled must be true or false.")
    if config["encoder"] not in ENCODERS:
        raise RTMPConfigError("Encoder must be libx264 or h264_v4l2m2m.")
    for field, minimum, maximum in (
        ("width", 160, 1920), ("height", 90, 1080),
        ("fps", 1, 60), ("bitrate_kbps", 250, 12000),
    ):
        value = config[field]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise RTMPConfigError(f"{field} must be an integer from {minimum} to {maximum}.")
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
    # Migrate existing RTMP installations and remove the now-unneeded external
    # destination and stream key from persistent configuration.
    if any(key not in DEFAULTS for key in values):
        _write_config(config, path)
    return config


def save_config(values, path=CONFIG_PATH):
    config = validate_config(values)
    _write_config(config, path)
    return config


def public_config(config):
    return dict(config)


def build_ffmpeg_command(config, input_url="http://127.0.0.1:8081/stream", native_h264=False):
    config = validate_config(config)
    if not config["enabled"]:
        raise RTMPConfigError("OBS output is disabled.")
    if not shutil.which("ffmpeg"):
        raise RTMPConfigError("FFmpeg is not installed.")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-rw_timeout", "15000000",
    ]
    if native_h264:
        command.extend([
            "-thread_queue_size", "512", "-probesize", "32",
            "-analyzeduration", "0", "-f", "mpegts", "-i",
            "udp://127.0.0.1:10000?fifo_size=1000000&overrun_nonfatal=1",
            "-map", "0:v:0", "-an", "-c:v", "copy",
        ])
    else:
        command.extend([
            "-f", "mpjpeg", "-i", input_url,
            "-an", "-vf",
            f"fps={config['fps']},scale={config['width']}:{config['height']}:force_original_aspect_ratio=decrease,pad={config['width']}:{config['height']}:(ow-iw)/2:(oh-ih)/2",
            "-pix_fmt", "yuv420p", "-c:v", config["encoder"],
            "-b:v", f"{config['bitrate_kbps']}k",
            "-maxrate", f"{config['bitrate_kbps']}k",
            "-bufsize", f"{config['bitrate_kbps'] * 2}k",
            "-g", str(config["fps"] * 2),
        ])
        if config["encoder"] == "libx264":
            command.extend(["-preset", "ultrafast", "-tune", "zerolatency"])
    command.extend([
        "-progress", "pipe:1", "-stats_period", "1", "-nostats",
        "-rtsp_transport", "tcp", "-f", "rtsp", RTSP_URL,
    ])
    return command


def native_h264_available(status_url="http://127.0.0.1:8081/status", timeout=25):
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(status_url, timeout=2) as response:
                status = json.loads(response.read(65536))
            return isinstance(status, dict) and status.get("native_h264") is True
        except urllib.error.HTTPError as error:
            if error.code in (404, 405):
                return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)
        except (OSError, ValueError, urllib.error.URLError):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate OBS output settings and FFmpeg availability")
    args = parser.parse_args()
    config = load_config()
    if not config["enabled"]:
        raise SystemExit("OBS output is disabled.")
    native_h264 = native_h264_available()
    command = build_ffmpeg_command(config, native_h264=native_h264)
    if not native_h264:
        encoders = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if encoders.returncode or not re.search(r"\b" + re.escape(config["encoder"]) + r"\b", encoders.stdout):
            raise SystemExit(f"Configured FFmpeg encoder is unavailable: {config['encoder']}")
    if args.check:
        print(json.dumps({**public_config(config), "source": "native_h264" if native_h264 else "mjpeg_transcode",
                          "rtsp_url": "rtsp://<petcam-host>:8554/petcam"}))
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.info("Starting OBS RTSP feed using %s", "Picamera2 native H.264 stream copy" if native_h264 else config["encoder"])
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    stopping = False
    latest_fps = None

    def forward_stderr():
        for raw_line in process.stderr:
            logging.warning("FFmpeg: %s", raw_line.rstrip())

    stderr_reader = threading.Thread(target=forward_stderr, name="obs-ffmpeg-logs", daemon=True)
    stderr_reader.start()

    def write_status(state):
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "state": state, "active": state == "publishing",
            "fps": latest_fps, "updated_at": time.time(),
            "source": "native_h264" if native_h264 else "mjpeg_transcode",
        }
        temporary = STATUS_PATH.with_name(STATUS_PATH.name + ".tmp")
        temporary.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        os.replace(temporary, STATUS_PATH)

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True
        if process.poll() is None:
            process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    write_status("starting")
    try:
        for raw_line in process.stdout:
            line = raw_line.strip()
            if line.startswith("fps="):
                try:
                    latest_fps = float(line.split("=", 1)[1])
                except ValueError:
                    latest_fps = None
                write_status("publishing")
            elif line.startswith("progress="):
                write_status("publishing" if line.endswith(("continue", "end")) else "starting")
        code = process.wait()
        write_status("stopped" if stopping and code == 0 else "failed" if code else "stopped")
        if code and not stopping:
            raise SystemExit(code)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        stderr_reader.join(timeout=2)
        if stopping:
            write_status("stopped")


if __name__ == "__main__":
    main()
