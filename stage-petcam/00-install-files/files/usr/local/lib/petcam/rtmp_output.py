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
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("PETCAM_RTMP_CONFIG", "/etc/petcam/rtmp.json"))
STATUS_PATH = Path(os.environ.get("PETCAM_RTMP_STATUS", "/run/petcam/rtmp-status.json"))
RTSP_URL = "rtsp://127.0.0.1:8554/petcam"
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
    # Migrate existing RTMP installations and remove the now-unneeded external
    # destination and stream key from persistent configuration.
    if isinstance(values, dict) and any(key not in DEFAULTS for key in values):
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
            "-map", "0:v:0", "-an", "-c:v", "copy",
        ])
    command.extend([
        "-progress", "pipe:1", "-stats_period", "1", "-nostats",
        "-rtsp_transport", "tcp", "-f", "rtsp", RTSP_URL,
    ])
    return command


def native_h264_available(status_url="http://127.0.0.1:8081/status", timeout=25):
    """Detect the live native H.264 capture process using its PID marker."""
    marker = Path("/var/lib/petcam/camera-status.json")
    try:
        status = json.loads(marker.read_text(encoding="utf-8"))
        pid = int(status.get("pid", 0))
        if status.get("native_h264") is True and pid > 1:
            os.kill(pid, 0)
            command_line = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
            if "ffmpeg" in command_line and "h264" in command_line.lower():
                active = subprocess.run(
                    ["systemctl", "is-active", "--quiet", "petcam-stream.service"],
                    check=False, timeout=2,
                ).returncode == 0
                if active:
                    return True
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        pass
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


def start_rtsp_server():
    binary = Path(__file__).resolve().parents[1] / "bin" / "mediamtx"
    if not binary.is_file():
        raise SystemExit("Bundled MediaMTX RTSP server is missing from this PetCam release.")
    runtime_config = STATUS_PATH.parent / "mediamtx.yml"
    runtime_config.parent.mkdir(parents=True, exist_ok=True)
    runtime_config.write_text("""logLevel: info
logDestinations: [stdout]
rtsp: yes
rtspAddress: :8554
rtmp: no
hls: no
webrtc: no
srt: no
api: no
metrics: no
pprof: no
playback: no
authMethod: internal
authInternalUsers:
  - user: any
    pass:
    ips: ["127.0.0.1", "::1"]
    permissions:
      - action: publish
        path: petcam
      - action: read
        path: petcam
  - user: any
    pass:
    ips: []
    permissions:
      - action: read
        path: petcam
paths:
  petcam:
    source: publisher
""", encoding="utf-8")
    if not os.access(binary, os.X_OK):
        raise SystemExit("Bundled MediaMTX RTSP server is not executable.")
    process = subprocess.Popen(
        [str(binary), str(runtime_config)],
        start_new_session=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(f"MediaMTX exited during startup with status {process.returncode}.")
        try:
            with socket.create_connection(("127.0.0.1", 8554), timeout=0.5):
                return process
        except OSError:
            time.sleep(0.2)
    process.terminate()
    raise SystemExit("MediaMTX did not open the local RTSP listener within 10 seconds.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate OBS output settings and FFmpeg availability")
    args = parser.parse_args()
    config = load_config()
    if not config["enabled"]:
        raise SystemExit("OBS output is disabled.")
    native_h264 = native_h264_available()
    command = build_ffmpeg_command(config, native_h264=native_h264)
    if args.check:
        binary = Path(__file__).resolve().parents[1] / "bin" / "mediamtx"
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise SystemExit("Bundled MediaMTX RTSP server is missing or not executable.")
        print(json.dumps({**public_config(config), "source": "native_h264" if native_h264 else "mjpeg_copy",
                          "rtsp_url": "rtsp://<petcam-host>:8554/petcam"}))
        return
    rtsp_server = start_rtsp_server()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.info("Starting OBS RTSP feed using %s", "native H.264 stream copy" if native_h264 else "MJPEG packet copy")
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
            "source": "native_h264" if native_h264 else "mjpeg_copy",
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
        if rtsp_server.poll() is None:
            rtsp_server.terminate()
            try:
                rtsp_server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                rtsp_server.kill()
                rtsp_server.wait()


if __name__ == "__main__":
    main()
