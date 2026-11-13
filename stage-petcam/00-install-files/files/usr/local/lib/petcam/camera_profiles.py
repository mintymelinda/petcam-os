#!/usr/bin/env python3
"""Stable identities, independent configurations, and resource checks for camera streams."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
from contextlib import contextmanager
from functools import wraps
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from camera import (
    PIXEL_BUDGETS,
    USTREAMER_FORMATS,
    discover_csi_cameras,
    discover_devices,
    filter_devices_by_format,
    mode_max_fps,
    mode_supports_fps,
    mode_supports_resolution,
    model_tier,
    read_model,
    select_csi_mode,
    select_mode,
)
from config import (
    CONFIG_PATH,
    ConfigError,
    _atomic_write,
    apply_config,
    parse_config,
    serialize_config,
    validate_config,
    wait_for_stream,
)

PROFILE_DIR = Path("/etc/petcam/cameras.d")
INDEX_FILE = Path("/etc/petcam/cameras.json")
MIN_PROFILE_PORT = 8082
MAX_PROFILE_PORT = 8099
USB2_SAFE_BANDWIDTH_MBPS = 200.0
FRAME_BUFFERS_PER_STREAM = 4
BYTES_PER_PIXEL = 3


@contextmanager
def profile_manager_guard():
    lock_path = INDEX_FILE.with_name("cameras.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o660)
    try:
        os.chmod(lock_path, 0o660)
        try:
            import grp
            os.chown(lock_path, -1, grp.getgrnam("petcam").gr_gid)
        except (ImportError, KeyError, PermissionError):
            pass
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _profile_locked(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        with profile_manager_guard():
            return function(*args, **kwargs)
    return guarded


class ProfileError(ValueError):
    """Raised when a camera profile cannot be safely created or changed."""


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-") or "camera"


def camera_identity(camera: dict[str, Any]) -> dict[str, Any]:
    kind = camera.get("kind")
    if kind == "usb":
        device = str(camera.get("device", ""))
        if not re.fullmatch(r"/dev/(?:video[0-9]+|v4l/by-id/[A-Za-z0-9._:+-]+)", device):
            raise ProfileError("USB camera has no valid device path.")
        physical_path = str(camera.get("physical_path", ""))
        stable = device.startswith("/dev/v4l/by-id/") or bool(physical_path)
        identity_source = device if device.startswith("/dev/v4l/by-id/") else physical_path or device
        digest = hashlib.sha256(identity_source.encode("utf-8")).hexdigest()[:12]
        return {
            "id": f"usb-{digest}",
            "kind": "usb",
            "device": device,
            "physical_path": physical_path,
            "stable_identity": stable,
            "label": camera.get("label") or f"USB camera · {device}",
        }
    if kind == "csi":
        sensor = _slug(str(camera.get("sensor", "csi")))
        index = camera.get("camera")
        device_path = str(camera.get("device_path", ""))
        if not isinstance(index, int) or index < 0:
            raise ProfileError("CSI camera index is invalid.")
        if device_path:
            digest = hashlib.sha256(f"{sensor}|{device_path}".encode("utf-8")).hexdigest()[:12]
            profile_id = f"csi-{digest}"
        else:
            profile_id = f"csi-{sensor}-{index}"
        return {
            "id": profile_id,
            "kind": "csi",
            "camera": index,
            "sensor": sensor,
            "device_path": device_path,
            "stable_identity": bool(device_path),
            "label": camera.get("label") or f"CSI {sensor} · camera {index}",
        }
    raise ProfileError("Camera type must be usb or csi.")


def _load_profiles() -> list[dict[str, Any]]:
    try:
        value = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as error:
        raise ProfileError(f"Unable to read camera profile index: {error}") from error
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ProfileError("Camera profile index is malformed.")
    seen_ids = set()
    seen_ports = set()
    for profile in value:
        profile_id = profile.get("id")
        if not isinstance(profile_id, str) or not re.fullmatch(r"[a-z0-9-]{1,64}", profile_id):
            raise ProfileError("Camera profile index contains an invalid identity.")
        if profile_id in seen_ids:
            raise ProfileError("Camera profile index contains a duplicate identity.")
        seen_ids.add(profile_id)
        port = profile.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not MIN_PROFILE_PORT <= port <= MAX_PROFILE_PORT:
            raise ProfileError(f"Camera profile {profile_id} has an invalid stream port.")
        if port in seen_ports:
            raise ProfileError(f"Camera profile index contains a duplicate stream port: {port}.")
        seen_ports.add(port)
        if profile.get("service") != f"petcam-stream@{profile_id}.service":
            raise ProfileError(f"Camera profile {profile_id} has an invalid systemd service name.")
    return value


def _save_profiles(profiles: list[dict[str, Any]]) -> None:
    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = INDEX_FILE.with_name(INDEX_FILE.name + ".tmp")
    temporary.write_text(json.dumps(profiles, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o640)
    try:
        import grp
        os.chown(temporary, -1, grp.getgrnam("petcam").gr_gid)
    except (ImportError, KeyError, PermissionError):
        pass
    os.replace(temporary, INDEX_FILE)


def profile_exists(camera: dict[str, Any]) -> bool:
    identity = camera_identity(camera)
    return any(item.get("id") == identity["id"] for item in _load_profiles())


def _profile_config_path(profile_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9-]{1,64}", profile_id):
        raise ProfileError("Invalid camera profile identity.")
    return PROFILE_DIR / f"{profile_id}.conf"


def _profile_service_action(action: str, profile_id: str) -> None:
    if action not in {"start", "stop", "restart", "enable", "disable"}:
        raise ProfileError("Invalid stream service action.")
    if not re.fullmatch(r"[a-z0-9-]{1,64}", profile_id):
        raise ProfileError("Invalid camera profile identity.")
    command = ["sudo", "-n", "/usr/local/sbin/petcam-stream-service", action, profile_id]
    result = subprocess.run(command, capture_output=True, text=True, timeout=45, check=False)
    if result.returncode:
        raise ProfileError(result.stderr.strip() or f"Unable to {action} camera stream {profile_id}.")


def _mode_selection(camera: dict[str, Any], mode: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    width, height, fps = mode.get("width"), mode.get("height"), mode.get("fps")
    if (
        not isinstance(width, int) or not isinstance(height, int)
        or not isinstance(fps, (int, float))
        or not 1 <= width <= 10000 or not 1 <= height <= 10000 or not 0 < fps <= 240
    ):
        raise ProfileError("Invalid capture mode.")
    identity = camera_identity(camera)
    if identity["kind"] == "usb":
        if not float(fps).is_integer():
            raise ProfileError("USB camera FPS must be a whole number.")
        discovered = next(
            (
                item for item in discover_devices()
                if (
                    item.get("physical_path") == identity["physical_path"]
                    if identity["physical_path"]
                    else item["device"] == identity["device"]
                )
            ),
            None,
        )
        if not discovered:
            raise ProfileError("USB camera is no longer available.")
        selected_format = str(mode.get("format", "")).upper()
        if selected_format not in USTREAMER_FORMATS:
            raise ProfileError(f"USB format {selected_format or '(empty)'} is not supported by the stream backend.")
        matches = [
            item for item in discovered["modes"]
            if item["format"] == selected_format
            and mode_supports_resolution(item, width, height)
            and (not item["fps"] and not item.get("fps_range") or mode_supports_fps(item, float(fps)))
        ]
        if not matches:
            raise ProfileError("That USB mode is no longer advertised by the camera.")
        values = {
            "SOURCE": "usb",
            "DEVICE": identity["device"] if identity["device"].startswith("/dev/v4l/by-id/") or not identity["physical_path"] else "",
            "USB_PHYSICAL_PATH": identity["physical_path"],
            "CSI_DEVICE_PATH": "",
            "CAMERA": "auto",
            "FORMAT": selected_format,
            "RESOLUTION": f"{width}x{height}",
            "FPS": str(int(fps)),
        }
        output_format = selected_format
    else:
        discovered = next(
            (
                item for item in discover_csi_cameras()
                if (
                    item.get("device_path") == identity["device_path"]
                    if identity["device_path"]
                    else item["index"] == identity["camera"] and item["sensor"].lower() == identity["sensor"]
                )
            ),
            None,
        )
        if not discovered or not any(
            item["width"] == width and item["height"] == height and abs(item["fps"] - fps) < 0.1
            for item in discovered["modes"]
        ):
            raise ProfileError("That CSI mode is no longer advertised by the camera.")
        values = {
            "SOURCE": "csi",
            "DEVICE": "",
            "USB_PHYSICAL_PATH": "",
            "CSI_DEVICE_PATH": identity["device_path"],
            "CAMERA": "auto" if identity["device_path"] else str(identity["camera"]),
            "FORMAT": "auto",
            "RESOLUTION": f"{width}x{height}",
            "FPS": str(fps),
        }
        output_format = "MJPEG"
    return values, {
        **identity,
        "resolution": f"{width}x{height}",
        "width": width,
        "height": height,
        "fps": float(fps),
        "format": output_format,
        "estimated_pixels_per_second": int(width * height * fps),
        "buffer_memory_bytes": width * height * BYTES_PER_PIXEL * FRAME_BUFFERS_PER_STREAM,
        "usb_raw_mbps": _usb_raw_mbps(identity["kind"], output_format, width, height, fps),
    }


def _usb_raw_mbps(kind: str, pixel_format: str, width: int, height: int, fps: float) -> float:
    if kind != "usb" or pixel_format in {"MJPG", "MJPEG", "JPEG"}:
        return 0.0
    bytes_per_pixel = {
        "YUYV": 2.0, "YVYU": 2.0, "UYVY": 2.0,
        "YUV420": 1.5, "YVU420": 1.5, "RGB565": 2.0,
        "RGB24": 3.0, "GREY": 1.0,
    }.get(pixel_format, 0.0)
    return width * height * fps * bytes_per_pixel * 8 / 1_000_000


def _same_usb_device(first: str, second: str) -> bool:
    try:
        return Path(first).resolve() == Path(second).resolve()
    except OSError:
        return first == second


def _default_stream_estimate(config_override: dict[str, str] | None = None) -> dict[str, Any]:
    """Mirror the primary stream's automatic selection to budget all active cameras."""
    config = config_override or parse_config(CONFIG_PATH, check_hardware=False)
    model = read_model()
    tier = model_tier(model)
    if config["SOURCE"] in {"auto", "usb"}:
        devices = discover_devices()
        if config.get("USB_PHYSICAL_PATH"):
            devices = [item for item in devices if item.get("physical_path") == config["USB_PHYSICAL_PATH"]]
        if config["DEVICE"]:
            devices = [item for item in devices if _same_usb_device(item["device"], config["DEVICE"])]
        devices = filter_devices_by_format(devices, config["FORMAT"])
        selected = select_mode(devices, tier)
        if selected:
            device, mode, native = selected
            usable_usb_mode = True
            if config["RESOLUTION"] != "auto":
                import re as regex
                match = regex.fullmatch(r"(\d+)x(\d+)", config["RESOLUTION"])
                width, height = map(int, match.groups())
                matching = [
                    item for item in device["modes"]
                    if item["format"] in USTREAMER_FORMATS
                    and mode_supports_resolution(item, width, height)
                ]
                if matching:
                    mode = matching[0]
                else:
                    usable_usb_mode = False
            if usable_usb_mode and config["FPS"] != "auto" and not mode_supports_fps(mode, float(config["FPS"])):
                usable_usb_mode = False
            if usable_usb_mode:
                width, height = map(int, config["RESOLUTION"].split("x")) if config["RESOLUTION"] != "auto" else (mode["width"], mode["height"])
                fps = float(config["FPS"]) if config["FPS"] != "auto" else mode_max_fps(mode)
                pixel_format = mode["format"]
                return {
                    "id": "default",
                    "kind": "usb",
                    "device": device["device"],
                    "physical_path": device.get("physical_path", ""),
                    "stable_identity": device["device"].startswith("/dev/v4l/by-id/") or bool(device.get("physical_path")),
                    "estimated_pixels_per_second": int(width * height * fps),
                    "buffer_memory_bytes": width * height * BYTES_PER_PIXEL * FRAME_BUFFERS_PER_STREAM,
                    "usb_raw_mbps": _usb_raw_mbps("usb", pixel_format, width, height, fps),
                    "tier": tier,
                }
        if config["SOURCE"] == "usb":
            raise ProfileError("The configured primary USB camera is unavailable; no additional stream can be budgeted.")
    if config["SOURCE"] in {"auto", "csi"}:
        camera_index = None if config["CAMERA"] == "auto" else int(config["CAMERA"])
        selected = select_csi_mode(
            discover_csi_cameras(), tier, camera_index, config["RESOLUTION"], config["FPS"],
            config.get("CSI_DEVICE_PATH") or None,
        )
        if selected:
            width, height = map(int, selected["resolution"].split("x"))
            fps = float(selected["fps"])
            return {
                "id": "default",
                "kind": "csi",
                "camera": selected["camera"],
                "sensor": selected["sensor"],
                "device_path": selected.get("device_path", ""),
                "stable_identity": bool(selected.get("device_path")),
                "estimated_pixels_per_second": int(width * height * fps),
                "buffer_memory_bytes": width * height * BYTES_PER_PIXEL * FRAME_BUFFERS_PER_STREAM,
                "usb_raw_mbps": 0.0,
                "tier": tier,
            }
    raise ProfileError("The primary camera mode could not be estimated safely; no additional stream was started.")


