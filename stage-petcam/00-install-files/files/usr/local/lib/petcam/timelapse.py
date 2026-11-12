#!/usr/bin/env python3
"""Capture periodic JPEG frames from PetCam's existing MJPEG stream."""
import argparse
from io import BytesIO
import json
import os
import shutil
import signal
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/var/lib/petcam/timelapse")
CONFIG = BASE / "config.json"
STATE = BASE / "status.json"
FRAMES = BASE / "frames"
DEFAULTS = {
    "enabled": False,
    "interval_seconds": 300,
    "schedule_start": "00:00",
    "schedule_end": "23:59",
    "retention_days": 30,
    "max_storage_mb": 2048,
    "min_free_mb": 256,
    "storage_path": "/var/lib/petcam/timelapse/frames",
    "max_width": 0,
    "jpeg_quality": 0,
}
STREAM_URL = "http://127.0.0.1:8080/stream"
_running = True


def _clock(value):
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise ValueError("Schedule times must use HH:MM.")
    hour, minute = value[:2], value[3:]
    if not hour.isdigit() or not minute.isdigit() or int(hour) > 23 or int(minute) > 59:
        raise ValueError("Schedule times must use valid 24-hour HH:MM values.")
    return value


def validate_config(values):
    if not isinstance(values, dict):
        raise ValueError("Timelapse configuration must be an object.")
    unknown = set(values) - set(DEFAULTS)
    if unknown:
        raise ValueError("Unknown timelapse setting(s): " + ", ".join(sorted(unknown)))
    result = {**DEFAULTS, **values}
    if not isinstance(result["enabled"], bool):
        raise ValueError("enabled must be true or false.")
    for key, low, high in (
        ("interval_seconds", 60, 86400),
        ("retention_days", 1, 3650),
        ("max_storage_mb", 64, 102400),
        ("min_free_mb", 64, 102400),
    ):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{key} must be an integer between {low} and {high}.")
    result["storage_path"] = str(result["storage_path"])
    storage_root = Path("/var/lib/petcam").resolve()
    storage_path = Path(result["storage_path"])
    if not storage_path.is_absolute() or ".." in storage_path.parts:
        raise ValueError("storage_path must be an absolute path inside /var/lib/petcam.")
    if not storage_path.resolve().is_relative_to(storage_root):
        raise ValueError("storage_path must remain inside /var/lib/petcam.")
    for key, low, high in (("max_width", 0, 4096), ("jpeg_quality", 0, 95)):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{key} must be an integer between {low} and {high}.")
    result["schedule_start"] = _clock(result["schedule_start"])
    result["schedule_end"] = _clock(result["schedule_end"])
    return result


def load_config(path=CONFIG):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = DEFAULTS
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Unable to read timelapse settings: {error}") from error
    return validate_config(data)


def save_config(values, path=CONFIG):
    config = validate_config(values)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(config, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o640)
    os.replace(temporary, path)
    return config


def in_schedule(now, start, end):
    current = now.strftime("%H:%M")
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def jpeg_frames(response, chunk_size=4096):
    buffer = bytearray()
    while True:
        reader = getattr(response, "read1", response.read)
        chunk = reader(chunk_size)
        if not chunk:
            return
        buffer.extend(chunk)
        while True:
            start = buffer.find(b"\xff\xd8")
            if start < 0:
                if len(buffer) > 1:
                    del buffer[:-1]
                break
            end = buffer.find(b"\xff\xd9", start + 2)
            if end < 0:
                if start:
                    del buffer[:start]
                break
            yield bytes(buffer[start:end + 2])
            del buffer[:end + 2]


def storage_status(directory=None):
    directory = Path(directory or FRAMES)
    files = sorted((p for p in directory.glob("*.jpg") if p.is_file()), key=lambda p: p.name) if directory.exists() else []
    return {"frames": len(files), "storage_bytes": sum(p.stat().st_size for p in files)}


def prune_frames(config, directory=None, now=None):
    directory = Path(directory or config["storage_path"])
    directory.mkdir(parents=True, exist_ok=True)
    now = now or time.time()
    cutoff = now - config["retention_days"] * 86400
    files = sorted((p for p in directory.glob("*.jpg") if p.is_file()), key=lambda p: (p.stat().st_mtime, p.name))
    for path in files:
        if path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
    files = sorted((p for p in directory.glob("*.jpg") if p.is_file()), key=lambda p: (p.stat().st_mtime, p.name))
    total = sum(p.stat().st_size for p in files)
    maximum = config["max_storage_mb"] * 1024 * 1024
    while files and total > maximum:
        oldest = files.pop(0)
        size = oldest.stat().st_size
        oldest.unlink(missing_ok=True)
        total -= size
    return storage_status(directory)


