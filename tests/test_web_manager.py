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
        self.assertIn('role="tablist"', MODULE.PAGE)
        self.assertLess(MODULE.PAGE.index(">Live</button>"), MODULE.PAGE.index(">Camera Settings</button>"))
        self.assertLess(MODULE.PAGE.index(">Camera Settings</button>"), MODULE.PAGE.index(">System</button>"))
        self.assertNotIn(">OBS</button>", MODULE.PAGE)
        self.assertNotIn(">Streams</button>", MODULE.PAGE)
        self.assertIn('id="copy-browser-source" type="button">URL Browser Source</button>', MODULE.PAGE)
        self.assertIn('id="panel-camera-settings"', MODULE.PAGE)
        self.assertIn('id="panel-system"', MODULE.PAGE)
        self.assertIn("activateTab", MODULE.PAGE)
        self.assertIn("Apply selected camera and mode", MODULE.PAGE)
        self.assertIn('" · Available: "+control.menu.map(option=>option.label).join(", ")', MODULE.PAGE)
        self.assertIn('(control.min!==null&&control.max!==null?" · Range: "', MODULE.PAGE)
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
        self.assertNotIn("/api/streams", MODULE.PAGE)
        self.assertNotIn("stream_path", MODULE.PAGE)
        self.assertNotIn("OBS browser source", MODULE.PAGE)
        self.assertIn("Network and OBS diagnostics", MODULE.PAGE)
        self.assertIn("/api/diagnostics", MODULE.PAGE)
        self.assertIn("/api/support-bundle", MODULE.PAGE)
        self.assertIn("Download redacted support bundle", MODULE.PAGE)
        self.assertNotIn("change-password", MODULE.PAGE)
        self.assertNotIn("/api/password", MODULE.PAGE)

    def test_admin_reauthentication_uses_masked_password_dialog_not_prompt(self):
        self.assertIn('<dialog id="login-dialog"', MODULE.PAGE)
        self.assertIn('id="login-password" type="password"', MODULE.PAGE)
        self.assertIn('autocomplete="current-password"', MODULE.PAGE)
        self.assertIn('dialog.showModal()', MODULE.PAGE)
        self.assertNotIn('window.prompt("Enter your PetCam manager password")', MODULE.PAGE)

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

    def test_update_install_uses_in_page_confirmation_not_native_dialogs(self):
        self.assertIn('id="update-confirmation" hidden', MODULE.PAGE)
        self.assertIn('id="cancel-update-install"', MODULE.PAGE)
        self.assertIn('id="confirm-update-install"', MODULE.PAGE)
        self.assertIn('$("update-confirmation").hidden=false', MODULE.PAGE)
        self.assertIn('$("confirm-update-install").onclick=installUpdate', MODULE.PAGE)
        self.assertNotIn('confirm("Install the signed PetCam application update now?"', MODULE.PAGE)

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
            with mock.patch.object(MODULE, "apply_config") as apply:
                MODULE.save_selection({
                    "kind": "usb",
                    "device": "/dev/video0",
                    "mode": {"width": 640, "height": 480, "fps": 30, "format": "MJPG"},
                })
            apply.assert_called_once_with({
                "SOURCE": "usb", "DEVICE": "/dev/video0",
                "USB_PHYSICAL_PATH": "", "CSI_DEVICE_PATH": "", "CAMERA": "auto",
                "FORMAT": "MJPG", "RESOLUTION": "640x480", "FPS": "30",
            })
        finally:
            MODULE.camera_inventory = original

    def test_performance_override_is_forwarded_to_primary_configuration(self):
        camera = {
            "kind": "usb", "id": "usb-fast", "device": "/dev/video0",
            "modes": [{"width": 1920, "height": 1080, "fps": [60], "format": "MJPG"}],
        }
        with mock.patch.object(MODULE, "camera_inventory", return_value=[camera]), \
                mock.patch.object(MODULE, "model_tier", return_value="pi3"), \
                mock.patch.object(MODULE, "PIXEL_BUDGETS", {"pi3": 1}), \
                mock.patch.object(MODULE, "apply_config") as apply:
            MODULE.save_selection({
                "kind": "usb", "camera_id": "usb-fast",
                "mode": {"width": 1920, "height": 1080, "fps": 60, "format": "MJPG"},
                "allow_performance_override": True,
            })
        self.assertEqual(apply.call_args.args[0]["FPS"], "60")

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
                mock.patch.object(MODULE, "apply_config") as apply:
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
                mock.patch.object(MODULE, "apply_config") as apply:
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

    def test_camera_inventory_cache_reuses_recent_discovery_and_returns_copies(self):
        camera = {"kind": "usb", "device": "/dev/video0", "modes": []}
        MODULE._camera_inventory_cache = None
        MODULE._camera_inventory_cache_at = 0.0
        try:
            with mock.patch.object(MODULE, "camera_inventory", return_value=[camera]) as discover:
                first = MODULE.cached_camera_inventory()
                first[0]["device"] = "/dev/changed"
                second = MODULE.cached_camera_inventory()
            discover.assert_called_once_with()
            self.assertEqual(second[0]["device"], "/dev/video0")
        finally:
            MODULE._camera_inventory_cache = None
            MODULE._camera_inventory_cache_at = 0.0

    def test_stream_proxy_uses_incremental_reads_without_explicit_flushes(self):
        class Upstream:
            status = 200
            headers = {"Content-Type": "multipart/x-mixed-replace; boundary=frame"}

            def __init__(self):
                self.chunks = iter((b"frame-one", b"frame-two", b""))
                self.read1_calls = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read1(self, size):
                self.read1_calls += 1
                return next(self.chunks)

            def read(self, _size):
                raise AssertionError("proxy should prefer incremental read1")

        class CountingWriter(io.BytesIO):
            flush_calls = 0

            def flush(self):
                self.flush_calls += 1
                super().flush()

        class Handler:
            def __init__(self):
                self.wfile = CountingWriter()
                self.headers_sent = []

            def send_response(self, status):
                self.status = status

            def send_header(self, name, value):
                self.headers_sent.append((name, value))

            def end_headers(self):
                pass

        upstream = Upstream()
        handler = Handler()
        with mock.patch.object(MODULE.urllib.request, "urlopen", return_value=upstream):
            MODULE.proxy_stream(handler, 8081)
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.wfile.getvalue(), b"frame-oneframe-two")
        self.assertEqual(upstream.read1_calls, 3)
        self.assertEqual(handler.wfile.flush_calls, 0)

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

    def test_camera_page_restores_saved_camera_and_mode_and_refreshes_preview(self):
        self.assertIn("activeCameraMatches", MODULE.PAGE)
        self.assertIn("activeConfig", MODULE.PAGE)
        self.assertIn("refreshLivePreview", MODULE.PAGE)
        self.assertIn('id="live-preview"', MODULE.PAGE)
        self.assertIn("saved camera settings are selected", MODULE.PAGE)

    def test_camera_page_displays_measured_capture_rate_separately_from_configured_fps(self):
        self.assertIn("Actual captured rate:", MODULE.PAGE)
        self.assertIn("latest_captured_fps", MODULE.PAGE)
        self.assertIn("configured:", MODULE.PAGE)
        self.assertIn("actual ", MODULE.PAGE)

    def test_camera_performance_warning_uses_single_capture_budget(self):
        self.assertIn("projectedPixelRate", MODULE.PAGE)
        self.assertIn("hardware_budget_pixels_per_second", MODULE.PAGE)
        self.assertIn("exceedsPerformanceBudget", MODULE.PAGE)
        self.assertIn("pixels/second", MODULE.PAGE)
        self.assertNotIn("aggregate pixels/second", MODULE.PAGE)
        self.assertNotIn("primary_estimated_pixels_per_second", MODULE.PAGE)

    def test_active_camera_config_returns_only_selection_fields(self):
        config = {
            "SOURCE": "usb", "DEVICE": "", "USB_PHYSICAL_PATH": "/sys/devices/usb1/1-1",
            "CSI_DEVICE_PATH": "", "CAMERA": "auto", "FORMAT": "MJPG",
            "RESOLUTION": "1280x720", "FPS": "30", "PORT": "8081", "CSI_QUALITY": "70",
        }
        with mock.patch.object(MODULE, "parse_config", return_value=config):
            active = MODULE.active_camera_config()
        self.assertEqual(active["RESOLUTION"], "1280x720")
        self.assertEqual(active["FPS"], "30")
        self.assertNotIn("PORT", active)
        self.assertNotIn("CSI_QUALITY", active)

    def test_live_capture_status_reads_ustreamer_metrics_directly(self):
        payload = {
            "ok": True,
            "result": {
                "source": {
                    "captured_fps": 15,
                    "resolution": {"width": 1280, "height": 720},
                },
                "encoder": {"type": "HW"},
            },
        }

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(payload).encode()

        with mock.patch.object(MODULE, "parse_config", return_value={"PORT": "8081"}), \
                mock.patch.object(MODULE.urllib.request, "urlopen", return_value=Response()) as open_url, \
                mock.patch.object(MODULE.subprocess, "run") as run:
            actual = MODULE.live_capture_status()

        open_url.assert_called_once_with("http://127.0.0.1:8081/state", timeout=2)
        run.assert_not_called()
        self.assertEqual(actual, {
            "latest_captured_fps": 15,
            "active_resolution": "1280x720",
            "active_capture_format": "MJPEG",
        })

    def test_live_capture_status_falls_back_to_csi_metrics(self):
        payload = {
            "backend": "csi",
            "captured_fps_average": 29.5,
        }

        class Response:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(self.body).encode()

        with mock.patch.object(MODULE, "parse_config", return_value={"PORT": "8081", "RESOLUTION": "1920x1080"}), \
                mock.patch.object(
                    MODULE.urllib.request,
                    "urlopen",
                    side_effect=[Response({"ok": False}), Response(payload)],
                ) as open_url:
            actual = MODULE.live_capture_status()

        self.assertEqual(open_url.call_count, 2)
        self.assertEqual(actual, {
            "latest_captured_fps": 29.5,
            "active_resolution": "1920x1080",
            "active_capture_format": "MJPEG",
        })

    def test_live_capture_status_returns_empty_when_backend_metrics_are_unavailable(self):
        with mock.patch.object(MODULE, "parse_config", return_value={"PORT": "8081"}), \
                mock.patch.object(MODULE.urllib.request, "urlopen", side_effect=OSError("offline")):
            self.assertEqual(MODULE.live_capture_status(), {})

    def test_invalid_capture_values_are_rejected(self):
        for mode in (
            {"width": 0, "height": 480, "fps": 30},
            {"width": 640, "height": 480, "fps": 0},
            {"width": 640, "height": 480, "fps": 999},
        ):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                MODULE.save_selection({"kind": "csi", "camera": 0, "mode": mode})


