import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
import sys
from unittest import mock
from io import BytesIO

from PIL import Image

sys.path.insert(0, str(Path("stage-petcam/00-install-files/files/usr/local/lib/petcam").resolve()))

SPEC = importlib.util.spec_from_file_location(
    "petcam_motion",
    "stage-petcam/00-install-files/files/usr/local/lib/petcam/motion_detection.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def jpeg(color, size=(320, 240), patch=None):
    image = Image.new("RGB", size, color)
    if patch:
        from PIL import ImageDraw
        ImageDraw.Draw(image).rectangle(patch, fill="white")
    output = BytesIO()
    image.save(output, format="JPEG", quality=90)
    return output.getvalue()


class MotionDetectionTests(unittest.TestCase):
    def test_defaults_are_opt_in_and_resource_bounded(self):
        config = MODULE.validate_config({})
        self.assertFalse(config["enabled"])
        self.assertEqual(config["sample_interval_seconds"], 2)
        self.assertEqual(config["confirm_samples"], 2)
        self.assertEqual(config["max_events"], 100)

    def test_rejects_invalid_ranges_and_unknown_settings(self):
        for values in (
            {"sample_interval_seconds": 0},
            {"sensitivity": 101},
            {"minimum_changed_percent": 0},
            {"confirm_samples": 6},
            {"min_event_interval_seconds": 1},
            {"quiet_hours_start": "25:00"},
            {"enabled": "true"},
            {"unexpected": True},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                MODULE.validate_config(values)

    def test_quiet_hours_support_day_and_overnight_windows(self):
        self.assertTrue(MODULE.in_quiet_hours(datetime(2026, 1, 1, 12, 0), "09:00", "17:00"))
        self.assertFalse(MODULE.in_quiet_hours(datetime(2026, 1, 1, 18, 0), "09:00", "17:00"))
        self.assertTrue(MODULE.in_quiet_hours(datetime(2026, 1, 1, 23, 0), "22:00", "07:00"))
        self.assertTrue(MODULE.in_quiet_hours(datetime(2026, 1, 1, 6, 0), "22:00", "07:00"))
        self.assertFalse(MODULE.in_quiet_hours(datetime(2026, 1, 1, 12, 0), "22:00", "22:00"))

    def test_static_scene_does_not_trigger_motion(self):
        detector = MODULE.MotionDetector(MODULE.validate_config({}))
        frame = jpeg("gray")
        self.assertEqual(detector.sample(frame), (0.0, False))
        self.assertFalse(detector.sample(frame)[1])

    def test_motion_requires_consecutive_confirmation_samples(self):
        detector = MODULE.MotionDetector(MODULE.validate_config({"confirm_samples": 2}))
        baseline = jpeg("black")
        changed = jpeg("white")
        detector.sample(baseline)
        percent, detected = detector.sample(changed)
        self.assertGreater(percent, 90)
        self.assertFalse(detected)
        percent, detected = detector.sample(changed)
        self.assertGreater(percent, 90)
        self.assertTrue(detected)
        self.assertFalse(detector.sample(changed)[1])

    def test_minimum_changed_area_rejects_small_scene_changes(self):
        detector = MODULE.MotionDetector(MODULE.validate_config({"minimum_changed_percent": 10}))
        detector.sample(jpeg("black"))
        changed = jpeg("black", patch=(0, 0, 9, 9))
        self.assertFalse(detector.sample(changed)[1])

    def test_sensitivity_adjusts_pixel_delta_threshold(self):
        self.assertLess(MODULE.pixel_delta_threshold(100), MODULE.pixel_delta_threshold(1))

    def test_cooldown_blocks_repeated_events(self):
        self.assertFalse(MODULE.cooldown_elapsed(150, 100, 60))
        self.assertTrue(MODULE.cooldown_elapsed(160, 100, 60))

    def test_event_snapshot_and_history_are_timestamped_and_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frames = root / "events"
            events = root / "events.json"
            config = MODULE.validate_config({"min_free_mb": 64})
            stamp = datetime(2026, 11, 12, 21, 0, tzinfo=timezone.utc)
            with mock.patch.object(MODULE, "free_space_ok", return_value=(True, 1024 * 1024 * 1024)):
                event = MODULE.record_event(jpeg("blue"), 12.34, config, frames, events, stamp)
            self.assertEqual(event["timestamp"], stamp.isoformat())
            self.assertTrue((frames / event["file"]).is_file())
            self.assertIn("20261112T210000", event["file"])
            self.assertEqual(MODULE.load_events(events), [event])
            self.assertFalse(events.with_name("events.json.tmp").exists())

    def test_retention_removes_expired_images_and_history_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frames = root / "events"
            frames.mkdir()
            old = frames / "old.jpg"
            recent = frames / "recent.jpg"
            old.write_bytes(b"old")
            recent.write_bytes(b"new")
            now = 2_000_000
            import os
            os.utime(old, (now - 200000, now - 200000))
            os.utime(recent, (now - 10, now - 10))
            events = root / "events.json"
            MODULE.save_events([
                {"timestamp": "old", "file": "old.jpg", "changed_percent": 10},
                {"timestamp": "recent", "file": "recent.jpg", "changed_percent": 20},
            ], events)
            config = MODULE.validate_config({"retention_days": 1})
            result = MODULE.prune_events(config, frames, events, now)
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())
            self.assertEqual(result["events"], 1)
            self.assertEqual(MODULE.load_events(events)[0]["file"], "recent.jpg")

    def test_event_limit_keeps_most_recent_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frames = root / "events"
            frames.mkdir()
            for name in ("one.jpg", "two.jpg", "three.jpg"):
                (frames / name).write_bytes(b"jpeg")
            events = root / "events.json"
            MODULE.save_events([{"file": name} for name in ("one.jpg", "two.jpg", "three.jpg")], events)
            config = MODULE.validate_config({"max_events": 2})
            result = MODULE.prune_events(config, frames, events)
            self.assertEqual(result["events"], 2)
            self.assertEqual([item["file"] for item in MODULE.load_events(events)], ["two.jpg", "three.jpg"])

    def test_low_storage_prevents_event_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            config = MODULE.validate_config({"min_free_mb": 128})
            with mock.patch.object(MODULE.shutil, "disk_usage", return_value=mock.Mock(free=1)):
                with self.assertRaisesRegex(OSError, "Free space below"):
                    MODULE.record_event(jpeg("red"), 30, config, Path(directory) / "events", Path(directory) / "events.json")
            self.assertEqual(list((Path(directory) / "events").glob("*.jpg")), [])

    def test_state_snapshot_reports_events_and_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frames = root / "events"
            frames.mkdir()
            (frames / "frame.jpg").write_bytes(b"jpeg")
            events = root / "events.json"
            MODULE.save_events([{"file": "frame.jpg", "timestamp": "now", "changed_percent": 8}], events)
            state = root / "status.json"
            state.write_text(json.dumps({"state": "monitoring", "cpu_percent": 1.2}), encoding="utf-8")
            result = MODULE.state_snapshot(MODULE.validate_config({}), state, frames, events)
            self.assertEqual(result["snapshots"], 1)
            self.assertEqual(result["storage_bytes"], 4)
            self.assertEqual(result["event_count"], 1)
            self.assertEqual(result["cpu_percent"], 1.2)

    def test_benchmark_reports_cpu_and_peak_memory(self):
        metrics = MODULE._metrics(__import__("time").monotonic(), __import__("time").process_time())
        self.assertGreaterEqual(metrics["cpu_percent"], 0)
        self.assertGreater(metrics["peak_rss_kib"], 0)

    def test_benchmark_reports_camera_disconnect_as_an_error(self):
        with mock.patch.object(MODULE.urllib.request, "urlopen", side_effect=OSError("camera disconnected")):
            with self.assertRaisesRegex(OSError, "camera disconnected"):
                MODULE.benchmark(10)

    def test_benchmark_rejects_invalid_duration(self):
        with self.assertRaises(ValueError):
            MODULE.benchmark(5)


if __name__ == "__main__":
    unittest.main()
