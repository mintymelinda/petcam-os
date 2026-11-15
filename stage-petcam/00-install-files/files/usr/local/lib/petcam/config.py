#!/usr/bin/env python3
"""Shared PetCam configuration parser, validator, and transactional apply logic."""

from __future__ import annotations

import fcntl
import http.client
from contextlib import contextmanager
import json
import os
import re
import socket
import subprocess
import sys
import time
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(os.environ.get("PETCAM_CONFIG_PATH", "/etc/petcam/petcam.conf"))
CAMERA_STATUS_PATH = Path("/var/lib/petcam/camera-status.json")
LAST_GOOD_PATH = Path(os.environ.get("PETCAM_LAST_GOOD_PATH", str(CONFIG_PATH.with_name(CONFIG_PATH.name + ".last-good"))))
DEFAULTS = {
    "SOURCE": "auto",
    "DEVICE": "",
    "USB_PHYSICAL_PATH": "",
    "CSI_DEVICE_PATH": "",
    "CAMERA": "auto",
    "FORMAT": "auto",
    "RESOLUTION": "auto",
    "FPS": "auto",
    "CSI_QUALITY": "70",
    "PORT": "8081",
}
FORMATS = {
    "MJPG", "MJPEG", "JPEG", "YUYV", "YVYU", "UYVY",
    "YUV420", "YVU420", "RGB565", "RGB24", "GREY", "H264",
}
DEVICE_PATTERN = re.compile(r"^/dev/(?:video[0-9]+|v4l/by-id/[A-Za-z0-9._:+-]+)$")
USB_PHYSICAL_PATH_PATTERN = re.compile(r"^/sys/devices/[A-Za-z0-9._:@/+\-]+$")
CSI_DEVICE_PATH_PATTERN = re.compile(r"^/base/[A-Za-z0-9._:@/+\-]+$")
RESOLUTION_PATTERN = re.compile(r"^([0-9]+)x([0-9]+)$")
NUMBER_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")


class ConfigError(ValueError):
    """Raised when a PetCam configuration is malformed or unsupported."""


class ConfigApplyError(ConfigError):
    """Raised when a staged configuration fails and rollback is attempted."""


