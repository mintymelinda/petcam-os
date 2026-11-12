import importlib.util
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "petcam_web_manager",
    "stage-petcam/00-install-files/files/usr/local/lib/petcam/web_manager.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class WebManagerTests(unittest.TestCase):
    def test_management_page_is_dependency_free_and_has_camera_controls(self):
        self.assertIn("Refresh cameras", MODULE.PAGE)
        self.assertIn("Live preview", MODULE.PAGE)
        self.assertIn("Use selected camera and mode", MODULE.PAGE)
        self.assertIn("https://www.paypal.me/kimmynightmare", MODULE.PAGE)
        self.assertIn('rel="noopener noreferrer"', MODULE.PAGE)
        self.assertIn("/api/cameras", MODULE.PAGE)
        self.assertIn("/api/select", MODULE.PAGE)
        self.assertIn("Software updates", MODULE.PAGE)
        self.assertIn("Check for updates", MODULE.PAGE)
        self.assertIn("Install update", MODULE.PAGE)
        self.assertIn("/api/update/check", MODULE.PAGE)
        self.assertIn("/api/update/install", MODULE.PAGE)
        self.assertIn("/api/update/status", MODULE.PAGE)

    def test_update_check_uses_fixed_privileged_command(self):
        completed = mock.Mock(returncode=0, stdout='{"update_available": false}', stderr="")
        with mock.patch.object(MODULE.subprocess, "run", return_value=completed) as run:
            self.assertFalse(MODULE.update_check()["update_available"])
        self.assertEqual(run.call_args.args[0], ["sudo", "-n", "/usr/local/bin/petcam-update", "check"])

    def test_update_status_reports_active_state_and_recent_logs(self):
        outputs = [
            mock.Mock(returncode=0, stdout="active\nsuccess\n0\n", stderr=""),
            mock.Mock(returncode=0, stdout="Downloading release\n", stderr=""),
        ]
        with mock.patch.object(MODULE.subprocess, "run", side_effect=outputs):
            status = MODULE.update_status()
        self.assertTrue(status["active"])
        self.assertEqual(status["result"], "success")
        self.assertIn("Downloading release", status["logs"])

    def test_valid_selection_uses_transactional_config_apply(self):
        MODULE.camera_inventory = lambda: [{
            "kind": "usb",
            "device": "/dev/video0",
            "modes": [{"width": 640, "height": 480, "fps": [30], "format": "MJPG"}],
        }]
        try:
            with mock.patch.object(MODULE, "apply_config") as apply:
                MODULE.save_selection({
                    "kind": "usb",
                    "device": "/dev/video0",
                    "mode": {"width": 640, "height": 480, "fps": 30, "format": "MJPG"},
                })
            apply.assert_called_once_with({
                "SOURCE": "usb", "DEVICE": "/dev/video0", "CAMERA": "auto",
                "FORMAT": "MJPG", "RESOLUTION": "640x480", "FPS": "30",
            })
        finally:
            MODULE.camera_inventory = lambda: []

    def test_usb_selection_rejects_unadvertised_device(self):
        original = MODULE.camera_inventory
        MODULE.camera_inventory = lambda: []
        try:
            with self.assertRaisesRegex(ValueError, "no longer advertised"):
                MODULE.save_selection({
                    "kind": "usb", "device": "/dev/video0",
                    "mode": {"width": 640, "height": 480, "fps": 30, "format": "MJPG"},
                })
        finally:
            MODULE.camera_inventory = original

    def test_invalid_capture_values_are_rejected(self):
        for mode in (
            {"width": 0, "height": 480, "fps": 30},
            {"width": 640, "height": 480, "fps": 0},
            {"width": 640, "height": 480, "fps": 999},
        ):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                MODULE.save_selection({"kind": "csi", "camera": 0, "mode": mode})


if __name__ == "__main__":
    unittest.main()
