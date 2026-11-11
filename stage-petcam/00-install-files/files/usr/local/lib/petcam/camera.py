#!/usr/bin/env python3
"""PetCam camera capability discovery and hardware-aware mode selection."""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

MODE_RE = re.compile(r"Size: Discrete (\d+)x(\d+)")
FORMAT_RE = re.compile(r"\[\d+\]: '([^']+)'")
FPS_RE = re.compile(r"\(([\d.]+) fps\)")
SIZE_RANGE_RE = re.compile(r"Size: (?:Stepwise|Continuous) (\d+)x(\d+) - (\d+)x(\d+)(?: with step (\d+)/(\d+))?")
FPS_RANGE_RE = re.compile(r"Interval: (?:Stepwise|Continuous) ([\d.]+)s - ([\d.]+)s(?: with step ([\d.]+)s)?")
NATIVE_JPEG_FORMATS = {"MJPG", "MJPEG", "JPEG"}
USTREAMER_FORMATS = NATIVE_JPEG_FORMATS | {"YUYV", "YVYU", "UYVY", "YUV420", "YVU420", "RGB565", "RGB24", "GREY"}
PIXEL_BUDGETS = {
    "zero": 1280 * 720 * 30,
    "pi1": 640 * 480 * 30,
    "pi2": 1280 * 720 * 30,
    "zero2": 1920 * 1080 * 30,
    "pi3": 1920 * 1080 * 30,
    "pi4": 1920 * 1080 * 60,
    "pi5": 3840 * 2160 * 30,
    "unknown": 1280 * 720 * 30,
}


def parse_modes(output):
    modes = []
    current_format = None
    current_mode = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        fmt = FORMAT_RE.search(line)
        if fmt:
            current_format = fmt.group(1).upper()
            current_mode = None
            continue
        size = MODE_RE.search(line)
        if size and current_format:
            current_mode = {
                "format": current_format,
                "width": int(size.group(1)),
                "height": int(size.group(2)),
                "fps": [],
            }
            modes.append(current_mode)
            continue
        size_range = SIZE_RANGE_RE.search(line)
        if size_range and current_format:
            min_w, min_h, max_w, max_h = map(int, size_range.groups()[:4])
            step_w, step_h = size_range.groups()[4:]
            current_mode = {
                "format": current_format,
                "width": max_w,
                "height": max_h,
                "fps": [],
                "size_range": {
                    "min_width": min_w, "min_height": min_h,
                    "max_width": max_w, "max_height": max_h,
                    "step_width": int(step_w or 1), "step_height": int(step_h or 1),
                },
            }
            modes.append(current_mode)
            continue
        fps = FPS_RE.search(line)
        if fps and current_mode:
            value = float(fps.group(1))
            if value > 0 and value not in current_mode["fps"]:
                current_mode["fps"].append(value)
            continue
        fps_range = FPS_RANGE_RE.search(line)
        if fps_range and current_mode:
            min_interval, max_interval = float(fps_range.group(1)), float(fps_range.group(2))
            if min_interval > 0 and max_interval > 0:
                current_mode["fps_range"] = {
                    "min": round(1 / max_interval, 3),
                    "max": round(1 / min_interval, 3),
                    "min_interval_seconds": min_interval,
                    "max_interval_seconds": max_interval,
                    "interval_step_seconds": float(fps_range.group(3)) if fps_range.group(3) else None,
                }
    for mode in modes:
        mode["fps"].sort(reverse=True)
    return modes


def mode_supports_resolution(mode, width, height):
    if mode["width"] == width and mode["height"] == height:
        return True
    bounds = mode.get("size_range")
    if not bounds:
        return False
    return (
        bounds["min_width"] <= width <= bounds["max_width"]
        and bounds["min_height"] <= height <= bounds["max_height"]
        and (width - bounds["min_width"]) % bounds["step_width"] == 0
        and (height - bounds["min_height"]) % bounds["step_height"] == 0
    )


def mode_max_fps(mode):
    return max(mode["fps"], default=mode.get("fps_range", {}).get("max", 30.0))


def mode_supports_fps(mode, fps):
    if any(abs(value - fps) < 0.1 for value in mode["fps"]):
        return True
    limits = mode.get("fps_range")
    if not limits or not limits["min"] <= fps <= limits["max"]:
        return False
    interval = 1 / fps
    step = limits["interval_step_seconds"]
    if step:
        steps = (interval - limits["min_interval_seconds"]) / step
        return abs(steps - round(steps)) < 0.001
    return limits["min_interval_seconds"] <= interval <= limits["max_interval_seconds"]

CSI_CAMERA_RE = re.compile(r"^\s*(\d+)\s*:\s*(\S+)\s*\[(\d+)x(\d+)\]")
CSI_MODE_RE = re.compile(r"(\d+)x(\d+)\s+\[([\d.]+)\s+fps")