def validate_config(values: dict[str, Any], check_hardware: bool = False) -> dict[str, str]:
    if not isinstance(values, dict):
        raise ConfigError("Configuration must be a key/value object.")
    unknown = sorted(set(values) - set(DEFAULTS))
    if unknown:
        raise ConfigError(f"Unknown configuration key(s): {', '.join(unknown)}")
    config = {key: str(values.get(key, default)).strip() for key, default in DEFAULTS.items()}

    if config["SOURCE"] not in {"auto", "usb", "csi"}:
        raise ConfigError("SOURCE must be auto, usb, or csi.")
    if config["DEVICE"] and not DEVICE_PATTERN.fullmatch(config["DEVICE"]):
        raise ConfigError("DEVICE must be empty, /dev/videoN, or a /dev/v4l/by-id path.")
    if config["USB_PHYSICAL_PATH"] and not USB_PHYSICAL_PATH_PATTERN.fullmatch(config["USB_PHYSICAL_PATH"]):
        raise ConfigError("USB_PHYSICAL_PATH must be empty or a /sys/devices physical USB path.")
    if config["CSI_DEVICE_PATH"] and not CSI_DEVICE_PATH_PATTERN.fullmatch(config["CSI_DEVICE_PATH"]):
        raise ConfigError("CSI_DEVICE_PATH must be empty or a /base device-tree path.")
    if config["CAMERA"] != "auto" and not config["CAMERA"].isdigit():
        raise ConfigError("CAMERA must be auto or a non-negative CSI camera index.")
    if config["SOURCE"] == "usb" and config["CAMERA"] != "auto":
        raise ConfigError("CAMERA must be auto when SOURCE=usb.")
    if config["USB_PHYSICAL_PATH"] and config["DEVICE"] and not config["DEVICE"].startswith("/dev/v4l/by-id/"):
        raise ConfigError("DEVICE must be empty or a persistent by-id path when USB_PHYSICAL_PATH is set.")
    if config["CSI_DEVICE_PATH"] and config["CAMERA"] != "auto":
        raise ConfigError("CAMERA must be auto when CSI_DEVICE_PATH is set.")
    if config["SOURCE"] == "csi" and (config["DEVICE"] or config["USB_PHYSICAL_PATH"]):
        raise ConfigError("USB device settings must be empty when SOURCE=csi.")
    if config["SOURCE"] == "usb" and config["CSI_DEVICE_PATH"]:
        raise ConfigError("CSI_DEVICE_PATH must be empty when SOURCE=usb.")
    if config["SOURCE"] == "auto" and (config["USB_PHYSICAL_PATH"] or config["CSI_DEVICE_PATH"]):
        raise ConfigError("A physical camera path requires SOURCE=usb or SOURCE=csi.")

    config["FORMAT"] = "auto" if config["FORMAT"].lower() == "auto" else config["FORMAT"].upper()
    if config["FORMAT"] != "auto" and config["FORMAT"] not in FORMATS:
        raise ConfigError(f"FORMAT must be auto or one of: {', '.join(sorted(FORMATS))}.")
    if config["FORMAT"] == "H264" and config["SOURCE"] != "usb":
        raise ConfigError("H264 capture requires SOURCE=usb.")

    resolution = config["RESOLUTION"]
    if resolution != "auto":
        match = RESOLUTION_PATTERN.fullmatch(resolution)
        if not match:
            raise ConfigError("RESOLUTION must be auto or WIDTHxHEIGHT.")
        width, height = map(int, match.groups())
        if not 1 <= width <= 10000 or not 1 <= height <= 10000:
            raise ConfigError("RESOLUTION dimensions must be between 1 and 10000.")

    fps = config["FPS"]
    if fps != "auto":
        if not NUMBER_PATTERN.fullmatch(fps):
            raise ConfigError("FPS must be auto or a positive number.")
        fps_value = float(fps)
        if not 0 < fps_value <= 240:
            raise ConfigError("FPS must be greater than 0 and no more than 240.")
        if config["SOURCE"] != "csi":
            if not fps_value.is_integer():
                raise ConfigError("USB and automatic-source FPS must be a whole number.")
            config["FPS"] = str(int(fps_value))

    if not config["CSI_QUALITY"].isdigit() or not 1 <= int(config["CSI_QUALITY"]) <= 100:
        raise ConfigError("CSI_QUALITY must be an integer from 1 to 100.")
    if not config["PORT"].isdigit() or not 1024 <= int(config["PORT"]) <= 65535:
        raise ConfigError("PORT must be an integer from 1024 to 65535.")
    if int(config["PORT"]) in {8080, 8443}:
        raise ConfigError("PORT cannot use 8080 or 8443; those ports are reserved for the public stream and HTTPS manager.")

    if check_hardware:
        if config["DEVICE"] and not Path(config["DEVICE"]).exists():
            raise ConfigError(f"Configured camera device is unavailable: {config['DEVICE']}")
        if config["USB_PHYSICAL_PATH"]:
            try:
                result = subprocess.run(
                    ["/usr/local/bin/petcam-camera", "discover"],
                    capture_output=True, text=True, timeout=20, check=False,
                )
                if result.returncode:
                    raise ConfigError(result.stderr.strip() or "Unable to discover USB cameras.")
                devices = json.loads(result.stdout).get("usb_devices", [])
                if not any(device.get("physical_path") == config["USB_PHYSICAL_PATH"] for device in devices):
                    raise ConfigError("Configured USB physical camera path is not available.")
            except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, AttributeError) as error:
                if isinstance(error, ConfigError):
                    raise
                raise ConfigError(f"Unable to verify configured USB camera: {error}") from error
        if config["SOURCE"] in {"auto", "csi"} and (config["CSI_DEVICE_PATH"] or config["CAMERA"] != "auto"):
            try:
                result = subprocess.run(
                    ["/usr/local/bin/petcam-camera", "discover-csi"],
                    capture_output=True, text=True, timeout=20, check=False,
                )
                if result.returncode:
                    raise ConfigError(result.stderr.strip() or "Unable to discover CSI cameras.")
                cameras = json.loads(result.stdout).get("csi_cameras", [])
                if config["CSI_DEVICE_PATH"]:
                    available = any(camera.get("device_path") == config["CSI_DEVICE_PATH"] for camera in cameras)
                    if not available:
                        raise ConfigError("Configured CSI physical device path is not available.")
                elif not any(str(camera.get("index")) == config["CAMERA"] for camera in cameras):
                    raise ConfigError(f"Configured CSI camera index {config['CAMERA']} is not available.")
            except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, AttributeError) as error:
                if isinstance(error, ConfigError):
                    raise
                raise ConfigError(f"Unable to verify configured CSI camera: {error}") from error
    return config


