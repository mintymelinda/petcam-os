import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "petcam_rtmp_output",
    "stage-petcam/00-install-files/files/usr/local/lib/petcam/rtmp_output.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RTMPOutputTests(unittest.TestCase):
    def valid_config(self, **overrides):
        return {
            "enabled": True,
            "server_url": "rtmps://live.example.test/app",
            "stream_key": "secret-stream-key",
            "encoder": "libx264",
            "width": 1280,
            "height": 720,
            "fps": 30,
            "bitrate_kbps": 2500,
            **overrides,
        }

    def test_rtmp_requires_supported_scheme_and_host(self):
        for url in ("https://live.example.test/app", "rtmp:///app", "rtmp://user:pass@host/app", "rtmp://host/app?key=x"):
            with self.subTest(url=url), self.assertRaises(MODULE.RTMPConfigError):
                MODULE.validate_config(self.valid_config(server_url=url))

    def test_enabled_output_requires_server_and_stream_key(self):
        with self.assertRaisesRegex(MODULE.RTMPConfigError, "server URL and stream key"):
            MODULE.validate_config(self.valid_config(stream_key=""))
        with self.assertRaises(MODULE.RTMPConfigError):
            MODULE.validate_config(self.valid_config(server_url=""))

    def test_output_settings_are_bounded_and_encoder_is_allowlisted(self):
        with self.assertRaises(MODULE.RTMPConfigError):
            MODULE.validate_config(self.valid_config(fps=0))
        with self.assertRaises(MODULE.RTMPConfigError):
            MODULE.validate_config(self.valid_config(encoder="shell-command"))

    def test_public_config_never_returns_the_stream_key(self):
        public = MODULE.public_config(MODULE.validate_config(self.valid_config()))
        self.assertEqual(public["stream_key"], "")
        self.assertTrue(public["stream_key_configured"])
        self.assertNotIn("secret-stream-key", repr(public))

    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rtmp.json"
            saved = MODULE.save_config(self.valid_config(), path)
            loaded = MODULE.load_config(path)
        self.assertEqual(saved, loaded)
        self.assertEqual(loaded["stream_key"], "secret-stream-key")

    def test_ffmpeg_command_reuses_local_mjpeg_feed_and_publishes_flv(self):
        config = MODULE.validate_config(self.valid_config())
        with patch.object(MODULE.shutil, "which", return_value="/usr/bin/ffmpeg"):
            command = MODULE.build_ffmpeg_command(config)
        self.assertIn("http://127.0.0.1:8081/stream", command)
        self.assertIn("mpjpeg", command)
        self.assertIn("libx264", command)
        self.assertEqual(command[-3:-1], ["-f", "flv"])
        self.assertEqual(command[-1], "rtmps://live.example.test/app/secret-stream-key")

    def test_disabled_output_cannot_build_publisher_command(self):
        config = MODULE.validate_config({"enabled": False})
        with patch.object(MODULE.shutil, "which", return_value="/usr/bin/ffmpeg"):
            with self.assertRaisesRegex(MODULE.RTMPConfigError, "disabled"):
                MODULE.build_ffmpeg_command(config)


if __name__ == "__main__":
    unittest.main()
