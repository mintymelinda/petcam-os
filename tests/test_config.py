import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

MODULE_PATH = Path(
    "stage-petcam/00-install-files/files/usr/local/lib/petcam/config.py"
)
SPEC = importlib.util.spec_from_file_location("petcam_config", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ConfigValidationTests(unittest.TestCase):
    def test_existing_default_config_is_compatible(self):
        values = {
            "SOURCE": "auto",
            "DEVICE": "",
            "CAMERA": "auto",
            "FORMAT": "auto",
            "RESOLUTION": "auto",
            "FPS": "auto",
            "CSI_QUALITY": "70",
            "PORT": "8081",
        }
        self.assertEqual(MODULE.validate_config(values), values)

    def test_parser_accepts_comments_and_existing_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "petcam.conf"
            path.write_text(
                "# Existing PetCam settings\nSOURCE=auto\nDEVICE=\nCAMERA=auto\n"
                "FORMAT=auto\nRESOLUTION=auto\nFPS=auto\nCSI_QUALITY=70\nPORT=8081\n"
            )
            self.assertEqual(MODULE.parse_config(path)["PORT"], "8081")

    def test_cli_apply_uses_transactional_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate_path = Path(directory) / "candidate.conf"
            candidate_path.write_text("SOURCE=csi\nCAMERA=auto\nFORMAT=auto\nFPS=30.0\n")
            with mock.patch.object(MODULE.sys, "argv", ["petcam-config", "apply", str(candidate_path)]), \
                    mock.patch.object(MODULE, "apply_config", return_value={"SOURCE": "csi"}) as apply, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(MODULE.main(), 0)
            apply.assert_called_once_with(MODULE.parse_config(candidate_path, check_hardware=True))

    def test_parser_rejects_missing_candidate_file(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.conf"
            with self.assertRaisesRegex(MODULE.ConfigError, "does not exist"):
                MODULE.parse_config(missing)

    def test_parser_rejects_malformed_duplicate_and_unknown_keys(self):
        cases = [
            ("SOURCE=auto\nnot-a-setting\n", "expected KEY=VALUE"),
            ("SOURCE=auto\nSOURCE=usb\n", "duplicate configuration key"),
            ("SOURCE=auto\nUNRECOGNIZED=yes\n", "unknown configuration key"),
            ("SOURCE=$(touch /tmp/pwned)\n", "shell syntax is not allowed"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "petcam.conf"
            for content, expected in cases:
                with self.subTest(content=content):
                    path.write_text(content)
                    with self.assertRaisesRegex(MODULE.ConfigError, expected):
                        MODULE.parse_config(path)

    def test_rejects_invalid_enums_ranges_and_reserved_ports(self):
        invalid = [
            ({"SOURCE": "network"}, "SOURCE must be"),
            ({"FORMAT": "H264"}, "FORMAT must be"),
            ({"RESOLUTION": "0x720"}, "dimensions"),
            ({"FPS": "0"}, "FPS must be greater"),
            ({"FPS": "241"}, "FPS must be greater"),
            ({"FPS": "29.97"}, "whole number"),
            ({"CSI_QUALITY": "101"}, "CSI_QUALITY"),
            ({"PORT": "8080"}, "reserved"),
            ({"PORT": "80"}, "PORT must be"),
        ]
        for updates, expected in invalid:
            with self.subTest(updates=updates):
                with self.assertRaisesRegex(MODULE.ConfigError, expected):
                    MODULE.validate_config({**MODULE.DEFAULTS, **updates})

    def test_csi_allows_fractional_fps_and_usb_requires_whole_fps(self):
        csi = MODULE.validate_config({**MODULE.DEFAULTS, "SOURCE": "csi", "FPS": "56.03"})
        self.assertEqual(csi["FPS"], "56.03")
        usb = MODULE.validate_config({**MODULE.DEFAULTS, "SOURCE": "usb", "FPS": "30.0"})
        self.assertEqual(usb["FPS"], "30")
        with self.assertRaisesRegex(MODULE.ConfigError, "whole number"):
            MODULE.validate_config({**MODULE.DEFAULTS, "SOURCE": "usb", "FPS": "29.97"})

    def test_unavailable_device_is_rejected_before_apply(self):
        values = {**MODULE.DEFAULTS, "SOURCE": "usb", "DEVICE": "/dev/video99"}
        with mock.patch.object(MODULE.Path, "exists", return_value=False):
            with self.assertRaisesRegex(MODULE.ConfigError, "unavailable"):
                MODULE.validate_config(values, check_hardware=True)

    def test_explicit_csi_camera_must_be_discovered(self):
        values = {**MODULE.DEFAULTS, "SOURCE": "csi", "CAMERA": "1"}
        result = SimpleNamespace(returncode=0, stdout=json.dumps({"csi_cameras": [{"index": 1}]}), stderr="")
        with mock.patch.object(MODULE.subprocess, "run", return_value=result):
            self.assertEqual(MODULE.validate_config(values, check_hardware=True)["CAMERA"], "1")
        result.stdout = json.dumps({"csi_cameras": [{"index": 0}]})
        with mock.patch.object(MODULE.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(MODULE.ConfigError, "not available"):
                MODULE.validate_config(values, check_hardware=True)


class TransactionalApplyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = self.root / "petcam.conf"
        self.last_good = self.root / "petcam.conf.last-good"
        self.original = (
            b"# existing settings\nSOURCE=auto\nDEVICE=\nCAMERA=auto\nFORMAT=auto\n"
            b"RESOLUTION=auto\nFPS=auto\nCSI_QUALITY=70\nPORT=8081\n"
        )
        self.config.write_bytes(self.original)

    def tearDown(self):
        self.directory.cleanup()

    def updates(self):
        return {"SOURCE": "csi", "CAMERA": "auto", "RESOLUTION": "640x480", "FPS": "30.0"}

    def test_invalid_active_config_can_be_repaired_from_last_good(self):
        self.config.write_text("SOURCE=invalid\nUNKNOWN=value\n")
        self.last_good.write_bytes(self.original)
        result = MODULE.apply_config(
            self.updates(), self.config, self.last_good,
            restart=mock.Mock(), ready=mock.Mock(return_value=True),
        )
        self.assertEqual(result["SOURCE"], "csi")
        self.assertEqual(MODULE.parse_config(self.config)["SOURCE"], "csi")

    def test_config_lock_reports_an_active_transaction(self):
        lock_path = MODULE.ensure_config_lock(self.config)
        self.assertFalse(MODULE.configuration_change_in_progress(self.config))
        with lock_path.open("r+") as lock_file:
            MODULE.fcntl.flock(lock_file.fileno(), MODULE.fcntl.LOCK_EX)
            try:
                self.assertTrue(MODULE.configuration_change_in_progress(self.config))
            finally:
                MODULE.fcntl.flock(lock_file.fileno(), MODULE.fcntl.LOCK_UN)
        self.assertFalse(MODULE.configuration_change_in_progress(self.config))

    def test_config_transaction_guard_holds_exclusive_lock_for_scope(self):
        with MODULE.config_transaction_guard(self.config) as acquired:
            self.assertTrue(acquired)
            self.assertTrue(MODULE.configuration_change_in_progress(self.config))
        self.assertFalse(MODULE.configuration_change_in_progress(self.config))

    def test_successful_apply_commits_and_updates_last_known_good(self):
        restart = mock.Mock()
        ready = mock.Mock(return_value=True)
        result = MODULE.apply_config(
            self.updates(), self.config, self.last_good, restart=restart, ready=ready
        )
        self.assertEqual(result["SOURCE"], "csi")
        self.assertEqual(MODULE.parse_config(self.config)["RESOLUTION"], "640x480")
        self.assertEqual(MODULE.parse_config(self.last_good)["RESOLUTION"], "640x480")
        restart.assert_called_once()
        ready.assert_called_once_with(8081)

    def test_failed_stream_readiness_restores_previous_config_and_restarts_it(self):
        restart = mock.Mock()
        ready = mock.Mock(side_effect=[False, True])
        with self.assertRaisesRegex(MODULE.ConfigApplyError, "previous settings were restored and the stream is healthy"):
            MODULE.apply_config(
                self.updates(), self.config, self.last_good, restart=restart, ready=ready
            )
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(self.last_good.read_bytes(), self.original)
        self.assertEqual(restart.call_count, 2)
        self.assertEqual(ready.call_args_list, [mock.call(8081), mock.call(8081)])

    def test_restart_failure_restores_previous_config(self):
        restart = mock.Mock(side_effect=[RuntimeError("backend failed to start"), None])
        ready = mock.Mock(return_value=True)
        with self.assertRaisesRegex(MODULE.ConfigApplyError, "backend failed to start"):
            MODULE.apply_config(
                self.updates(), self.config, self.last_good, restart=restart, ready=ready
            )
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(restart.call_count, 2)
        ready.assert_called_once_with(8081)

    def test_failed_rollback_preserves_the_previous_last_known_good_file(self):
        known_good = b"SOURCE=usb\nDEVICE=/dev/video0\n"
        self.last_good.write_bytes(known_good)
        restart = mock.Mock(side_effect=[None, RuntimeError("rollback restart failed")])
        ready = mock.Mock(return_value=False)
        with self.assertRaisesRegex(MODULE.ConfigApplyError, "restarting them failed"):
            MODULE.apply_config(
                self.updates(), self.config, self.last_good, restart=restart, ready=ready
            )
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(self.last_good.read_bytes(), known_good)

    def test_invalid_candidate_does_not_write_or_restart(self):
        restart = mock.Mock()
        ready = mock.Mock()
        with self.assertRaises(MODULE.ConfigError):
            MODULE.apply_config(
                {"SOURCE": "invalid"}, self.config, self.last_good, restart=restart, ready=ready
            )
        self.assertEqual(self.config.read_bytes(), self.original)
        restart.assert_not_called()
        ready.assert_not_called()


if __name__ == "__main__":
    unittest.main()