def parse_config(path: Path | str = CONFIG_PATH, check_hardware: bool = False) -> dict[str, str]:
    path = Path(path)
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as error:
        if path != CONFIG_PATH:
            raise ConfigError(f"Configuration file does not exist: {path}") from error
        lines = []
    except OSError as error:
        raise ConfigError(f"Unable to read {path}: {error}") from error

    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"{path}:{number}: expected KEY=VALUE.")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not re.fullmatch(r"[A-Z_]+", key):
            raise ConfigError(f"{path}:{number}: invalid configuration key.")
        if key in values:
            raise ConfigError(f"{path}:{number}: duplicate configuration key {key}.")
        if key not in DEFAULTS:
            raise ConfigError(f"{path}:{number}: unknown configuration key {key}.")
        if any(char in value for char in ("$", chr(96), ";", "|", "&", "<", ">", "(", ")", "\\", "'", '"')):
            raise ConfigError(f"{path}:{number}: shell syntax is not allowed in configuration values.")
        values[key] = value
    return validate_config(values, check_hardware=check_hardware)


def serialize_config(values: dict[str, Any]) -> bytes:
    config = validate_config(values)
    return (
        "# PetCam camera settings; managed by PetCam.\n"
        + "".join(f"{key}={config[key]}\n" for key in DEFAULTS)
    ).encode("utf-8")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(content)
    os.chmod(temporary, 0o640)
    try:
        import grp
        os.chown(temporary, -1, grp.getgrnam("petcam").gr_gid)
    except (ImportError, KeyError, PermissionError):
        pass
    os.replace(temporary, path)


def wait_for_stream(port: int, timeout: int = 30) -> bool:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/stream"
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            with urllib.request.urlopen(url, timeout=min(15, remaining)) as response:
                if response.status == 200 and response.read(1):
                    return True
        except (OSError, urllib.error.URLError, TimeoutError, ValueError, http.client.HTTPException):
            pass
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    return False


def _read_rtsp_response(connection, buffer: bytearray):
    while b"\r\n\r\n" not in buffer:
        chunk = connection.recv(4096)
        if not chunk:
            raise OSError("RTSP server closed the connection before its response headers.")
        buffer.extend(chunk)
    header_end = buffer.index(b"\r\n\r\n") + 4
    header_block = bytes(buffer[:header_end])
    lines = header_block.decode("iso-8859-1").split("\r\n")
    status = lines[0]
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    body_length = int(headers.get("content-length", "0"))
    while len(buffer) < header_end + body_length:
        chunk = connection.recv(4096)
        if not chunk:
            raise OSError("RTSP server closed the connection before its response body.")
        buffer.extend(chunk)
    body = bytes(buffer[header_end:header_end + body_length])
    del buffer[:header_end + body_length]
    return status, headers, body


def _read_h264_rtp_packet(connection, buffer: bytearray, deadline: float) -> bool:
    while time.monotonic() < deadline:
        while len(buffer) < 4:
            connection.settimeout(max(0.1, deadline - time.monotonic()))
            chunk = connection.recv(4096)
            if not chunk:
                return False
            buffer.extend(chunk)
        if buffer[0] != 0x24:
            del buffer[0]
            continue
        channel = buffer[1]
        packet_length = int.from_bytes(buffer[2:4], "big")
        while len(buffer) < packet_length + 4:
            connection.settimeout(max(0.1, deadline - time.monotonic()))
            chunk = connection.recv(4096)
            if not chunk:
                return False
            buffer.extend(chunk)
        packet = bytes(buffer[4:4 + packet_length])
        del buffer[:4 + packet_length]
        if channel != 0 or len(packet) < 13 or packet[1] & 0x7F != 96:
            continue
        header_length = 12 + (packet[0] & 0x0F) * 4
        if packet[0] & 0x10:
            if len(packet) < header_length + 4:
                continue
            extension_length = int.from_bytes(packet[header_length + 2:header_length + 4], "big")
            header_length += 4 + extension_length * 4
        if len(packet) <= header_length:
            continue
        nal_type = packet[header_length] & 0x1F
        if nal_type in range(1, 24) or nal_type in (24, 28, 29):
            return True
    return False


