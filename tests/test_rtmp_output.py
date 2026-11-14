import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "petcam_obs_output",
    "stage-petcam/00-install-files/files/usr/local/lib/petcam/rtmp_output.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class OBSOutputTests(unittest.TestCase):
    def valid_config(self, **overrides):
        return {
            "enabled": True,
            "encoder": "libx264",
            "width": 1280,
            "height": 720,
            "fps": 30,
            "bitrate_kbps": 2500,
            **overrides,
        }

    def test_output_settings_are_bounded_and_encoder_is_allowlisted(self):
        with self.assertRaises(MODULE.RTMPConfigError):
            MODULE.validate_config(self.valid_config(fps=0))
        with self.assertRaises(MODULE.RTMPConfigError):
            MODULE.validate_config(self.valid_config(encoder="shell-command"))

    def test_public_config_contains_no_remote_destination_or_credentials(self):
        public = MODULE.public_config(MODULE.validate_config(self.valid_config()))
        self.assertNotIn("server_url", public)
        self.assertNotIn("stream_key", public)

    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "obs-output.json"
            saved = MODULE.save_config(self.valid_config(), path)
            loaded = MODULE.load_config(path)
        self.assertEqual(saved, loaded)

    def test_legacy_rtmp_configuration_is_migrated_and_secret_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rtmp.json"
            path.write_text(json.dumps({
                **self.valid_config(),
                "server_url": "rtmps://live.example.test/app",
                "stream_key": "old-private-key",
            }), encoding="utf-8")
            loaded = MODULE.load_config(path)
            persisted = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(loaded["width"], 1280)
        self.assertNotIn("server_url", persisted)
        self.assertNotIn("stream_key", persisted)
        self.assertNotIn("old-private-key", path.read_text(encoding="utf-8"))

    def test_native_h264_path_uses_mpegts_and_stream_copy_to_local_rtsp(self):
        config = MODULE.validate_config(self.valid_config())
        with patch.object(MODULE.shutil, "which", return_value="/usr/bin/ffmpeg"):
            command = MODULE.build_ffmpeg_command(config, native_h264=True)
        self.assertIn("mpegts", command)
        self.assertIn("udp://127.0.0.1:10000?fifo_size=1000000&overrun_nonfatal=1", command)
        self.assertIn("copy", command)
        self.assertNotIn("mpjpeg", command)
        self.assertNotIn("libx264", command)
        self.assertIn("rtsp", command)
        self.assertEqual(command[-1], "rtsp://127.0.0.1:8554/petcam")

    def test_mjpeg_fallback_encodes_and_publishes_to_local_rtsp(self):
        config = MODULE.validate_config(self.valid_config())
        with patch.object(MODULE.shutil, "which", return_value="/usr/bin/ffmpeg"):
            command = MODULE.build_ffmpeg_command(config)
        self.assertIn("http://127.0.0.1:8081/stream", command)
        self.assertIn("mpjpeg", command)
        self.assertIn("libx264", command)
        self.assertIn("rtsp", command)
        self.assertEqual(command[-1], "rtsp://127.0.0.1:8554/petcam")

    def test_native_h264_detection_reads_capture_status(self):
        response = type("Response", (), {
            "__enter__": lambda self: self,
            "__exit__": lambda self, *args: None,
            "read": lambda self, size: b'{"native_h264":true}',
        })()
        with patch.object(MODULE.urllib.request, "urlopen", return_value=response):
            self.assertTrue(MODULE.native_h264_available(timeout=0))

    def test_disabled_output_cannot_build_publisher_command(self):
        config = MODULE.validate_config({"enabled": False})
        with patch.object(MODULE.shutil, "which", return_value="/usr/bin/ffmpeg"):
            with self.assertRaisesRegex(MODULE.RTMPConfigError, "disabled"):
                MODULE.build_ffmpeg_command(config)


if __name__ == "__main__":
    unittest.main()
