import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "petcam_timelapse",
    "stage-petcam/00-install-files/files/usr/local/lib/petcam/timelapse.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class TimelapseTests(unittest.TestCase):
    def test_defaults_are_disabled_and_valid(self):
        self.assertFalse(MODULE.validate_config({})["enabled"])

    def test_rejects_invalid_intervals_and_schedule(self):
        with self.assertRaises(ValueError):
            MODULE.validate_config({"interval_seconds": 10})
        with self.assertRaises(ValueError):
            MODULE.validate_config({"schedule_start": "25:00"})

    def test_schedule_supports_normal_and_overnight_windows(self):
        normal = datetime(2026, 1, 1, 12, 0)
        overnight = datetime(2026, 1, 1, 23, 0)
        morning = datetime(2026, 1, 1, 5, 0)
        self.assertTrue(MODULE.in_schedule(normal, "09:00", "17:00"))
        self.assertFalse(MODULE.in_schedule(normal, "18:00", "06:00"))
        self.assertTrue(MODULE.in_schedule(overnight, "18:00", "06:00"))
        self.assertTrue(MODULE.in_schedule(morning, "18:00", "06:00"))

    def test_config_write_is_atomic_and_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            expected = MODULE.save_config({"enabled": True, "interval_seconds": 120}, path)
            self.assertEqual(MODULE.load_config(path), expected)
            self.assertFalse(path.with_name("config.json.tmp").exists())

    def test_capture_uses_predictable_utc_timestamp_and_atomic_write(self):
        with tempfile.TemporaryDirectory() as directory:
            frame = b"\xff\xd8test-jpeg\xff\xd9"
            stamp = datetime(2026, 11, 12, 12, 30, 15, 123000, tzinfo=timezone.utc)
            path = MODULE.capture_frame(frame, directory, stamp)
            self.assertEqual(path.name, "20261112T123015.123000Z.jpg")
            self.assertEqual(path.read_bytes(), frame)
            self.assertFalse(path.with_suffix(".jpg.tmp").exists())

    def test_rejects_incomplete_jpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                MODULE.capture_frame(b"not-jpeg", directory)

    def test_retention_prunes_by_age_and_storage_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old.jpg"
            newer = root / "newer.jpg"
            old.write_bytes(b"a" * 20)
            newer.write_bytes(b"b" * 20)
            now = 2_000_000
            import os
            os.utime(old, (now - 100000, now - 100000))
            os.utime(newer, (now - 10, now - 10))
            config = MODULE.validate_config({"retention_days": 1, "max_storage_mb": 64})
            result = MODULE.prune_frames(config, root, now)
            self.assertFalse(old.exists())
            self.assertTrue(newer.exists())
            self.assertEqual(result["frames"], 1)

    def test_mjpeg_parser_extracts_complete_frames_across_chunks(self):
        class Response:
            def __init__(self):
                self.parts = [b"boundary\r\n\xff", b"\xd8one\xff", b"\xd9boundary\xff\xd8two\xff\xd9"]
            def read(self, size):
                return self.parts.pop(0) if self.parts else b""
        self.assertEqual(list(MODULE.jpeg_frames(Response())), [b"\xff\xd8one\xff\xd9", b"\xff\xd8two\xff\xd9"])

    def test_storage_cap_prunes_oldest_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            oldest = root / "oldest.jpg"
            newest = root / "newest.jpg"
            with oldest.open("wb") as stream:
                stream.truncate(40 * 1024 * 1024)
            with newest.open("wb") as stream:
                stream.truncate(40 * 1024 * 1024)
            import os
            os.utime(oldest, (100, 100))
            os.utime(newest, (200, 200))
            config = MODULE.validate_config({"max_storage_mb": 64})
            MODULE.prune_frames(config, root, now=300)
            self.assertFalse(oldest.exists())
            self.assertTrue(newest.exists())

    def test_state_snapshot_reports_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "frame.jpg").write_bytes(b"jpeg")
            state = root / "status.json"
            state.write_text(json.dumps({"state": "capturing"}), encoding="utf-8")
            result = MODULE.state_snapshot(MODULE.validate_config({}), state, root)
            self.assertEqual(result["frames"], 1)
            self.assertEqual(result["storage_bytes"], 4)


if __name__ == "__main__":
    unittest.main()