def _available_memory_bytes() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 256 * 1024 * 1024


def _check_resource_budget(
    candidate: dict[str, Any], existing: list[dict[str, Any]], primary: dict[str, Any] | None = None,
    allow_performance_override: bool = False,
) -> dict[str, Any]:
    primary = primary or _default_stream_estimate()
    model = read_model()
    tier = model_tier(model)
    streams = [primary, *existing, candidate]
    pixel_rate = sum(int(item.get("estimated_pixels_per_second", 0)) for item in streams)
    pixel_budget = PIXEL_BUDGETS[tier]
    if pixel_rate > pixel_budget and not allow_performance_override:
        raise ProfileError(
            f"Adding this camera would exceed the {tier} aggregate capture budget "
            f"({pixel_rate:,} > {pixel_budget:,} pixels/second). Reduce resolution or FPS, "
            "or explicitly confirm the performance warning to continue."
        )
    raw_usb_mbps = sum(float(item.get("usb_raw_mbps", 0)) for item in streams)
    if raw_usb_mbps > USB2_SAFE_BANDWIDTH_MBPS:
        raise ProfileError(
            f"Raw USB camera modes would use an estimated {raw_usb_mbps:.1f} Mbps, "
            f"above the conservative {USB2_SAFE_BANDWIDTH_MBPS:.0f} Mbps USB 2.0 budget."
        )
    buffer_memory = sum(int(item.get("buffer_memory_bytes", 0)) for item in streams)
    memory_budget = min(int(_available_memory_bytes() * 0.25), 128 * 1024 * 1024)
    if buffer_memory > memory_budget:
        raise ProfileError(
            f"Estimated camera frame buffers need {buffer_memory / 1024 / 1024:.1f} MiB, "
            f"above the conservative {memory_budget / 1024 / 1024:.1f} MiB memory budget."
        )
    return {
        "estimated_total_pixels_per_second": pixel_rate,
        "pixel_budget_pixels_per_second": pixel_budget,
        "estimated_raw_usb_mbps": round(raw_usb_mbps, 1),
        "usb2_safe_budget_mbps": USB2_SAFE_BANDWIDTH_MBPS,
        "estimated_frame_buffer_mib": round(buffer_memory / 1024 / 1024, 1),
        "frame_buffer_budget_mib": round(memory_budget / 1024 / 1024, 1),
        "hardware_tier": tier,
    }