def parse_csi_cameras(output):
    cameras = []
    current = None
    for line in output.splitlines():
        camera_match = CSI_CAMERA_RE.search(line)
        if camera_match:
            current = {
                "index": int(camera_match.group(1)),
                "sensor": camera_match.group(2),
                "sensor_width": int(camera_match.group(3)),
                "sensor_height": int(camera_match.group(4)),
                "modes": [],
            }
            cameras.append(current)
            continue
        mode_match = CSI_MODE_RE.search(line)
        if mode_match and current:
            mode = {
                "width": int(mode_match.group(1)),
                "height": int(mode_match.group(2)),
                "fps": float(mode_match.group(3)),
            }
            if mode not in current["modes"]:
                current["modes"].append(mode)
    return cameras


def discover_csi_cameras():
    result = subprocess.run(
        ["rpicam-hello", "--list-cameras"],
        capture_output=True, text=True, check=False, timeout=15,
    )
    if result.returncode:
        return []
    return parse_csi_cameras(result.stdout + "\n" + result.stderr)


def select_csi_mode(cameras, tier, requested_camera=None, resolution="auto", fps="auto"):
    if requested_camera is not None:
        cameras = [camera for camera in cameras if camera["index"] == requested_camera]
    candidates = []
    for camera in cameras:
        for mode in camera["modes"]:
            candidates.append((
                mode["width"] * mode["height"] * mode["fps"],
                mode["width"] * mode["height"],
                mode["fps"],
                camera,
                mode,
            ))
    if not candidates:
        return None
    if resolution != "auto":
        match = re.fullmatch(r"(\d+)x(\d+)", resolution)
        if not match:
            raise ValueError("Resolution must be 'auto' or WIDTHxHEIGHT.")
        width, height = map(int, match.groups())
        candidates = [item for item in candidates if item[4]["width"] == width and item[4]["height"] == height]
        if not candidates:
            raise ValueError(f"CSI resolution {resolution} is not advertised by the selected camera.")
    if fps != "auto":
        if not fps.isdigit() or int(fps) < 1:
            raise ValueError("FPS must be 'auto' or a positive integer.")
        requested_fps = int(fps)
        candidates = [item for item in candidates if abs(item[4]["fps"] - requested_fps) < 0.1]
        if not candidates:
            raise ValueError(f"CSI frame rate {fps} is not advertised for the selected resolution.")
    budget = PIXEL_BUDGETS[tier]
    within_budget = [item for item in candidates if item[0] <= budget]
    chosen = max(within_budget, key=lambda item: (item[1], item[2])) if within_budget else min(candidates, key=lambda item: item[0])
    camera, mode = chosen[3], chosen[4]
    return {
        "camera": camera["index"],
        "sensor": camera["sensor"],
        "resolution": f'{mode["width"]}x{mode["height"]}',
        "fps": int(mode["fps"]) if mode["fps"].is_integer() else mode["fps"],
        "mode_fps": [item["fps"] for item in camera["modes"] if item["width"] == mode["width"] and item["height"] == mode["height"]],
        "estimated_pixels_per_second": chosen[0],
        "hardware_budget_pixels_per_second": budget,
        "performance_warning": chosen[0] > budget,
    }


def model_tier(model):
    value = model.lower()
    if "zero 2" in value or "zero 2 w" in value:
        return "zero2"
    if "zero" in value:
        return "zero"
    if "raspberry pi 1" in value or "raspberry pi model a" in value or "raspberry pi model b" in value:
        return "pi1"
    if "raspberry pi 2" in value:
        return "pi2"
    if "raspberry pi 5" in value or "raspberry pi 500" in value or "compute module 5" in value:
        return "pi5"
    if "raspberry pi 4" in value or "pi 400" in value or "compute module 4" in value:
        return "pi4"
    if "raspberry pi 3" in value or "compute module 3" in value:
        return "pi3"
    return "unknown"


def read_model():
    path = Path("/proc/device-tree/model")
    try:
        return path.read_text(errors="replace").strip("\x00\n ")
    except OSError:
        return "Unknown Raspberry Pi model"