def capture_frame(frame, directory=None, now=None):
    if not frame.startswith(b"\xff\xd8") or not frame.endswith(b"\xff\xd9"):
        raise ValueError("The camera stream did not provide a complete JPEG frame.")
    directory = Path(directory or FRAMES)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    base = stamp.strftime("%Y%m%dT%H%M%S.%fZ")
    target = directory / f"{base}.jpg"
    suffix = 1
    while target.exists():
        target = directory / f"{base}-{suffix:03d}.jpg"
        suffix += 1
    temporary = target.with_suffix(".jpg.tmp")
    with temporary.open("xb") as stream:
        stream.write(frame)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    return target


def write_state(state, path=STATE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    preserved = {key: previous.get(key) for key in ("last_capture_at", "last_file")}
    preserved.update(state)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(preserved, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def state_snapshot(config, state_path=STATE, frames_dir=None):
    try:
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {"state": "waiting", "last_capture_at": None, "last_error": None}
    return {**state, **storage_status(frames_dir or config["storage_path"]), "enabled": config["enabled"], "config": config}


def _free_space_ok(config, directory=None):
    directory = Path(directory or config["storage_path"])
    directory.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(directory).free
    return free >= config["min_free_mb"] * 1024 * 1024, free


def process_frame(frame, config):
    if config["max_width"] == 0 and config["jpeg_quality"] == 0:
        return frame
    try:
        from PIL import Image
    except ImportError as error:
        raise RuntimeError("Pillow is required to resize or adjust timelapse JPEG quality.") from error
    with Image.open(BytesIO(frame)) as image:
        image.load()
        if config["max_width"] and image.width > config["max_width"]:
            height = max(1, round(image.height * config["max_width"] / image.width))
            resampling = getattr(Image, "Resampling", Image).LANCZOS
            image = image.resize((config["max_width"], height), resampling)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        output = BytesIO()
        image.save(output, format="JPEG", quality=config["jpeg_quality"] or 95, optimize=False)
        return output.getvalue()


def capture_from_stream(config, directory=None, url=STREAM_URL):
    request = urllib.request.Request(url, headers={"User-Agent": "PetCam-Timelapse/1"})
    with urllib.request.urlopen(request, timeout=10) as response:
        frame = next(jpeg_frames(response), None)
    if frame is None:
        raise OSError("The live stream closed before a JPEG frame arrived.")
    return capture_frame(process_frame(frame, config), directory or config["storage_path"])


def run():
    global _running
    BASE.mkdir(parents=True, exist_ok=True)
    FRAMES.mkdir(parents=True, exist_ok=True)
    next_capture = 0.0
    last_error = None
    while _running:
        try:
            config = load_config()
            if not config["enabled"]:
                write_state({"state": "disabled", "last_error": None})
                time.sleep(2)
                continue
            now = datetime.now().astimezone()
            if not in_schedule(now, config["schedule_start"], config["schedule_end"]):
                write_state({"state": "outside_schedule", "last_error": None})
                time.sleep(5)
                continue
            if time.monotonic() < next_capture:
                time.sleep(min(2, max(0.2, next_capture - time.monotonic())))
                continue
            directory = Path(config["storage_path"])
            prune_frames(config, directory)
            enough_space, free = _free_space_ok(config, directory)
            if not enough_space:
                last_error = f"Free space below configured minimum ({free // 1048576} MiB available)."
                write_state({"state": "storage_limit", "last_error": last_error, **storage_status()})
                next_capture = time.monotonic() + config["interval_seconds"]
                time.sleep(2)
                continue
            try:
                path = capture_from_stream(config, directory)
                prune_frames(config, directory)
                last_error = None
                write_state({
                    "state": "running", "last_capture_at": datetime.now(timezone.utc).isoformat(),
                    "last_file": path.name, "last_error": None, **storage_status(),
                })
            except (OSError, urllib.error.URLError, StopIteration, ValueError) as error:
                last_error = str(error)
                write_state({"state": "camera_unavailable", "last_error": last_error, **storage_status()})
            next_capture = time.monotonic() + config["interval_seconds"]
        except Exception as error:
            last_error = str(error)
            write_state({"state": "error", "last_error": last_error, **storage_status()})
            time.sleep(5)


def _stop(_signum, _frame):
    global _running
    _running = False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args()
    if args.validate:
        print(json.dumps(load_config(), indent=2))
        return 0
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
