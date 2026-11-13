import importlib.util
import io
import json
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
        self.assertIn("Apply selected camera and mode", MODULE.PAGE)
        self.assertIn("above device estimate", MODULE.PAGE)
        self.assertIn("Use this mode anyway?", MODULE.PAGE)
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
        self.assertIn("Configure stream", MODULE.PAGE)
        self.assertIn("Add independent stream", MODULE.PAGE)
        self.assertIn("Camera streams", MODULE.PAGE)
        self.assertIn("/api/streams/configure", MODULE.PAGE)
        self.assertIn("/api/streams/remove", MODULE.PAGE)
        self.assertIn("stream_path", MODULE.PAGE)
        self.assertIn("Network and OBS diagnostics", MODULE.PAGE)
        self.assertIn("/api/diagnostics", MODULE.PAGE)
        self.assertIn("/api/support-bundle", MODULE.PAGE)
        self.assertIn("Download redacted support bundle", MODULE.PAGE)
        self.assertNotIn("change-password", MODULE.PAGE)
        self.assertNotIn("/api/password", MODULE.PAGE)

    def test_first_run_admin_setup_is_available_in_the_browser(self):
        self.assertIn('id="first-run-form"', MODULE.PAGE)
        self.assertIn('id="setup-password"', MODULE.PAGE)
        self.assertIn("/api/setup/status", MODULE.PAGE)
        self.assertIn("/api/setup", MODULE.PAGE)
        self.assertIn("SSH is not required", MODULE.PAGE)
        self.assertIn("initialize_password", MODULE.Handler.do_POST.__code__.co_names)
        self.assertIn("is_configured", MODULE.Handler.do_GET.__code__.co_names)

    def test_first_run_setup_endpoint_creates_session_and_cookie(self):
        payload = json.dumps({
            "password": "a sufficiently long password",
            "password_confirmation": "a sufficiently long password",
        }).encode()
        class Request:
            path = "/api/setup"
            headers = {
                "Origin": "https://petcam.local:8443",
                "Host": "petcam.local:8443",
                "Content-Length": str(len(payload)),
            }
            rfile = io.BytesIO(payload)
            response = None

            def send_json(self, status, body, headers=None):
                self.response = (status, body, headers or {})

        request = Request()
        with mock.patch.object(MODULE, "is_configured", return_value=False), \
                mock.patch.object(MODULE, "initialize_password") as initialize, \
                mock.patch.object(MODULE, "new_session", return_value=("session-token", "csrf-token")):
            MODULE.Handler.do_POST(request)
        initialize.assert_called_once_with("a sufficiently long password")
        self.assertEqual(request.response[0], 201)
        self.assertEqual(request.response[1]["csrf_token"], "csrf-token")
        self.assertIn("HttpOnly; Secure; SameSite=Strict", request.response[2]["Set-Cookie"])

    def test_first_run_setup_endpoint_rejects_non_same_origin_request(self):
        class Request:
            path = "/api/setup"
            headers = {"Origin": "http://attacker.example", "Host": "petcam.local:8443"}
            response = None

            def send_json(self, status, body, headers=None):
                self.response = (status, body, headers or {})

        request = Request()
        with mock.patch.object(MODULE, "is_configured", return_value=False), \
                mock.patch.object(MODULE, "initialize_password") as initialize:
            MODULE.Handler.do_POST(request)
        self.assertEqual(request.response[0], 403)
        initialize.assert_not_called()

    def test_first_run_setup_endpoint_cannot_reconfigure_existing_device(self):
        class Request:
            path = "/api/setup"
            headers = {}
            response = None

            def send_json(self, status, body, headers=None):
                self.response = (status, body, headers or {})

        request = Request()
        with mock.patch.object(MODULE, "is_configured", return_value=True), \
                mock.patch.object(MODULE, "initialize_password") as initialize:
            MODULE.Handler.do_POST(request)
        self.assertEqual(request.response[0], 409)
        initialize.assert_not_called()

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
        original = MODULE.camera_inventory
        MODULE.camera_inventory = lambda: [{
            "kind": "usb",
            "device": "/dev/video0",
            "modes": [{"width": 640, "height": 480, "fps": [30], "format": "MJPG"}],
        }]
        try:
            with mock.patch.object(MODULE, "apply_primary_config") as apply:
                MODULE.save_selection({
                    "kind": "usb",
                    "device": "/dev/video0",
                    "mode": {"width": 640, "height": 480, "fps": 30, "format": "MJPG"},
                })
            apply.assert_called_once_with(
                {
                    "SOURCE": "usb", "DEVICE": "/dev/video0",
                    "USB_PHYSICAL_PATH": "", "CSI_DEVICE_PATH": "", "CAMERA": "auto",
                    "FORMAT": "MJPG", "RESOLUTION": "640x480", "FPS": "30",
                },
                {
                    "kind": "usb", "device": "/dev/video0",
                    "modes": [{"width": 640, "height": 480, "fps": [30], "format": "MJPG"}],
                },
                allow_performance_override=False,
            )
        finally:
            MODULE.camera_inventory = original

    def test_performance_override_is_forwarded_to_primary_configuration(self):
        camera = {
            "kind": "usb", "id": "usb-fast", "device": "/dev/video0",
            "modes": [{"width": 1920, "height": 1080, "fps": [60], "format": "MJPG"}],
        }
        with mock.patch.object(MODULE, "camera_inventory", return_value=[camera]), \
                mock.patch.object(MODULE, "apply_primary_config") as apply:
            MODULE.save_selection({
                "kind": "usb", "camera_id": "usb-fast",
                "mode": {"width": 1920, "height": 1080, "fps": 60, "format": "MJPG"},
                "allow_performance_override": True,
            })
        self.assertTrue(apply.call_args.kwargs["allow_performance_override"])

    def test_primary_usb_selection_persists_physical_topology(self):
        physical_path = "/sys/devices/platform/soc/usb1/1-1/1-1.2"
        camera = {
            "kind": "usb",
            "id": "usb-stable",
            "device": "/dev/video4",
            "physical_path": physical_path,
            "modes": [{"width": 640, "height": 480, "fps": [30], "format": "MJPG"}],
        }
        with mock.patch.object(MODULE, "camera_inventory", return_value=[camera]), \
                mock.patch.object(MODULE, "apply_primary_config") as apply:
            MODULE.save_selection({
                "kind": "usb",
                "camera_id": "usb-stable",
                "device": "/dev/video0",
                "physical_path": physical_path,
                "mode": {"width": 640, "height": 480, "fps": 30, "format": "MJPG"},
            })
        values = apply.call_args.args[0]
        self.assertEqual(values["DEVICE"], "")
        self.assertEqual(values["USB_PHYSICAL_PATH"], physical_path)

    def test_primary_csi_selection_persists_device_tree_path(self):
        device_path = "/base/soc/i2c0mux/i2c@1/imx708@1a"
        camera = {
            "kind": "csi",
            "id": "csi-stable",
            "camera": 1,
            "sensor": "imx708",
            "device_path": device_path,
            "modes": [{"width": 640, "height": 480, "fps": 30}],
        }
        with mock.patch.object(MODULE, "camera_inventory", return_value=[camera]), \
                mock.patch.object(MODULE, "apply_primary_config") as apply:
            MODULE.save_selection({
                "kind": "csi",
                "camera_id": "csi-stable",
                "camera": 0,
                "device_path": device_path,
                "mode": {"width": 640, "height": 480, "fps": 30},
            })
        values = apply.call_args.args[0]
        self.assertEqual(values["CAMERA"], "auto")
        self.assertEqual(values["CSI_DEVICE_PATH"], device_path)

    def test_usb_selection_rejects_unadvertised_device(self):
        original = MODULE.camera_inventory
        MODULE.camera_inventory = lambda: []
        try:
            with self.assertRaisesRegex(ValueError, "no longer available"):
                MODULE.save_selection({
                    "kind": "usb", "device": "/dev/video0",
                    "mode": {"width": 640, "height": 480, "fps": 30, "format": "MJPG"},
                })
        finally:
            MODULE.camera_inventory = original

    def test_camera_inventory_exposes_stable_identity_metadata(self):
        with mock.patch.object(MODULE, "run_camera", return_value={
            "usb_devices": [{"device": "/dev/v4l/by-id/usb-camera", "modes": []}],
            "csi_cameras": [{"index": 0, "sensor": "imx708", "modes": []}],
        }):
            cameras = MODULE.camera_inventory()
        self.assertEqual(len(cameras), 2)
        self.assertTrue(cameras[0]["stable_identity"])
        self.assertEqual(cameras[1]["id"], "csi-imx708-0")
        self.assertIn(cameras[0]["hardware_tier"], MODULE.PIXEL_BUDGETS)
        self.assertEqual(
            cameras[0]["hardware_budget_pixels_per_second"],
            MODULE.PIXEL_BUDGETS[cameras[0]["hardware_tier"]],
        )
        self.assertEqual(
            cameras[1]["hardware_budget_pixels_per_second"],
            cameras[0]["hardware_budget_pixels_per_second"],
        )

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