def wait_for_native_h264(timeout: int = 30) -> bool:
    """Require a real H.264 RTP packet from the local GStreamer RTSP endpoint."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        buffer = bytearray()
        try:
            with socket.create_connection(("127.0.0.1", 8554), timeout=1) as connection:
                connection.settimeout(1)
                connection.sendall(
                    "DESCRIBE rtsp://127.0.0.1:8554/petcam RTSP/1.0\r\n"
                    "CSeq: 1\r\n"
                    "Accept: application/sdp\r\n\r\n".encode("ascii")
                )
                status, headers, sdp = _read_rtsp_response(connection, buffer)
                if " 200 " not in status or b"m=video " not in sdp or b"H264/90000" not in sdp.upper():
                    continue
                control = next(
                    (line.split(":", 1)[1].strip() for line in sdp.decode("utf-8", errors="replace").splitlines()
                     if line.startswith("a=control:") and line != "a=control:*"),
                    None,
                )
                if not control:
                    continue
                base_uri = headers.get("content-base", "rtsp://127.0.0.1:8554/petcam/")
                track_uri = control if control.startswith("rtsp://") else base_uri.rstrip("/") + "/" + control.lstrip("/")
                connection.sendall(
                    f"SETUP {track_uri} RTSP/1.0\r\n"
                    "CSeq: 2\r\n"
                    "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n\r\n".encode("ascii")
                )
                setup_status, setup_headers, _body = _read_rtsp_response(connection, buffer)
                session = setup_headers.get("session", "").split(";", 1)[0]
                if " 200 " not in setup_status or not session:
                    continue
                connection.sendall(
                    f"PLAY rtsp://127.0.0.1:8554/petcam RTSP/1.0\r\n"
                    "CSeq: 3\r\n"
                    f"Session: {session}\r\n"
                    "Range: npt=0.000-\r\n\r\n".encode("ascii")
                )
                play_status, _play_headers, _play_body = _read_rtsp_response(connection, buffer)
                if " 200 " not in play_status:
                    continue
                if _read_h264_rtp_packet(connection, buffer, min(deadline, time.monotonic() + 3)):
                    return True
        except (OSError, ValueError, TimeoutError):
            pass
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))
    return False


def wait_for_configured_capture(config: dict[str, str], ready=wait_for_stream) -> bool:
    native_h264 = config.get("FORMAT") == "H264"
    if not native_h264 and config.get("FORMAT") == "auto":
        try:
            marker = json.loads(CAMERA_STATUS_PATH.read_text(encoding="utf-8"))
            native_h264 = isinstance(marker, dict) and marker.get("native_h264") is True
        except (OSError, ValueError):
            native_h264 = False
    if native_h264:
        return wait_for_native_h264()
    return ready(int(config["PORT"]))


def restart_capture() -> None:
    command = ["/usr/bin/systemctl", "restart", "petcam-stream.service"]
    if os.geteuid() != 0:
        command = ["sudo", "-n", *command]
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode:
        raise ConfigApplyError(result.stderr.strip() or "Unable to restart the camera stream service.")

    # The RTSP server opens V4L2 after petcam-stream publishes the selected mode.
    try:
        output_config = json.loads(Path("/etc/petcam/rtmp.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        output_config = {"enabled": False}
    if output_config.get("enabled") is True:
        output_command = ["/usr/local/sbin/petcam-rtmp-service", "restart"]
        if os.geteuid() != 0:
            output_command = ["sudo", "-n", *output_command]
        output = subprocess.run(output_command, capture_output=True, text=True, timeout=30, check=False)
        if output.returncode:
            raise ConfigApplyError(output.stderr.strip() or "Unable to start the GStreamer RTSP server.")


def _apply_config_transaction(
    updates: dict[str, Any],
    path: Path | str = CONFIG_PATH,
    last_good_path: Path | str = LAST_GOOD_PATH,
    restart=restart_capture,
    ready=wait_for_stream,
) -> dict[str, str]:
    path = Path(path)
    last_good_path = Path(last_good_path)
    active_raw = path.read_bytes() if path.exists() else None
    try:
        previous = parse_config(path)
        previous_content = active_raw if active_raw is not None else serialize_config(previous)
    except ConfigError:
        try:
            previous = parse_config(last_good_path)
            previous_content = last_good_path.read_bytes()
        except ConfigError:
            previous = validate_config(DEFAULTS)
            previous_content = serialize_config(previous)
    candidate = validate_config({**previous, **updates}, check_hardware=True)

    _atomic_write(path, serialize_config(candidate))
    try:
        restart()
        if not wait_for_configured_capture(candidate, ready):
            expected = "native H.264 packets" if candidate["FORMAT"] == "H264" else "an MJPEG frame"
            raise ConfigApplyError(f"The new camera settings did not produce {expected} before the startup timeout.")
    except Exception as apply_error:
        _atomic_write(path, previous_content)
        try:
            restart()
            restored = wait_for_configured_capture(previous, ready)
        except Exception as rollback_error:
            raise ConfigApplyError(
                f"New settings failed ({apply_error}); previous settings were restored, but restarting them failed ({rollback_error})."
            ) from apply_error
        if not restored:
            raise ConfigApplyError(
                f"New settings failed ({apply_error}); previous settings were restored, but the previous stream did not become healthy."
            ) from apply_error
        _atomic_write(last_good_path, previous_content)
        raise ConfigApplyError(
            f"New settings failed ({apply_error}); previous settings were restored and the stream is healthy again."
        ) from apply_error
    _atomic_write(last_good_path, serialize_config(candidate))
    return candidate


_apply_lock = threading.Lock()


def _config_lock_path(path: Path | str = CONFIG_PATH) -> Path:
    path = Path(path)
    return path.with_name(".petcam-config.lock")


def ensure_config_lock(path: Path | str = CONFIG_PATH) -> Path:
    lock_path = _config_lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        os.chmod(lock_path, 0o660)
        try:
            import grp
            os.chown(lock_path, -1, grp.getgrnam("petcam").gr_gid)
        except (ImportError, KeyError, PermissionError):
            pass
    return lock_path


@contextmanager
def config_transaction_guard(path: Path | str = CONFIG_PATH):
    """Hold the configuration lock while another subsystem inspects stream health."""
    lock_path = ensure_config_lock(path)
    with lock_path.open("r+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def configuration_change_in_progress(path: Path | str = CONFIG_PATH) -> bool:
    lock_path = _config_lock_path(path)
    if not lock_path.exists():
        return False
    try:
        with lock_path.open("r+") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def apply_config(
    updates: dict[str, Any],
    path: Path | str = CONFIG_PATH,
    last_good_path: Path | str = LAST_GOOD_PATH,
    restart=restart_capture,
    ready=wait_for_stream,
) -> dict[str, str]:
    path = Path(path)
    lock_path = ensure_config_lock(path)
    with _apply_lock, lock_path.open("r+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            return _apply_config_transaction(
                updates, path, last_good_path, restart=restart, ready=ready
            )
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def main() -> int:
    arguments = sys.argv[1:]
    if arguments in (["validate"], ["validate", "--check-hardware"]):
        try:
            print(json.dumps(parse_config(check_hardware="--check-hardware" in arguments), indent=2))
            return 0
        except ConfigError as error:
            print(str(error), file=sys.stderr)
            return 2
    if len(arguments) == 2 and arguments[0] == "apply":
        candidate_path = Path(arguments[1])
        if candidate_path.resolve() == CONFIG_PATH.resolve():
            print("Apply requires a separate candidate file; do not overwrite the active config before validation.", file=sys.stderr)
            return 2
        try:
            candidate = parse_config(candidate_path, check_hardware=True)
            applied = apply_config(candidate)
            print(json.dumps(applied, indent=2))
            return 0
        except ConfigError as error:
            print(str(error), file=sys.stderr)
            return 1
    print(
        "Usage: petcam-config validate [--check-hardware] | petcam-config apply CANDIDATE_FILE",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
