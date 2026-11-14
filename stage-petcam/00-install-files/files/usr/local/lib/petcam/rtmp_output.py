#!/usr/bin/env python3
"""Optional RTMP publisher that reuses PetCam's existing local MJPEG stream."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

CONFIG_PATH = Path(os.environ.get("PETCAM_RTMP_CONFIG", "/etc/petcam/rtmp.json"))
STATUS_PATH = Path(os.environ.get("PETCAM_RTMP_STATUS", "/run/petcam/rtmp-status.json"))
DEFAULTS = {
    "enabled": False,
    "server_url": "",
    "stream_key": "",
    "encoder": "libx264",
    "width": 1280,
    "height": 720,
    "fps": 30,
    "bitrate_kbps": 2500,
}
ENCODERS = {"libx264", "h264_v4l2m2m"}


class RTMPConfigError(ValueError):
    """Raised when RTMP output settings are invalid."""


def validate_config(values):
    if not isinstance(values, dict):
        raise RTMPConfigError("RTMP settings must be an object.")
    unknown = sorted(set(values) - set(DEFAULTS))
    if unknown:
        raise RTMPConfigError("Unknown RTMP setting(s): " + ", ".join(unknown))
    config = {**DEFAULTS, **values}
    if not isinstance(config["enabled"], bool):
        raise RTMPConfigError("RTMP enabled must be true or false.")
    server = str(config["server_url"]).strip()
    key = str(config["stream_key"]).strip()
    if server:
        parsed = urlsplit(server)
        if parsed.scheme not in {"rtmp", "rtmps"} or not parsed.hostname:
            raise RTMPConfigError("RTMP server URL must use rtmp:// or rtmps:// and include a host.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RTMPConfigError("Put the stream key in its own field; URL credentials, query strings, and fragments are not allowed.")
        if any(char in server for char in ("\r", "\n", " ", "\t")):
            raise RTMPConfigError("RTMP server URL contains invalid whitespace.")
    if key and not re.fullmatch(r"[A-Za-z0-9._~-]{1,512}", key):
        raise RTMPConfigError("Stream key must be 1–512 characters using letters, numbers, dot, underscore, tilde, or hyphen.")
    if config["enabled"] and (not server or not key):
        raise RTMPConfigError("Set both the RTMP server URL and stream key before enabling output.")
    config["server_url"] = server.rstrip("/")
    config["stream_key"] = key
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


def load_config(path=CONFIG_PATH):
    try:
        values = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return validate_config({})
    except (OSError, json.JSONDecodeError) as error:
        raise RTMPConfigError(f"Unable to read RTMP settings: {error}") from error
    return validate_config(values)


def save_config(values, path=CONFIG_PATH):
    config = validate_config(values)
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
    return config


def public_config(config):
    return {
        **{key: value for key, value in config.items() if key != "stream_key"},
        "stream_key_configured": bool(config.get("stream_key")),
        "stream_key": "",
    }


def build_ffmpeg_command(config, input_url="http://127.0.0.1:8081/stream"):
    config = validate_config(config)
    if not config["enabled"]:
        raise RTMPConfigError("RTMP output is disabled.")
    if not shutil.which("ffmpeg"):
        raise RTMPConfigError("FFmpeg is not installed.")
    output_url = config["server_url"] + "/" + config["stream_key"]
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-rw_timeout", "15000000", "-f", "mpjpeg", "-i", input_url,
        "-progress", "pipe:1", "-stats_period", "1", "-nostats",
        "-an", "-vf",
        f"fps={config['fps']},scale={config['width']}:{config['height']}:force_original_aspect_ratio=decrease,pad={config['width']}:{config['height']}:(ow-iw)/2:(oh-ih)/2",
        "-pix_fmt", "yuv420p", "-c:v", config["encoder"],
        "-b:v", f"{config['bitrate_kbps']}k",
        "-maxrate", f"{config['bitrate_kbps']}k",
        "-bufsize", f"{config['bitrate_kbps'] * 2}k",
        "-g", str(config["fps"] * 2),
    ]
    if config["encoder"] == "libx264":
        command.extend(["-preset", "ultrafast", "-tune", "zerolatency"])
    command.extend(["-f", "flv", output_url])
    return command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate settings and FFmpeg availability without publishing")
    args = parser.parse_args()
    config = load_config()
    if not config["enabled"]:
        raise SystemExit("RTMP output is disabled.")
    command = build_ffmpeg_command(config)
    encoders = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        capture_output=True, text=True, timeout=15, check=False,
    )
    if encoders.returncode or not re.search(r"\b" + re.escape(config["encoder"]) + r"\b", encoders.stdout):
        raise SystemExit(f"Configured FFmpeg encoder is unavailable: {config['encoder']}")
    if args.check:
        print(json.dumps(public_config(config)))
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.info("Starting RTMP publisher using %s; destination credentials are redacted", config["encoder"])
    process = subprocess.Popen(command, stdout=subprocess.PIPE, text=True, bufsize=1)
    stopping = False
    latest_fps = None

    def write_status(state):
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "state": state, "active": state == "publishing",
            "fps": latest_fps, "updated_at": time.time(),
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
        if stopping:
            write_status("stopped")


if __name__ == "__main__":
    main()
