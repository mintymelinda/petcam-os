import importlib.util
import unittest

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
        self.assertIn("/api/cameras", MODULE.PAGE)
        self.assertIn("/api/select", MODULE.PAGE)

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