def apply_primary_config(
    values: dict[str, str], selected_camera: dict[str, Any], allow_performance_override: bool = False
) -> dict[str, Any]:
    with profile_manager_guard():
        if profile_exists(selected_camera):
            raise ProfileError("This camera already has an independent stream. Select its profile to change it.")
        validate_primary_change(values, allow_performance_override=allow_performance_override)
        return apply_config(values)


def validate_primary_change(
    values: dict[str, str], allow_performance_override: bool = False
) -> dict[str, Any]:
    current = parse_config(CONFIG_PATH, check_hardware=False)
    candidate_config = validate_config({**current, **values}, check_hardware=True)
    primary = _default_stream_estimate(candidate_config)
    performance_exceeded = primary["estimated_pixels_per_second"] > PIXEL_BUDGETS[model_tier(read_model())]
    if performance_exceeded and not allow_performance_override:
        raise ProfileError(
            f"This capture mode exceeds the estimated performance budget for this {model_tier(read_model())} "
            "device. Confirm the warning to use it anyway, or choose a lower resolution or FPS."
        )
    empty_candidate = {
        "estimated_pixels_per_second": 0,
        "buffer_memory_bytes": 0,
        "usb_raw_mbps": 0.0,
    }
    return _check_resource_budget(
        empty_candidate, _load_profiles(), primary=primary,
        allow_performance_override=performance_exceeded and allow_performance_override,
    )


