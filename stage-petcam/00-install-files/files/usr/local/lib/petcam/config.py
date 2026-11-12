#!/usr/bin/env python3
"""Shared PetCam configuration parser, validator, and transactional apply logic."""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CONFIG_PATH = Path("/etc/petcam/petcam.conf")
LAST_GOOD_PATH = Path("/etc/petcam/petcam.conf.last-good")
DEFAULTS = {
    "SOURCE": "auto",
    "DEVICE": "",
    "CAMERA": "auto",
    "FORMAT": "auto",
    "RESOLUTION": "auto",
    "FPS": "auto",
    "CSI_QUALITY": "70",
    "PORT": "8081",
}
FORMATS = {
    "MJPG", "MJPEG", "JPEG", "YUYV", "YVYU", "UYVY",
    "YUV420", "YVU420", "RGB565", "RGB24", "GREY",
}
DEVICE_PATTERN = re.compile(r"^/dev/(?:video[0-9]+|v4l/by-id/[A-Za-z0-9._:+-]+)$")
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
    if config["CAMERA"] != "auto" and not config["CAMERA"].isdigit():
        raise ConfigError("CAMERA must be auto or a non-negative CSI camera index.")
    if config["SOURCE"] == "usb" and config["CAMERA"] != "auto":
        raise ConfigError("CAMERA must be auto when SOURCE=usb.")
    if config["SOURCE"] == "csi" and config["DEVICE"]:
        raise ConfigError("DEVICE must be empty when SOURCE=csi.")

    config["FORMAT"] = "auto" if config["FORMAT"].lower() == "auto" else config["FORMAT"].upper()
    if config["FORMAT"] != "auto" and config["FORMAT"] not in FORMATS:
        raise ConfigError(f"FORMAT must be auto or one of: {', '.join(sorted(FORMATS))}.")

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
        if config["SOURCE"] in {"auto", "csi"} and config["CAMERA"] != "auto":
            try:
                result = subprocess.run(
                    ["/usr/local/bin/petcam-camera", "discover-csi"],
                    capture_output=True, text=True, timeout=20, check=False,
                )
                if result.returncode:
                    raise ConfigError(result.stderr.strip() or "Unable to discover CSI cameras.")
                cameras = json.loads(result.stdout).get("csi_cameras", [])
                if not any(str(camera.get("index")) == config["CAMERA"] for camera in cameras):
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


def restart_capture() -> None:
    command = ["/usr/bin/systemctl", "restart", "petcam-stream.service"]
    if os.geteuid() != 0:
        command = ["sudo", "-n", *command]
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode:
        raise ConfigApplyError(result.stderr.strip() or "Unable to restart the camera stream service.")


def apply_config(
    updates: dict[str, Any],
    path: Path | str = CONFIG_PATH,
    last_good_path: Path | str = LAST_GOOD_PATH,
    restart=restart_capture,
    ready=wait_for_stream,
) -> dict[str, str]:
    path = Path(path)
    last_good_path = Path(last_good_path)
    previous_raw = path.read_bytes() if path.exists() else None
    previous = parse_config(path)
    candidate = validate_config({**previous, **updates}, check_hardware=True)
    previous_content = previous_raw if previous_raw is not None else serialize_config(previous)

    _atomic_write(last_good_path, previous_content)
    _atomic_write(path, serialize_config(candidate))
    try:
        restart()
        if not ready(int(candidate["PORT"])):
            raise ConfigApplyError("The new camera settings did not produce an MJPEG frame before the startup timeout.")
    except Exception as apply_error:
        if previous_raw is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_write(path, previous_raw)
        try:
            restart()
            restored = ready(int(previous["PORT"]))
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
