import importlib.util
import json
import os
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
        return {"enabled": True, **overrides}

    def test_output_configuration_contains_only_enabled_switch(self):
        self.assertEqual(MODULE.validate_config(self.valid_config()), {"enabled": True})

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
            persisted_text = path.read_text(encoding="utf-8")
            persisted = json.loads(persisted_text)
        self.assertEqual(loaded, {"enabled": True})
        self.assertNotIn("server_url", persisted)
        self.assertNotIn("stream_key", persisted)
        self.assertNotIn("old-private-key", persisted_text)

    def test_native_h264_pipeline_uses_v4l2_and_rtp_packetization_only(self):
        pipeline = MODULE.build_gstreamer_launch({
            "native_h264": True,
            "device": "/dev/v4l/by-id/usb-Alpha_Imaging_Tech._Corp._Razer_Kiyo-video-index0",
            "resolution": "1280x720",
            "fps": "30",
        })
        self.assertIn("v4l2src", pipeline)
        self.assertIn("video/x-h264,width=1280,height=720,framerate=30/1", pipeline)
        self.assertIn("h264parse", pipeline)
        self.assertIn("rtph264pay name=pay0 pt=96", pipeline)
        for forbidden in ("avdec_h264", "decodebin", "x264enc", "avenc_h264", "ffmpeg", "mpegts", "udp://"):
            self.assertNotIn(forbidden, pipeline)

    def test_csi_hardware_h264_pipeline_reads_local_compressed_stream(self):
        pipeline = MODULE.build_gstreamer_launch({
            "native_h264": True, "backend": "csi", "tcp_port": 8556,
            "resolution": "1920x1080", "fps": 30,
        })
        self.assertIn("tcpclientsrc host=127.0.0.1 port=8556", pipeline)
        self.assertIn("h264parse config-interval=-1", pipeline)
        self.assertIn("rtph264pay name=pay0 pt=96", pipeline)
        for forbidden in ("v4l2src", "avdec_h264", "x264enc", "avenc_h264", "ffmpeg"):
            self.assertNotIn(forbidden, pipeline)

    def test_csi_h264_pipeline_rejects_invalid_bridge_port(self):
        with self.assertRaisesRegex(MODULE.RTMPConfigError, "bridge port"):
            MODULE.build_gstreamer_launch({
                "native_h264": True, "backend": "csi", "tcp_port": 70000,
                "resolution": "1280x720", "fps": 30,
            })

    def test_native_h264_pipeline_rejects_invalid_camera_metadata(self):
        with self.assertRaisesRegex(MODULE.RTMPConfigError, "device path is invalid"):
            MODULE.build_gstreamer_launch({
                "native_h264": True,
                "device": "/tmp/not-a-camera",
                "resolution": "1280x720",
                "fps": "30",
            })

    def test_mjpeg_browser_stream_can_be_packetized_without_decoding(self):
        pipeline = MODULE.build_gstreamer_launch({"native_h264": False})
        self.assertIn("souphttpsrc", pipeline)
        self.assertIn("multipartdemux", pipeline)
        self.assertIn("jpegparse", pipeline)
        self.assertIn("rtpjpegpay", pipeline)
        self.assertNotIn("ffmpeg", pipeline)

    def test_runtime_check_requires_all_gstreamer_plugins(self):
        class Factory:
            @staticmethod
            def find(name):
                return object() if name != "h264parse" else None

        fake_gst = type("FakeGst", (), {"ElementFactory": Factory})
        with patch.object(MODULE, "_load_gstreamer", return_value=(None, fake_gst, None)):
            with self.assertRaisesRegex(MODULE.RTMPConfigError, "h264parse"):
                MODULE.check_gstreamer_runtime()

    def test_camera_marker_requires_a_live_capture_service_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "camera-status.json"
            marker.write_text(json.dumps({
                "pid": os.getpid(),
                "native_h264": True,
                "device": "/dev/video0",
                "resolution": "1280x720",
                "fps": "30",
            }), encoding="utf-8")
            self.assertTrue(MODULE.native_h264_available(marker_path=marker))
            marker.write_text(json.dumps({"pid": 99999999, "native_h264": True, "device": "/dev/video0"}))
            self.assertFalse(MODULE.native_h264_available(marker_path=marker))

    def test_disabled_output_exits_cleanly_without_starting_server(self):
        with tempfile.TemporaryDirectory() as directory:
            status_path = Path(directory) / "rtmp-status.json"
            with patch.object(MODULE, "load_config", return_value={"enabled": False}):
                with patch.object(MODULE, "STATUS_PATH", status_path):
                    with patch.object(MODULE, "native_h264_available") as detect:
                        with patch.object(MODULE, "_load_gstreamer") as load:
                            with patch("sys.argv", ["rtmp_output.py"]):
                                self.assertIsNone(MODULE.main())
            detect.assert_not_called()
            load.assert_not_called()
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "disabled")
            self.assertFalse(status["active"])



if __name__ == "__main__":
    unittest.main()