class CameraControlTests(unittest.TestCase):
    SAMPLE_CONTROLS = """brightness 0x00980900 (int) : min=0 max=255 step=1 default=128 value=128 flags=has-min-max
white_balance_automatic 0x0098090c (bool) : default=1 value=1
power_line_frequency 0x00980918 (menu) : min=0 max=2 default=2 value=2 (60 Hz)
        0: Disabled
        1: 50 Hz
        2: 60 Hz
auto_exposure 0x009a0901 (menu) : min=0 max=3 default=0 value=3 (Aperture Priority Mode)
        1: Manual Mode
        3: Aperture Priority Mode
exposure_time_absolute 0x009a0902 (int) : min=3 max=2047 step=1 default=250 value=250 flags=inactive, has-min-max
exposure_dynamic_framerate 0x009a0903 (bool) : default=0 value=1
"""

    def test_parser_discovers_integer_boolean_and_menu_controls(self):
        controls = MODULE.parse_v4l2_controls(self.SAMPLE_CONTROLS)
        by_name = {control["name"]: control for control in controls}
        self.assertEqual(by_name["brightness"]["min"], 0)
        self.assertEqual(by_name["brightness"]["max"], 255)
        self.assertTrue(by_name["brightness"]["writable"])
        self.assertEqual(by_name["white_balance_automatic"]["type"], "bool")
        self.assertEqual(by_name["power_line_frequency"]["menu"], [
            {"value": 0, "label": "Disabled"},
            {"value": 1, "label": "50 Hz"},
            {"value": 2, "label": "60 Hz"},
        ])
        self.assertEqual(by_name["auto_exposure"]["value"], 3)
        self.assertFalse(by_name["exposure_time_absolute"]["writable"])
        self.assertTrue(by_name["exposure_dynamic_framerate"]["writable"])

    def test_active_usb_camera_is_resolved_from_saved_physical_identity(self):
        camera = {
            "kind": "usb",
            "device": "/dev/video4",
            "physical_path": "/sys/devices/usb1/1-1.2",
        }
        config = {
            "SOURCE": "usb",
            "DEVICE": "",
            "USB_PHYSICAL_PATH": "/sys/devices/usb1/1-1.2",
        }
        with mock.patch.object(MODULE, "active_camera_config", return_value=config), \
                mock.patch.object(MODULE, "camera_inventory", return_value=[camera]):
            self.assertEqual(MODULE.active_usb_camera(), camera)

    def test_camera_controls_are_unavailable_for_non_usb_active_source(self):
        with mock.patch.object(MODULE, "active_camera_config", return_value={"SOURCE": "csi"}):
            result = MODULE.read_camera_controls()
        self.assertFalse(result["supported"])
        self.assertEqual(result["controls"], [])

    def test_read_camera_controls_uses_only_discovered_device(self):
        camera = {"device": "/dev/video4", "label": "USB camera"}
        completed = mock.Mock(returncode=0, stdout=self.SAMPLE_CONTROLS, stderr="")
        with mock.patch.object(MODULE.subprocess, "run", return_value=completed) as run:
            result = MODULE.read_camera_controls(camera)
        self.assertTrue(result["supported"])
        self.assertEqual(result["device"], "/dev/video4")
        self.assertEqual(len(result["controls"]), 6)
        self.assertEqual(run.call_args.args[0], [
            "/usr/bin/v4l2-ctl", "--device", "/dev/video4", "--list-ctrls-menus",
        ])

    def test_read_camera_controls_rejects_untrusted_device_paths(self):
        with self.assertRaisesRegex(ValueError, "invalid V4L2 device path"):
            MODULE.read_camera_controls({"device": "/tmp/fake-video"})

    def test_setting_integer_control_uses_validated_v4l2_command(self):
        camera = {"device": "/dev/video4"}
        control = {
            "name": "brightness", "type": "int", "min": 0, "max": 255,
            "step": 1, "menu": [], "writable": True,
        }
        snapshot = {"supported": True, "controls": [control]}
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(MODULE, "active_usb_camera", return_value=camera), \
                mock.patch.object(MODULE, "read_camera_controls", side_effect=[snapshot, snapshot]), \
                mock.patch.object(MODULE.subprocess, "run", return_value=completed) as run:
            result = MODULE.set_camera_control({"name": "brightness", "value": 200})
        self.assertEqual(result, snapshot)
        self.assertEqual(run.call_args.args[0], [
            "/usr/bin/v4l2-ctl", "--device", "/dev/video4", "--set-ctrl=brightness=200",
        ])

    def test_setting_boolean_control_accepts_json_boolean(self):
        camera = {"device": "/dev/video4"}
        control = {
            "name": "exposure_dynamic_framerate", "type": "bool",
            "min": None, "max": None, "step": 1, "menu": [], "writable": True,
        }
        snapshot = {"supported": True, "controls": [control]}
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(MODULE, "active_usb_camera", return_value=camera), \
                mock.patch.object(MODULE, "read_camera_controls", side_effect=[snapshot, snapshot]), \
                mock.patch.object(MODULE.subprocess, "run", return_value=completed) as run:
            MODULE.set_camera_control({"name": "exposure_dynamic_framerate", "value": False})
        self.assertTrue(run.call_args.args[0][-1].endswith("=0"))

    def test_setting_menu_control_rejects_unadvertised_values(self):
        camera = {"device": "/dev/video4"}
        control = {
            "name": "power_line_frequency", "type": "menu", "min": 0, "max": 2,
            "step": 1, "menu": [{"value": 0, "label": "Disabled"}], "writable": True,
        }
        with mock.patch.object(MODULE, "active_usb_camera", return_value=camera), \
                mock.patch.object(MODULE, "read_camera_controls", return_value={
                    "supported": True, "controls": [control],
                }), \
                mock.patch.object(MODULE.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "not an advertised menu option"):
                MODULE.set_camera_control({"name": "power_line_frequency", "value": 2})
        run.assert_not_called()

    def test_setting_control_rejects_out_of_range_and_inactive_values(self):
        camera = {"device": "/dev/video4"}
        control = {
            "name": "brightness", "type": "int", "min": 0, "max": 255,
            "step": 1, "menu": [], "writable": True,
        }
        with mock.patch.object(MODULE, "active_usb_camera", return_value=camera), \
                mock.patch.object(MODULE, "read_camera_controls", return_value={
                    "supported": True, "controls": [control],
                }), \
                mock.patch.object(MODULE.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "above the supported maximum"):
                MODULE.set_camera_control({"name": "brightness", "value": 300})
        run.assert_not_called()
        control["writable"] = False
        with mock.patch.object(MODULE, "active_usb_camera", return_value=camera), \
                mock.patch.object(MODULE, "read_camera_controls", return_value={
                    "supported": True, "controls": [control],
                }), \
                mock.patch.object(MODULE.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "inactive or read-only"):
                MODULE.set_camera_control({"name": "brightness", "value": 100})
        run.assert_not_called()

    def test_management_page_exposes_dynamic_camera_controls(self):
        self.assertIn('id="camera-controls-list"', MODULE.PAGE)
        self.assertIn("/api/camera-controls", MODULE.PAGE)
        self.assertIn("Dynamic framerate", MODULE.PAGE)
        self.assertIn("actual captured FPS", MODULE.PAGE)

    def test_all_camera_control_editor_types_are_added_to_the_editor(self):
        self.assertIn('let input,output;', MODULE.PAGE)
        self.assertIn('if(output)editor.append(input,output);else editor.append(input);', MODULE.PAGE)
        self.assertIn('input.type="checkbox"', MODULE.PAGE)
        self.assertIn('input.type="range"', MODULE.PAGE)
        self.assertIn('input.type="number"', MODULE.PAGE)
        self.assertIn('document.createElement("select")', MODULE.PAGE)


class CameraIdentityTests(unittest.TestCase):
    def test_usb_camera_identity_prefers_physical_path(self):
        camera = {"kind": "usb", "device": "/dev/video4", "physical_path": "/sys/devices/usb1/1-1"}
        identity = MODULE.camera_identity(camera)
        self.assertTrue(identity["stable_identity"])
        self.assertEqual(identity["id"], MODULE.camera_identity({
            **camera, "device": "/dev/video7",
        })["id"])

    def test_csi_camera_identity_uses_device_tree_path_when_available(self):
        camera = {"kind": "csi", "camera": 0, "sensor": "IMX219", "device_path": "/sys/firmware/devicetree/base/cam0"}
        identity = MODULE.camera_identity(camera)
        self.assertTrue(identity["stable_identity"])
        self.assertTrue(identity["id"].startswith("csi-"))


if __name__ == "__main__":
    unittest.main()
