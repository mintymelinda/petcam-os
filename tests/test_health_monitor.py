import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(
    "stage-petcam/00-install-files/files/usr/local/lib/petcam/health_monitor.py"
)
SPEC = importlib.util.spec_from_file_location("petcam_health_monitor", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class HealthMonitorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state_file = Path(self.directory.name) / "health.json"
        self.state_patch = mock.patch.object(MODULE, "STATE_FILE", self.state_file)
        self.state_patch.start()
        self.active = {"state": "active", "active_age": 120.0}
        self.services = mock.patch.object(
            MODULE, "service_snapshot", side_effect=lambda name: self.active
        )
        self.services.start()

    def tearDown(self):
        self.services.stop()
        self.state_patch.stop()
        self.directory.cleanup()

    def test_healthy_backend_and_public_proxy_clear_failure_streak(self):
        MODULE.save_state({"consecutive_failures": 3, "recovery_count": 4, "next_recovery_at": 900})
        with mock.patch.object(MODULE, "probe_stream", side_effect=[True, True]):
            report = MODULE.check_health(now=1000)
        self.assertEqual(report["state"], "healthy")
        self.assertTrue(report["backend_stream_ready"])
        self.assertTrue(report["public_stream_ready"])
        self.assertEqual(report["consecutive_failures"], 0)
        self.assertEqual(report["recovery_count"], 4)
        self.assertEqual(report["next_recovery_at"], 0)

    def test_unknown_systemd_state_does_not_trigger_recovery(self):
        with mock.patch.object(MODULE, "service_snapshot", return_value={"state": "unknown", "active_age": None}), \
                mock.patch.object(MODULE, "restart_service") as restart:
            report = MODULE.check_health(now=1000)
        self.assertEqual(report["state"], "service_state_unknown")
        restart.assert_not_called()

    def test_startup_grace_does_not_probe_or_restart(self):
        self.active = {"state": "active", "active_age": 5.0}
        with mock.patch.object(MODULE, "probe_stream") as probe, \
                mock.patch.object(MODULE, "restart_service") as restart:
            report = MODULE.check_health(now=1000)
        self.assertEqual(report["state"], "starting")
        probe.assert_not_called()
        restart.assert_not_called()

    def test_disconnected_camera_is_reported_without_restarting_service(self):
        with mock.patch.object(MODULE, "probe_stream", return_value=False), \
                mock.patch.object(MODULE, "camera_present", return_value=False), \
                mock.patch.object(MODULE, "restart_service") as restart:
            report = MODULE.check_health(now=1000)
        self.assertEqual(report["state"], "camera_disconnected")
        self.assertFalse(report["camera_connected"])
        restart.assert_not_called()

    def test_dead_capture_process_is_distinguished_from_stalled_stream(self):
        def snapshot(service):
            if service == "petcam-stream.service":
                return {"state": "inactive", "active_age": None}
            return self.active
        with mock.patch.object(MODULE, "service_snapshot", side_effect=snapshot), \
                mock.patch.object(MODULE, "camera_present", return_value=True), \
                mock.patch.object(MODULE, "probe_stream") as probe, \
                mock.patch.object(MODULE, "restart_service", return_value=(True, "")) as restart:
            report = MODULE.check_health(now=1000)
        self.assertEqual(report["state"], "recovering")
        self.assertEqual(report["reason"], "Capture service is not active.")
        probe.assert_not_called()
        restart.assert_called_once_with("petcam-stream.service")

    def test_live_but_stalled_backend_is_restarted(self):
        with mock.patch.object(MODULE, "probe_stream", return_value=False), \
                mock.patch.object(MODULE, "camera_present", return_value=True), \
                mock.patch.object(MODULE, "restart_service", return_value=(True, "")) as restart:
            report = MODULE.check_health(now=1000)
        self.assertEqual(report["state"], "recovering")
        self.assertEqual(report["recovery_target"], "petcam-stream.service")
        self.assertEqual(report["recovery_count"], 1)
        restart.assert_called_once_with("petcam-stream.service")

    def test_recovered_backend_and_public_stream_return_to_healthy(self):
        with mock.patch.object(MODULE, "probe_stream", side_effect=[False, True, True]), \
                mock.patch.object(MODULE, "camera_present", return_value=True), \
                mock.patch.object(MODULE, "restart_service", return_value=(True, "")):
            recovering = MODULE.check_health(now=1000)
            healthy = MODULE.check_health(now=1060)
        self.assertEqual(recovering["state"], "recovering")
        self.assertEqual(healthy["state"], "healthy")
        self.assertEqual(healthy["recovery_count"], 1)
        self.assertEqual(healthy["consecutive_failures"], 0)

    def test_healthy_backend_with_broken_public_proxy_restarts_only_web_service(self):
        with mock.patch.object(MODULE, "probe_stream", side_effect=[True, False]), \
                mock.patch.object(MODULE, "restart_service", return_value=(True, "")) as restart:
            report = MODULE.check_health(now=1000)
        self.assertEqual(report["state"], "recovering")
        self.assertEqual(report["recovery_target"], "petcam-web.service")
        restart.assert_called_once_with("petcam-web.service")

    def test_camera_discovery_failure_does_not_trigger_restart(self):
        with mock.patch.object(MODULE, "probe_stream", return_value=False), \
                mock.patch.object(MODULE, "camera_present", return_value=None), \
                mock.patch.object(MODULE, "restart_service") as restart:
            report = MODULE.check_health(now=1000)
        self.assertEqual(report["state"], "camera_detection_failed")
        restart.assert_not_called()

    def test_repeated_failures_observe_bounded_backoff(self):
        MODULE.save_state({
            "consecutive_failures": 2,
            "recovery_count": 2,
            "next_recovery_at": 2000,
        })
        with mock.patch.object(MODULE, "probe_stream", return_value=False), \
                mock.patch.object(MODULE, "camera_present", return_value=True), \
                mock.patch.object(MODULE, "restart_service") as restart:
            report = MODULE.check_health(now=1500)
        self.assertEqual(report["state"], "recovery_backoff")
        self.assertEqual(report["next_recovery_at"], 2000)
        self.assertEqual(report["consecutive_failures"], 3)
        restart.assert_not_called()


if __name__ == "__main__":
    unittest.main()
