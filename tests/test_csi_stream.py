import importlib.util
import unittest

SPEC = importlib.util.spec_from_file_location(
    "petcam_csi_stream",
    "stage-petcam/00-install-files/files/usr/local/lib/petcam/csi_stream.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CSIStreamTests(unittest.TestCase):
    def test_source_retains_camera_mode_for_picamera2_backend(self):
        source = MODULE.CSIFrameSource(1, 640, 480, 30, 70)
        self.assertEqual(source.camera, 1)
        self.assertEqual(source.width, 640)
        self.assertEqual(source.height, 480)
        self.assertEqual(source.fps, 30)
        self.assertFalse(source.stop_event.is_set())

    def test_hardware_h264_requires_confirmed_encoder_before_starting_capture(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        result = SimpleNamespace(returncode=1, stdout="{}")
        args = SimpleNamespace(host="127.0.0.1", tcp_port=8556, camera=0, fps=30)
        with patch.object(MODULE.subprocess, "run", return_value=result), \
                patch.object(MODULE.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(SystemExit, "refusing software encoding"):
                MODULE.run_hardware_h264(args, 1280, 720)
        popen.assert_not_called()

    def test_hardware_h264_launches_rpicam_encoder_and_cleans_marker(self):
        import json
        import os
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "camera-status.json"
            process = Mock()
            process.stdout.read.return_value = b""
            process.wait.return_value = 0
            process.poll.return_value = 0
            bridge = Mock()
            capability = SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"available": True, "device": "/dev/video11", "encoder": "bcm2835-codec-encode"}),
            )
            args = SimpleNamespace(host="127.0.0.1", tcp_port=8556, camera=0, fps=30)
            with patch.object(MODULE, "CAMERA_STATUS_PATH", marker), \
                    patch.object(MODULE.subprocess, "run", return_value=capability), \
                    patch.object(MODULE.subprocess, "Popen", return_value=process) as popen, \
                    patch.object(MODULE, "H264TCPBridge", return_value=bridge), \
                    patch.object(MODULE.signal, "signal"):
                MODULE.run_hardware_h264(args, 1280, 720)
            command = popen.call_args.args[0]
            self.assertIn("--codec", command)
            self.assertIn("h264", command)
            self.assertIn("--inline", command)
            self.assertFalse(marker.exists())
            bridge.start.assert_called_once()
            bridge.stop.assert_called_once()
            self.assertEqual(process.terminate.call_count, 0)

    def test_extracts_concatenated_jpeg_frames(self):
        first = b"\xff\xd8first\xff\xd9"
        second = b"\xff\xd8second\xff\xd9"
        buffer = bytearray(b"noise" + first + second)
        self.assertEqual(MODULE.extract_jpeg_frames(buffer), [first, second])
        self.assertEqual(buffer, bytearray())

    def test_preserves_partial_jpeg_until_end_marker_arrives(self):
        frame = b"\xff\xd8partial-frame\xff\xd9"
        buffer = bytearray(frame[:-2])
        self.assertEqual(MODULE.extract_jpeg_frames(buffer), [])
        buffer.extend(frame[-2:])
        self.assertEqual(MODULE.extract_jpeg_frames(buffer), [frame])
        self.assertEqual(buffer, bytearray())


if __name__ == "__main__":
    unittest.main()