def discover_devices():
    paths = []
    seen_devices = set()
    for pattern in ("/dev/v4l/by-id/*", "/dev/video*"):
        for entry in sorted(Path("/").glob(pattern.lstrip("/"))):
            try:
                resolved = str(entry.resolve(strict=True))
            except OSError:
                continue
            if resolved not in seen_devices:
                seen_devices.add(resolved)
                # Preserve stable by-id symlinks when available; otherwise use /dev/videoN.
                paths.append(str(entry))
    devices = []
    for device in paths:
        result = subprocess.run(
            ["v4l2-ctl", "--device", device, "--list-formats-ext"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode:
            continue
        modes = parse_modes(result.stdout)
        if modes:
            devices.append({"device": device, "modes": modes})
    return devices

def filter_devices_by_format(devices, requested_format):
    if requested_format == "auto":
        return devices
    requested_format = requested_format.upper()
    return [
        {**device, "modes": [mode for mode in device["modes"] if mode["format"] == requested_format]}
        for device in devices
        if any(mode["format"] == requested_format for mode in device["modes"])
    ]


def select_mode(devices, tier):
    candidates = []
    for device in devices:
        for mode in device["modes"]:
            # Select only formats supported by µStreamer; discovery still lists
            # every advertised fourcc, including formats that cannot be streamed.
            if mode["format"] not in USTREAMER_FORMATS:
                continue
            fmt_rank = 0 if mode["format"] in NATIVE_JPEG_FORMATS else 1
            max_fps = mode_max_fps(mode)
            pixels_per_second = mode["width"] * mode["height"] * max_fps
            candidates.append((fmt_rank, pixels_per_second, mode["width"] * mode["height"], max_fps, device, mode))
    if not candidates:
        return None
    native = [item for item in candidates if item[0] == 0]
    pool = native or candidates
    budget = PIXEL_BUDGETS[tier]
    within_budget = [item for item in pool if item[1] <= budget]
    # If every advertised native mode exceeds the heuristic budget, start
    # with the least demanding one and warn; never hide a camera capability.
    chosen = max(within_budget, key=lambda item: (item[2], item[3])) if within_budget else min(pool, key=lambda item: item[1])
    return chosen[4], chosen[5], bool(native)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("discover", "select", "discover-csi", "select-csi", "status"))
    parser.add_argument("--device")
    parser.add_argument("--camera", type=int)
    parser.add_argument("--format", default="auto")
    parser.add_argument("--resolution", default="auto")
    parser.add_argument("--fps", default="auto")
    args = parser.parse_args()

    model = read_model()
    tier = model_tier(model)
    devices = discover_devices()
    if args.device:
        requested = str(Path(args.device).resolve())
        devices = [
            item for item in devices
            if item["device"] == args.device or str(Path(item["device"]).resolve()) == requested
        ]

    if args.command == "discover":
        print(json.dumps({"model": model, "tier": tier, "usb_devices": devices, "csi_cameras": discover_csi_cameras()}, indent=2))
        return 0
    if args.command == "discover-csi":
        print(json.dumps({"model": model, "tier": tier, "csi_cameras": discover_csi_cameras()}, indent=2))
        return 0
    if args.command == "select-csi":
        try:
            selected_csi = select_csi_mode(
                discover_csi_cameras(), tier, args.camera, args.resolution, args.fps
            )
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2
        if not selected_csi:
            print("No supported CSI camera or camera mode found. Check the ribbon cable and camera-stack detection.", file=sys.stderr)
            return 1
        print(json.dumps({"backend": "csi", "model": model, "tier": tier, **selected_csi}))
        return 0
    if args.command == "select":
        devices = filter_devices_by_format(devices, args.format)
        selected = select_mode(devices, tier)
        if not selected:
            print("No usable V4L2 capture device or supported mode found.", file=sys.stderr)
            return 1
        device, mode, native = selected
        if args.resolution != "auto":
            match = re.fullmatch(r"(\d+)x(\d+)", args.resolution)
            if not match:
                print("Resolution must be 'auto' or WIDTHxHEIGHT.", file=sys.stderr)
                return 2
            width_value, height_value = map(int, match.groups())
            matching = [m for m in device["modes"] if m["format"] in USTREAMER_FORMATS and mode_supports_resolution(m, width_value, height_value)]
            if not matching:
                print(f"Requested resolution {args.resolution} is not advertised by {device['device']}.", file=sys.stderr)
                return 2
            matching.sort(key=lambda m: (m["format"] not in NATIVE_JPEG_FORMATS, -mode_max_fps(m)))
            mode = matching[0]
            native = mode["format"] in NATIVE_JPEG_FORMATS
        width = args.resolution if args.resolution != "auto" else f'{mode["width"]}x{mode["height"]}'
        fps = args.fps if args.fps != "auto" else "0"
        fmt = mode["format"]
        encoder = "HW" if fmt in NATIVE_JPEG_FORMATS else "CPU"
        if args.fps != "auto" and (not args.fps.isdigit() or int(args.fps) < 1):
            print("FPS must be 'auto' or a positive integer.", file=sys.stderr)
            return 2
        if args.fps != "auto":
            fps_value = int(args.fps)
            if (mode["fps"] or mode.get("fps_range")) and not mode_supports_fps(mode, fps_value):
                print(f"Requested {fps_value} FPS is not advertised for {width} {fmt}.", file=sys.stderr)
                return 2
        selected_fps = int(fps) if fps != "0" else mode_max_fps(mode)
        selected_width, selected_height = map(int, width.split("x"))
        pixel_rate = selected_width * selected_height * selected_fps
        warning = pixel_rate > PIXEL_BUDGETS[tier] or not native
        print(json.dumps({
            "device": device["device"], "resolution": width, "fps": fps,
            "format": "JPEG" if fmt in NATIVE_JPEG_FORMATS else fmt,
            "encoder": encoder, "native_mjpeg": native, "model": model,
            "tier": tier, "mode_fps": mode["fps"],
            "estimated_pixels_per_second": pixel_rate,
            "hardware_budget_pixels_per_second": PIXEL_BUDGETS[tier],
            "performance_warning": warning,
        }))
        return 0
    # Runtime snapshot: captured FPS is taken from uStreamer performance logs.
    try:
        status = subprocess.run(
            ["systemctl", "is-active", "petcam-stream.service"],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
    except OSError:
        status = "unknown"
    try:
        log = subprocess.run(
            ["journalctl", "-u", "petcam-stream.service", "-n", "300", "--no-pager"],
            capture_output=True, text=True, check=False,
        ).stdout
    except OSError:
        log = ""
    fps_values = re.findall(r"captured_fps=(\d+)", log)
    resolutions = re.findall(r"Using resolution: (\d+x\d+)", log)
    formats = re.findall(r"Using format: ([A-Za-z0-9_-]+)", log)
    devices_in_log = re.findall(r"Using V4L2 device: (\S+)", log)
    start_config = re.findall(r"Starting camera (\S+) at (\d+x\d+), format=([^,]+)", log)
    port = 8080
    try:
        for config_line in Path("/etc/petcam/petcam.conf").read_text().splitlines():
            match = re.match(r"\s*PORT\s*=\s*(\d+)", config_line)
            if match:
                port = int(match.group(1))
    except OSError:
        pass
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
            http_status = response.status
    except Exception:
        http_status = None
    def sample_stream_mbps():
        received = 0
        started = time.monotonic()
        header = b""
        body_started = False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as stream:
                stream.sendall(b"GET /stream HTTP/1.1\r\nHost: localhost\r\n\r\n")
                while time.monotonic() - started < 1.0:
                    stream.settimeout(min(0.2, max(0.05, 1.0 - (time.monotonic() - started))))
                    try:
                        chunk = stream.recv(65536)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    if not body_started:
                        header += chunk
                        separator = header.find(b"\r\n\r\n")
                        if separator >= 0:
                            if not header.startswith((b"HTTP/1.1 200", b"HTTP/1.0 200")):
                                return None
                            received += len(header) - separator - 4
                            body_started = True
                    else:
                        received += len(chunk)
        except OSError:
            return None
        elapsed = max(time.monotonic() - started, 0.001)
        return round(received * 8 / elapsed / 1_000_000, 3) if body_started else None

    def transmitted_bytes():
        total = 0
        for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
            fields = line.split()
            if len(fields) >= 10 and fields[0].rstrip(":") != "lo":
                total += int(fields[9])
        return total
    tx_before = transmitted_bytes()
    stream_mbps = sample_stream_mbps()
    tx_after = transmitted_bytes()
    temp = "unavailable"
    for path in ("/sys/class/thermal/thermal_zone0/temp",):
        try:
            temp = f"{int(Path(path).read_text().strip()) / 1000:.1f} C"
            break
        except (OSError, ValueError):
            pass
    report = {
        "model": model,
        "service": status,
        "http_endpoint_reachable": http_status is not None,
        "http_status": http_status,
        "camera_devices_detected": len(devices),
        "active_camera_device": devices_in_log[-1] if devices_in_log else (start_config[-1][0] if start_config else None),
        "latest_captured_fps": int(fps_values[-1]) if fps_values else None,
        "active_resolution": resolutions[-1] if resolutions else (start_config[-1][1] if start_config else None),
        "active_capture_format": formats[-1] if formats else (start_config[-1][2] if start_config else None),
        "network_transmitted_bytes_since_boot": tx_after,
        "network_interface_tx_mbps_sampled": round(max(0, tx_after - tx_before) * 8 / 1_000_000, 3),
        "local_mjpeg_stream_mbps_sampled": stream_mbps,
        "temperature": temp,
        "dropped_frames": None,
        "dropped_frames_note": "The standard UVC/µStreamer interface does not expose a reliable cumulative dropped-frame counter; not inferred.",
        "bitrate_note": "The MJPEG stream rate is sampled locally for one second; interface transmit rate includes all non-loopback traffic.",
    }
    print(json.dumps(report, indent=2))
    return 0 if status == "active" else 1


if __name__ == "__main__":
    sys.exit(main())
