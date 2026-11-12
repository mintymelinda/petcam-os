import importlib.util
import json
import sys
import threading
import tempfile
import unittest
from pathlib import Path
from unittest import mock

LIB = Path("stage-petcam/00-install-files/files/usr/local/lib/petcam")
sys.path.insert(0, str(LIB))
import config as CONFIG_MODULE

SPEC = importlib.util.spec_from_file_location("petcam_camera_profiles", LIB / "camera_profiles.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CameraIdentityTests(unittest.TestCase):
    def test_usb_by_id_identity_is_stable_and_video_node_is_marked_unstable(self):
        stable = MODULE.camera_identity({
            "kind": "usb", "device": "/dev/v4l/by-id/usb-046d_C922-video-index0"
        })
        repeated = MODULE.camera_identity({
            "kind": "usb", "device": "/dev/v4l/by-id/usb-046d_C922-video-index0"
        })
        unstable = MODULE.camera_identity({"kind": "usb", "device": "/dev/video2"})
        self.assertEqual(stable["id"], repeated["id"])
        self.assertTrue(stable["stable_identity"])
        self.assertFalse(unstable["stable_identity"])
        self.assertNotEqual(stable["id"], unstable["id"])

    def test_usb_physical_topology_keeps_identity_when_video_node_changes(self):
        physical_path = "/sys/devices/platform/soc/usb1/1-1/1-1.2"
        first = MODULE.camera_identity({
            "kind": "usb", "device": "/dev/video0", "physical_path": physical_path
        })
        after_reenumeration = MODULE.camera_identity({
            "kind": "usb", "device": "/dev/video4", "physical_path": physical_path
        })
        self.assertEqual(first["id"], after_reenumeration["id"])
        self.assertTrue(first["stable_identity"])

    def test_csi_identity_falls_back_to_sensor_and_index_without_a_physical_path(self):
        first = MODULE.camera_identity({"kind": "csi", "sensor": "imx708", "camera": 0})
        second = MODULE.camera_identity({"kind": "csi", "sensor": "imx708", "camera": 1})
        self.assertEqual(first["id"], "csi-imx708-0")
        self.assertFalse(first["stable_identity"])
        self.assertNotEqual(first["id"], second["id"])

    def test_csi_physical_path_produces_a_stable_identity(self):
        camera = {
            "kind": "csi", "sensor": "imx708", "camera": 0,
            "device_path": "/base/soc/i2c0mux/i2c@0/imx708@1a",
        }
        first = MODULE.camera_identity(camera)
        second = MODULE.camera_identity({**camera, "camera": 1})
        self.assertTrue(first["stable_identity"])
        self.assertEqual(first["id"], second["id"])


    def test_usb_identity_is_stable_when_video_node_number_changes(self):
        first = MODULE.camera_identity({
            "kind": "usb",
            "device": "/dev/video0",
            "physical_path": "/sys/devices/platform/soc/usb1/1-1/1-1.2",
        })
        second = MODULE.camera_identity({
            "kind": "usb",
            "device": "/dev/video4",
            "physical_path": "/sys/devices/platform/soc/usb1/1-1/1-1.2",
        })
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(first["stable_identity"])

    def test_usb_profile_pins_to_physical_path_not_video_node(self):
        camera = {
            "kind": "usb",
            "device": "/dev/video0",
            "physical_path": "/sys/devices/platform/soc/usb1/1-1/1-1.2",
            "label": "USB camera",
        }
        discovered = {
            "device": "/dev/video4",
            "physical_path": camera["physical_path"],
            "modes": [{"format": "MJPG", "width": 640, "height": 480, "fps": [30]}],
        }
        with mock.patch.object(MODULE, "discover_devices", return_value=[discovered]):
            values, profile = MODULE._mode_selection(camera, {"format": "MJPG", "width": 640, "height": 480, "fps": 30})
        self.assertEqual(values["DEVICE"], "")
        self.assertEqual(values["USB_PHYSICAL_PATH"], camera["physical_path"])
        self.assertEqual(values["CSI_DEVICE_PATH"], "")

    def test_csi_profile_persists_physical_device_tree_path(self):
        camera = {
            "kind": "csi",
            "camera": 0,
            "sensor": "imx708",
            "device_path": "/base/soc/i2c0mux/i2c@1/imx708@1a",
            "label": "CSI camera",
        }
        discovered = {
            "index": 1,
            "sensor": "imx708",
            "device_path": camera["device_path"],
            "modes": [{"width": 640, "height": 480, "fps": 30.0}],
        }
        with mock.patch.object(MODULE, "discover_csi_cameras", return_value=[discovered]):
            values, profile = MODULE._mode_selection(camera, {"width": 640, "height": 480, "fps": 30})
        self.assertEqual(values["CAMERA"], "auto")
        self.assertEqual(values["CSI_DEVICE_PATH"], camera["device_path"])

class CameraProfileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.profile_dir = self.root / "cameras.d"
        self.index = self.root / "cameras.json"
        self.config = self.root / "petcam.conf"
        self.config.write_text(
            "SOURCE=usb\nDEVICE=/dev/video0\nCAMERA=auto\nFORMAT=MJPG\n"
            "RESOLUTION=640x480\nFPS=30\nCSI_QUALITY=70\nPORT=8081\n"
        )
        self.profile_dir.mkdir()
        self.patches = [
            mock.patch.object(MODULE, "PROFILE_DIR", self.profile_dir),
            mock.patch.object(MODULE, "INDEX_FILE", self.index),
            mock.patch.object(MODULE, "CONFIG_PATH", self.config),
            mock.patch.object(MODULE, "_default_stream_estimate", return_value={
                "id": "default", "kind": "usb", "device": "/dev/video0",
                "estimated_pixels_per_second": 9216000,
                "buffer_memory_bytes": 3686400, "usb_raw_mbps": 0.0,
            }),
            mock.patch.object(MODULE, "read_model", return_value="Raspberry Pi 4 Model B"),
            mock.patch.object(MODULE, "model_tier", return_value="pi4"),
            mock.patch.object(MODULE, "validate_config", side_effect=lambda values, check_hardware=False: CONFIG_MODULE.validate_config(values)),
            mock.patch.object(MODULE, "_available_memory_bytes", return_value=1024 * 1024 * 1024),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.directory.cleanup()

    def camera(self):
        return {
            "kind": "usb",
            "device": "/dev/v4l/by-id/usb-046d_C922-video-index0",
            "label": "USB · Logitech C922",
            "modes": [{"format": "MJPG", "width": 640, "height": 480, "fps": [30]}],
        }

    def mode(self):
        return {"format": "MJPG", "width": 640, "height": 480, "fps": 30}

    def test_profile_index_rejects_invalid_or_conflicting_ports_and_services(self):
        cases = [
            ([{"id": "usb-one", "port": 8081, "service": "petcam-stream@usb-one.service"}], "invalid stream port"),
            ([
                {"id": "usb-one", "port": 8082, "service": "petcam-stream@usb-one.service"},
                {"id": "usb-two", "port": 8082, "service": "petcam-stream@usb-two.service"},
            ], "duplicate stream port"),
            ([{"id": "usb-one", "port": 8082, "service": "petcam-stream@other.service"}], "invalid systemd service name"),
        ]
        for profiles, expected in cases:
            with self.subTest(profiles=profiles):
                self.index.write_text(json.dumps(profiles))
                with self.assertRaisesRegex(MODULE.ProfileError, expected):
                    MODULE._load_profiles()

    def test_creates_profile_with_separate_config_port_and_service(self):
        restart = mock.Mock()
        ready = mock.Mock(return_value=True)
        with mock.patch.object(MODULE, "discover_devices", return_value=[
            {"device": self.camera()["device"], "modes": self.camera()["modes"]}
        ]):
            profile = MODULE.create_profile(self.camera(), self.mode(), restart_service=restart, ready=ready)
        self.assertEqual(profile["port"], 8082)
        self.assertTrue(profile["stable_identity"])
        self.assertEqual(profile["service"], f"petcam-stream@{profile['id']}.service")
        self.assertTrue((self.profile_dir / f"{profile['id']}.conf").exists())
        saved = json.loads(self.index.read_text())
        self.assertEqual(saved[0]["id"], profile["id"])
        restart.assert_called_once_with("enable", profile["id"])
        ready.assert_called_once_with(8082)

    def test_refuses_to_duplicate_primary_or_existing_profile(self):
        with mock.patch.object(MODULE, "_default_stream_estimate", return_value={
            "id": "default", "kind": "usb", "device": self.camera()["device"],
            "estimated_pixels_per_second": 9216000,
            "buffer_memory_bytes": 3686400, "usb_raw_mbps": 0.0,
        }), mock.patch.object(MODULE, "discover_devices", return_value=[
            {"device": self.camera()["device"], "modes": self.camera()["modes"]}
        ]):
            with self.assertRaisesRegex(MODULE.ProfileError, "already assigned to the primary"):
                MODULE.create_profile(self.camera(), self.mode())

    def test_failed_stream_start_removes_partial_profile(self):
        restart = mock.Mock()
        with mock.patch.object(MODULE, "discover_devices", return_value=[
            {"device": self.camera()["device"], "modes": self.camera()["modes"]}
        ]):
            with self.assertRaisesRegex(MODULE.ProfileError, "did not produce"):
                MODULE.create_profile(self.camera(), self.mode(), restart_service=restart, ready=mock.Mock(return_value=False))
        self.assertEqual(json.loads(self.index.read_text()), [])
        self.assertEqual(list(self.profile_dir.iterdir()), [])
        self.assertEqual(restart.call_args_list[0], mock.call("enable", mock.ANY))
        self.assertEqual(restart.call_args_list[1], mock.call("disable", mock.ANY))

    def test_rejects_aggregate_pixel_budget_overrun(self):
        candidate = {
            "estimated_pixels_per_second": 200_000_000,
            "buffer_memory_bytes": 1_000_000,
            "usb_raw_mbps": 0,
        }
        with self.assertRaisesRegex(MODULE.ProfileError, "aggregate capture budget"):
            MODULE._check_resource_budget(candidate, [])

    def test_rejects_raw_usb_bandwidth_overrun(self):
        candidate = {
            "estimated_pixels_per_second": 1,
            "buffer_memory_bytes": 1,
            "usb_raw_mbps": 250,
        }
        with self.assertRaisesRegex(MODULE.ProfileError, "USB 2.0 budget"):
            MODULE._check_resource_budget(candidate, [])

    def test_configure_restarts_only_the_selected_camera_instance(self):
        profile = {
            "id": MODULE.camera_identity(self.camera())["id"],
            "port": 8082,
            "service": f"petcam-stream@{MODULE.camera_identity(self.camera())['id']}.service",
            "created_at": "2026-11-12T00:00:00+00:00",
        }
        MODULE._save_profiles([profile])
        restart = mock.Mock()
        with mock.patch.object(MODULE, "discover_devices", return_value=[
            {"device": self.camera()["device"], "modes": self.camera()["modes"]}
        ]), mock.patch.object(MODULE, "apply_config") as apply:
            updated = MODULE.configure_profile(profile["id"], self.camera(), self.mode(), restart_service=restart)
        self.assertEqual(updated["port"], 8082)
        self.assertEqual(apply.call_args.kwargs["path"], self.profile_dir / f"{profile['id']}.conf")
        apply.call_args.kwargs["restart"]()
        restart.assert_called_once_with("restart", profile["id"])

    def test_stream_port_keeps_default_url_and_routes_profiles_to_their_ports(self):
        with mock.patch.object(MODULE, "_load_profiles", return_value=[
            {"id": "usb-test", "port": 8082}
        ]), mock.patch.object(MODULE, "parse_config", return_value={"PORT": "8081"}):
            self.assertEqual(MODULE.stream_port("default"), 8081)
            self.assertEqual(MODULE.stream_port("usb-test"), 8082)
            with self.assertRaisesRegex(MODULE.ProfileError, "does not exist"):
                MODULE.stream_port("unknown")

    def test_primary_reconfiguration_cannot_overrun_existing_stream_budget(self):
        existing = [{
            "estimated_pixels_per_second": 10_000_000,
            "buffer_memory_bytes": 1_000_000,
            "usb_raw_mbps": 0.0,
        }]
        with mock.patch.object(MODULE, "validate_config", return_value={"SOURCE": "usb"}), \
                mock.patch.object(MODULE, "_default_stream_estimate", return_value={
                    "estimated_pixels_per_second": 120_000_000,
                    "buffer_memory_bytes": 1_000_000,
                    "usb_raw_mbps": 0.0,
                }), \
                mock.patch.object(MODULE, "_load_profiles", return_value=existing):
            with self.assertRaisesRegex(MODULE.ProfileError, "aggregate capture budget"):
                MODULE.validate_primary_change({"SOURCE": "usb"})

    def test_profile_manager_lock_serializes_concurrent_changes(self):
        acquired = threading.Event()
        def contender():
            with MODULE.profile_manager_guard():
                acquired.set()
        with MODULE.profile_manager_guard():
            thread = threading.Thread(target=contender)
            thread.start()
            self.assertFalse(acquired.wait(0.1))
        self.assertTrue(acquired.wait(2))
        thread.join(timeout=2)

    def test_removes_only_the_requested_profile(self):
        profile = {
            "id": "usb-test", "port": 8082, "service": "petcam-stream@usb-test.service",
            "estimated_pixels_per_second": 1,
        }
        MODULE._save_profiles([profile])
        (self.profile_dir / "usb-test.conf").write_text("SOURCE=usb\n")
        (self.profile_dir / "usb-test.conf.last-good").write_text("SOURCE=usb\n")
        restart = mock.Mock()
        MODULE.remove_profile("usb-test", restart_service=restart)
        self.assertEqual(json.loads(self.index.read_text()), [])
        self.assertFalse((self.profile_dir / "usb-test.conf").exists())
        self.assertFalse((self.profile_dir / "usb-test.conf.last-good").exists())
        restart.assert_called_once_with("disable", "usb-test")


if __name__ == "__main__":
    unittest.main()
