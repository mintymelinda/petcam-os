#!/usr/bin/env python3
"""Low-resource motion detection using sampled frames from the active MJPEG stream."""
import argparse
from datetime import datetime, timezone
from io import BytesIO
import json
import os
import resource
import shutil
import signal
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageChops

from timelapse import capture_frame, jpeg_frames, prune_frames

BASE = Path("/var/lib/petcam/motion")
CONFIG = BASE / "config.json"
STATE = BASE / "status.json"
EVENTS = BASE / "events.json"
FRAMES = BASE / "events"
STREAM_URL = "http://127.0.0.1:8080/stream"
DEFAULTS = {
    "enabled": False,
    "sample_interval_seconds": 2,
    "sensitivity": 50,
    "minimum_changed_percent": 2.5,
    "confirm_samples": 2,
    "min_event_interval_seconds": 60,
    "quiet_hours_enabled": False,
    "quiet_hours_start": "22:00",
    "quiet_hours_end": "07:00",
    "retention_days": 14,
    "max_storage_mb": 512,
    "min_free_mb": 256,
    "max_events": 100,
}
_running = True


def _clock(value):
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise ValueError("Quiet-hour times must use HH:MM.")
    hour, minute = value[:2], value[3:]
    if not hour.isdigit() or not minute.isdigit() or int(hour) > 23 or int(minute) > 59:
        raise ValueError("Quiet-hour times must be valid 24-hour HH:MM values.")
    return value


def validate_config(values):
    if not isinstance(values, dict):
        raise ValueError("Motion configuration must be an object.")
    unknown = set(values) - set(DEFAULTS)
    if unknown:
        raise ValueError("Unknown motion setting(s): " + ", ".join(sorted(unknown)))
    result = {**DEFAULTS, **values}
    if not isinstance(result["enabled"], bool) or not isinstance(result["quiet_hours_enabled"], bool):
        raise ValueError("enabled and quiet_hours_enabled must be true or false.")
    for key, low, high in (
        ("sample_interval_seconds", 1, 30),
        ("sensitivity", 1, 100),
        ("confirm_samples", 1, 5),
        ("min_event_interval_seconds", 5, 86400),
        ("retention_days", 1, 3650),
        ("max_storage_mb", 64, 102400),
        ("min_free_mb", 64, 102400),
        ("max_events", 1, 1000),
    ):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{key} must be an integer between {low} and {high}.")
    value = result["minimum_changed_percent"]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.1 <= value <= 20:
        raise ValueError("minimum_changed_percent must be between 0.1 and 20.")
    result["minimum_changed_percent"] = float(value)
    result["quiet_hours_start"] = _clock(result["quiet_hours_start"])
    result["quiet_hours_end"] = _clock(result["quiet_hours_end"])
    return result


def load_config(path=CONFIG):
    try:
        values = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        values = DEFAULTS
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Unable to read motion settings: {error}") from error
    return validate_config(values)


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


def in_quiet_hours(now, start, end):
    if start == end:
        return False
    current = now.strftime("%H:%M")
    if start < end:
        return start <= current < end
    return current >= start or current < end


def pixel_delta_threshold(sensitivity):
    return max(8, 52 - round(sensitivity * 0.42))


class MotionDetector:
    """Compare tiny grayscale samples; never decode every frame from the stream."""

    def __init__(self, config):
        self.config = config
        self.baseline = None
        self.consecutive = 0

    def sample(self, jpeg):
        with Image.open(BytesIO(jpeg)) as source:
            source.thumbnail((160, 120), getattr(Image, "Resampling", Image).BILINEAR)
            current = source.convert("L")
        if self.baseline is None or self.baseline.size != current.size:
            self.baseline = current.copy()
            self.consecutive = 0
            return 0.0, False
        difference = ImageChops.difference(self.baseline, current)
        histogram = difference.histogram()
        threshold = pixel_delta_threshold(self.config["sensitivity"])
        changed = sum(histogram[threshold:])
        changed_percent = changed * 100.0 / (current.width * current.height)
        if changed_percent >= self.config["minimum_changed_percent"]:
            self.consecutive += 1
            detected = self.consecutive >= self.config["confirm_samples"]
            if detected:
                self.baseline = current.copy()
                self.consecutive = 0
            return changed_percent, detected
        self.baseline = current.copy()
        self.consecutive = 0
        return changed_percent, False