def _next_port(profiles: list[dict[str, Any]]) -> int:
    used = {int(profile.get("port", 0)) for profile in profiles}
    used.add(int(parse_config(CONFIG_PATH).get("PORT", "8081")))
    for port in range(MIN_PROFILE_PORT, MAX_PROFILE_PORT + 1):
        if port not in used:
            return port
    raise ProfileError("No free local camera stream ports remain.")


@_profile_locked
def create_profile(
    camera: dict[str, Any],
    mode: dict[str, Any],
    restart_service=_profile_service_action,
    ready=wait_for_stream,
    allow_performance_override: bool = False,
) -> dict[str, Any]:
    profiles = _load_profiles()
    values, candidate = _mode_selection(camera, mode)
    if any(profile.get("id") == candidate["id"] for profile in profiles):
        raise ProfileError("This camera already has an independent stream profile.")
    primary = _default_stream_estimate()
    same_primary = False
    if candidate["kind"] == "usb" and primary.get("kind") == "usb":
        candidate_path = str(candidate.get("physical_path", ""))
        primary_path = str(primary.get("physical_path", ""))
        same_primary = (
            candidate_path == primary_path
            if candidate_path and primary_path
            else _same_usb_device(str(candidate.get("device", "")), str(primary.get("device", "")))
        )
    elif candidate["kind"] == "csi" and primary.get("kind") == "csi":
        same_primary = (
            candidate.get("device_path") == primary.get("device_path")
            if candidate.get("device_path") and primary.get("device_path")
            else candidate.get("camera") == primary.get("camera")
            and candidate.get("sensor") == primary.get("sensor")
        )
    if same_primary:
        raise ProfileError("This camera is already assigned to the primary /stream endpoint.")
    port = _next_port(profiles)
    candidate["port"] = port
    candidate["service"] = f"petcam-stream@{candidate['id']}.service"
    performance_exceeded = candidate["estimated_pixels_per_second"] > PIXEL_BUDGETS[model_tier(read_model())]
    if performance_exceeded and not allow_performance_override:
        raise ProfileError(
            f"This capture mode exceeds the estimated performance budget for this {model_tier(read_model())} "
            "device. Confirm the warning to use it anyway, or choose a lower resolution or FPS."
        )
    budget = _check_resource_budget(
        candidate, profiles,
        allow_performance_override=performance_exceeded and allow_performance_override,
    )
    validated = validate_config({**values, "PORT": str(port)}, check_hardware=True)
    path = _profile_config_path(candidate["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ProfileError("A camera configuration file already exists for this identity.")
    _atomic_write(path, serialize_config(validated))
    candidate["resource_budget"] = budget
    candidate["created_at"] = datetime.now(timezone.utc).isoformat()
    updated = [*profiles, candidate]
    _save_profiles(updated)
    try:
        restart_service("enable", candidate["id"])
        if not ready(port):
            raise ProfileError("The new camera stream did not produce an MJPEG frame before the startup timeout.")
    except Exception as error:
        try:
            restart_service("disable", candidate["id"])
        except Exception:
            pass
        path.unlink(missing_ok=True)
        _save_profiles(profiles)
        if isinstance(error, ProfileError):
            raise
        raise ProfileError(str(error)) from error
    return candidate


@_profile_locked
def configure_profile(
    profile_id: str,
    camera: dict[str, Any],
    mode: dict[str, Any],
    restart_service=_profile_service_action,
    ready=wait_for_stream,
    allow_performance_override: bool = False,
) -> dict[str, Any]:
    profiles = _load_profiles()
    existing = next((item for item in profiles if item["id"] == profile_id), None)
    if not existing:
        raise ProfileError("Camera stream profile does not exist.")
    values, candidate = _mode_selection(camera, mode)
    if candidate["id"] != profile_id:
        raise ProfileError("The selected hardware does not match this stream's stable camera identity.")
    candidate["port"] = int(existing["port"])
    candidate["service"] = existing["service"]
    others = [item for item in profiles if item["id"] != profile_id]
    performance_exceeded = candidate["estimated_pixels_per_second"] > PIXEL_BUDGETS[model_tier(read_model())]
    if performance_exceeded and not allow_performance_override:
        raise ProfileError(
            f"This capture mode exceeds the estimated performance budget for this {model_tier(read_model())} "
            "device. Confirm the warning to use it anyway, or choose a lower resolution or FPS."
        )
    budget = _check_resource_budget(
        candidate, others,
        allow_performance_override=performance_exceeded and allow_performance_override,
    )
    validated = validate_config({**values, "PORT": str(candidate["port"])}, check_hardware=True)
    path = _profile_config_path(profile_id)
    last_good = path.with_name(path.name + ".last-good")
    try:
        apply_config(
            validated,
            path=path,
            last_good_path=last_good,
            restart=lambda: restart_service("restart", profile_id),
            ready=ready,
        )
    except ConfigError as error:
        raise ProfileError(str(error)) from error
    candidate["resource_budget"] = budget
    candidate["created_at"] = existing.get("created_at")
    _save_profiles([*others, candidate])
    return candidate


@_profile_locked
def remove_profile(profile_id: str, restart_service=_profile_service_action) -> None:
    profiles = _load_profiles()
    existing = next((item for item in profiles if item["id"] == profile_id), None)
    if not existing:
        raise ProfileError("Camera stream profile does not exist.")
    restart_service("disable", profile_id)
    path = _profile_config_path(profile_id)
    path.unlink(missing_ok=True)
    path.with_name(path.name + ".last-good").unlink(missing_ok=True)
    _save_profiles([item for item in profiles if item["id"] != profile_id])


def _service_state(service: str) -> str:
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "is-active", service],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return result.stdout.strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def _stream_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/stream", timeout=3) as response:
            return response.status == 200 and bool(response.read(1))
    except (OSError, urllib.error.URLError, TimeoutError, ValueError):
        return False


def list_profiles() -> list[dict[str, Any]]:
    try:
        default = parse_config(CONFIG_PATH)
        default_port = int(default["PORT"])
    except ConfigError:
        default_port = 8081
    default_state = _service_state("petcam-stream.service")
    result = [{
        "id": "default",
        "label": "Primary camera stream",
        "kind": "primary",
        "port": default_port,
        "service": "petcam-stream.service",
        "state": default_state,
        "ready": default_state == "active" and _stream_ready(default_port),
        "stream_path": "/stream",
        "stable_identity": None,
        "managed_independently": False,
    }]
    for profile in _load_profiles():
        item = dict(profile)
        state = _service_state(profile["service"])
        item.update({
            "state": state,
            "ready": state == "active" and _stream_ready(int(profile["port"])),
            "stream_path": f"/stream/{profile['id']}",
            "managed_independently": True,
        })
        result.append(item)
    return result


def stream_port(profile_id: str) -> int:
    if profile_id in {"", "default"}:
        return int(parse_config(CONFIG_PATH)["PORT"])
    profile = next((item for item in _load_profiles() if item["id"] == profile_id), None)
    if not profile:
        raise ProfileError("Camera stream profile does not exist.")
    return int(profile["port"])


def resource_budget_report() -> dict[str, Any]:
    profiles = _load_profiles()
    primary = _default_stream_estimate()
    model = read_model()
    tier = model_tier(model)
    total_pixel_rate = int(primary["estimated_pixels_per_second"]) + sum(
        int(item.get("estimated_pixels_per_second", 0)) for item in profiles
    )
    return {
        "hardware_model": model,
        "hardware_tier": tier,
        "estimated_total_pixels_per_second": total_pixel_rate,
        "pixel_budget_pixels_per_second": PIXEL_BUDGETS[tier],
        "additional_stream_count": len(profiles),
    }
