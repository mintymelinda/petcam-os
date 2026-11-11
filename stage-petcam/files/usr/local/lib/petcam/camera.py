#!/usr/bin/env python3
"""PetCam camera capability discovery and hardware-aware mode selection."""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

MODE_RE = re.compile(r"Size: Discrete (\d+)x(\d+)")
FORMAT_RE = re.compile(r"\[\d+\]: '([^']+)'")
FPS_RE = re.compile(r"\(([\d.]+) fps\)")
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
    return bool(limits and limits["min"] <= fps <= limits["max"])

def model_tier(model):
    value = model.lower()
    if "zero 2" in value or "zero 2 w" in value:
        return "zero2"
    if "zero" in value:
        return "zero"
    if "raspberry pi 1" in value:
        return "pi1"
    if "raspberry pi 2" in value:
        return "pi2"
    if "raspberry pi 5" in value:
        return "pi5"
    if "raspberry pi 4" in value or "pi 400" in value or "compute module 4" in value:
        return "pi4"
    if "raspberry pi 3" in value:
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
    for pattern in ("/dev/v4l/by-id/*", "/dev/video*"):
        for entry in sorted(Path("/").glob(pattern.lstrip("/"))):
            try:
                resolved = str(entry.resolve(strict=True))
            except OSError:
                continue
            if resolved not in paths:
                paths.append(resolved)
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


def select_mode(devices, tier):
    candidates = []
    for device in devices:
        for mode in device["modes"]:
            # Native MJPEG is preferred; uncompressed modes remain discoverable.
            fmt_rank = 0 if mode["format"] in ("MJPG", "MJPEG") else 1
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
    parser.add_argument("command", choices=("discover", "select", "status"))
    parser.add_argument("--device")
    parser.add_argument("--resolution", default="auto")
    parser.add_argument("--fps", default="auto")
    args = parser.parse_args()

    model = read_model()
    tier = model_tier(model)
    devices = discover_devices()
    if args.device:
        devices = [item for item in devices if item["device"] == str(Path(args.device).resolve()) or item["device"] == args.device]

    if args.command == "discover":
        print(json.dumps({"model": model, "tier": tier, "devices": devices}, indent=2))
        return 0
    if args.command == "select":
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
            matching = [m for m in device["modes"] if mode_supports_resolution(m, width_value, height_value)]
            if not matching:
                print(f"Requested resolution {args.resolution} is not advertised by {device['device']}.", file=sys.stderr)
                return 2
            matching.sort(key=lambda m: (m["format"] not in ("MJPG", "MJPEG"), -max(m["fps"], default=0)))
            mode = matching[0]
            native = mode["format"] in ("MJPG", "MJPEG")
        width = args.resolution if args.resolution != "auto" else f'{mode["width"]}x{mode["height"]}'
        fps = args.fps if args.fps != "auto" else "0"
        fmt = mode["format"]
        encoder = "HW" if fmt in ("MJPG", "MJPEG") else "CPU"
        if args.fps != "auto" and (not args.fps.isdigit() or int(args.fps) < 1):
            print("FPS must be 'auto' or a positive integer.", file=sys.stderr)
            return 2
        if args.fps != "auto":
            fps_value = int(args.fps)
            if (mode["fps"] or mode.get("fps_range")) and not mode_supports_fps(mode, fps_value):
                print(f"Requested {fps_value} FPS is not advertised for {width} {fmt}.", file=sys.stderr)
                return 2
        selected_fps = int(fps) if fps != "0" else mode_max_fps(mode)
        pixel_rate = mode["width"] * mode["height"] * selected_fps
        warning = pixel_rate > PIXEL_BUDGETS[tier] or not native
        print(json.dumps({
            "device": device["device"], "resolution": width, "fps": fps,
            "format": "JPEG" if fmt in ("MJPG", "MJPEG") else fmt,
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
    def transmitted_bytes():
        total = 0
        for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
            fields = line.split()
            if len(fields) >= 10 and fields[0].rstrip(":") != "lo":
                total += int(fields[9])
        return total
    tx_before = transmitted_bytes()
    time.sleep(1)
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
        "latest_captured_fps": int(fps_values[-1]) if fps_values else None,
        "network_transmitted_bytes_since_boot": tx_after,
        "network_interface_tx_mbps_sampled": round(max(0, tx_after - tx_before) * 8 / 1_000_000, 3),
        "temperature": temp,
        "dropped_frames": None,
        "dropped_frames_note": "The standard UVC/µStreamer interface does not expose a reliable cumulative dropped-frame counter; not inferred.",
        "bitrate_note": "Cumulative interface bytes are reported; bitrate requires comparing two samples over a known interval.",
    }
    print(json.dumps(report, indent=2))
    return 0 if status == "active" else 1


if __name__ == "__main__":
    sys.exit(main())