def load_events(path=EVENTS):
    try:
        events = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return events if isinstance(events, list) else []


def save_events(events, path=EVENTS):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(events, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o640)
    os.replace(temporary, path)


def prune_events(config, directory=FRAMES, events_path=EVENTS, now=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    storage = prune_frames(config, directory, now)
    existing = {path.name for path in directory.glob("*.jpg") if path.is_file()}
    events = [event for event in load_events(events_path) if event.get("file") in existing]
    events = events[-config["max_events"]:]
    save_events(events, events_path)
    return {**storage, "events": len(events)}


def state_snapshot(config, state_path=STATE, frames_dir=FRAMES, events_path=EVENTS):
    try:
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {"state": "waiting", "last_event_at": None, "last_error": None}
    events = load_events(events_path)
    storage = {
        "snapshots": len(list(Path(frames_dir).glob("*.jpg"))) if Path(frames_dir).exists() else 0,
        "storage_bytes": sum(p.stat().st_size for p in Path(frames_dir).glob("*.jpg") if p.is_file()) if Path(frames_dir).exists() else 0,
    }
    return {**state, **storage, "event_count": len(events), "events": list(reversed(events[-20:])), "enabled": config["enabled"], "config": config}


def free_space_ok(config, directory=FRAMES):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(directory).free
    return free >= config["min_free_mb"] * 1024 * 1024, free


def record_event(frame, changed_percent, config, directory=FRAMES, events_path=EVENTS, now=None):
    directory = Path(directory)
    enough_space, free = free_space_ok(config, directory)
    if not enough_space:
        raise OSError(f"Free space below configured minimum ({free // 1048576} MiB available).")
    prune_events(config, directory, events_path)
    stamp = now or datetime.now(timezone.utc)
    path = capture_frame(frame, directory, stamp)
    event = {
        "timestamp": stamp.astimezone(timezone.utc).isoformat(),
        "file": path.name,
        "changed_percent": round(float(changed_percent), 2),
    }
    events = load_events(events_path)
    events.append(event)
    save_events(events[-config["max_events"]:], events_path)
    return event


def _metrics(started, cpu_started):
    usage = resource.getrusage(resource.RUSAGE_SELF)
    elapsed = max(time.monotonic() - started, 0.001)
    cpu = time.process_time() - cpu_started
    return {
        "cpu_percent": round(cpu * 100.0 / elapsed, 2),
        "peak_rss_kib": int(usage.ru_maxrss),
    }


def benchmark(duration_seconds=120, url=STREAM_URL, sample_interval=2):
    if not 10 <= duration_seconds <= 3600:
        raise ValueError("Benchmark duration must be between 10 and 3600 seconds.")
    config = validate_config({"sample_interval_seconds": sample_interval})
    detector = MotionDetector(config)
    started = time.monotonic()
    cpu_started = time.process_time()
    next_sample = 0.0
    samples = 0
    request = urllib.request.Request(url, headers={"User-Agent": "PetCam-Motion-Benchmark/1"})
    with urllib.request.urlopen(request, timeout=10) as response:
        for frame in jpeg_frames(response):
            elapsed = time.monotonic() - started
            if elapsed >= duration_seconds:
                break
            if elapsed < next_sample:
                continue
            next_sample = elapsed + sample_interval
            detector.sample(frame)
            samples += 1
    elapsed = time.monotonic() - started
    metrics = _metrics(started, cpu_started)
    return {
        "duration_seconds": round(elapsed, 2),
        "samples_processed": samples,
        "sample_interval_seconds": sample_interval,
        **metrics,
    }


def cooldown_elapsed(now, last_event, minimum_interval):
    return now - last_event >= minimum_interval


def _last_event_epoch(events):
    if not events:
        return 0.0
    try:
        return datetime.fromisoformat(events[-1]["timestamp"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return 0.0


def run():
    global _running
    BASE.mkdir(parents=True, exist_ok=True)
    FRAMES.mkdir(parents=True, exist_ok=True)
    last_state = None
    while _running:
        try:
            config = load_config()
            if not config["enabled"]:
                if last_state != "disabled":
                    write_state({"state": "disabled", "last_error": None})
                    last_state = "disabled"
                time.sleep(2)
                continue
            now = datetime.now().astimezone()
            if config["quiet_hours_enabled"] and in_quiet_hours(now, config["quiet_hours_start"], config["quiet_hours_end"]):
                if last_state != "quiet_hours":
                    write_state({"state": "quiet_hours", "last_error": None})
                    last_state = "quiet_hours"
                time.sleep(5)
                continue
            directory = FRAMES
            prune_events(config, directory)
            enough_space, free = free_space_ok(config, directory)
            if not enough_space:
                write_state({"state": "storage_limit", "last_error": f"Free space below configured minimum ({free // 1048576} MiB available)."})
                last_state = "storage_limit"
                time.sleep(5)
                continue
            detector = MotionDetector(config)
            last_event = _last_event_epoch(load_events())
            request = urllib.request.Request(STREAM_URL, headers={"User-Agent": "PetCam-Motion/1"})
            stream_ended = True
            started = time.monotonic()
            cpu_started = time.process_time()
            next_sample = 0.0
            next_config_check = 0.0
            last_status_write = 0.0
            with urllib.request.urlopen(request, timeout=10) as response:
                for frame in jpeg_frames(response):
                    if not _running:
                        stream_ended = False
                        break
                    tick = time.monotonic()
                    if tick >= next_config_check:
                        refreshed = load_config()
                        local_now = datetime.now().astimezone()
                        quiet = refreshed["quiet_hours_enabled"] and in_quiet_hours(local_now, refreshed["quiet_hours_start"], refreshed["quiet_hours_end"])
                        if not refreshed["enabled"] or quiet:
                            stream_ended = False
                            break
                        if refreshed != config:
                            config = refreshed
                            detector = MotionDetector(config)
                        next_config_check = tick + 2
                    if tick < next_sample:
                        continue
                    next_sample = tick + config["sample_interval_seconds"]
                    changed_percent, detected = detector.sample(frame)
                    if detected:
                        event_time = datetime.now(timezone.utc)
                        epoch = event_time.timestamp()
                        if cooldown_elapsed(epoch, last_event, config["min_event_interval_seconds"]):
                            enough_space, free = free_space_ok(config, directory)
                            if enough_space:
                                try:
                                    event = record_event(frame, changed_percent, config, directory, now=event_time)
                                except OSError as error:
                                    write_state({"state": "storage_limit", "last_error": str(error)})
                                else:
                                    last_event = epoch
                                    write_state({
                                        "state": "motion_detected", "last_event_at": event["timestamp"],
                                        "last_file": event["file"], "last_motion_percent": event["changed_percent"],
                                        "last_error": None, "event_count": len(load_events()),
                                        **_metrics(started, cpu_started),
                                    })
                            else:
                                write_state({"state": "storage_limit", "last_error": f"Free space below configured minimum ({free // 1048576} MiB available)."})
                    if tick - last_status_write >= 30:
                        write_state({
                            "state": "monitoring", "last_motion_percent": round(changed_percent, 2),
                            "last_error": None, "event_count": len(load_events()),
                            **_metrics(started, cpu_started),
                        })
                        last_status_write = tick
                if stream_ended:
                    write_state({"state": "camera_unavailable", "last_error": "The MJPEG stream ended before the worker was stopped."})
                    last_state = "camera_unavailable"
            if _running:
                time.sleep(5)
        except (OSError, urllib.error.URLError, ValueError) as error:
            write_state({"state": "camera_unavailable", "last_error": str(error)})
            last_state = "camera_unavailable"
            time.sleep(5)
        except Exception as error:
            write_state({"state": "error", "last_error": str(error)})
            last_state = "error"
            time.sleep(5)


def write_state(state, path=STATE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    preserved = {key: previous.get(key) for key in ("last_event_at", "last_file", "event_count")}
    preserved.update(state)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(preserved, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o640)
    os.replace(temporary, path)


def _stop(_signum, _frame):
    global _running
    _running = False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--benchmark-seconds", type=int, default=0, help="Measure real-stream CPU and peak RSS without saving snapshots.")
    parser.add_argument("--sample-interval", type=int, default=2)
    args = parser.parse_args()
    if args.validate:
        print(json.dumps(load_config(), indent=2))
        return 0
    if args.benchmark_seconds:
        print(json.dumps(benchmark(args.benchmark_seconds, sample_interval=args.sample_interval), indent=2))
        return 0
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
