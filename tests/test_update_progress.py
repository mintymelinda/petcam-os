import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
UPDATER_SPEC = importlib.util.spec_from_file_location(
    "petcam_updater_progress",
    ROOT / "stage-petcam/00-install-files/files/usr/local/lib/petcam/updater.py",
)
UPDATER = importlib.util.module_from_spec(UPDATER_SPEC)
UPDATER_SPEC.loader.exec_module(UPDATER)

MANAGER_SPEC = importlib.util.spec_from_file_location(
    "petcam_web_manager_progress",
    ROOT / "stage-petcam/00-install-files/files/usr/local/lib/petcam/web_manager.py",
)
MANAGER = importlib.util.module_from_spec(MANAGER_SPEC)
MANAGER_SPEC.loader.exec_module(MANAGER)


class UpdateProgressTests(unittest.TestCase):
    def test_progress_is_persisted_and_new_attempt_resets_old_terminal_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            with mock.patch.object(UPDATER, "STATE", state_dir):
                UPDATER.write_update_state("downloading", "Downloading release", target_version="1.2.3")
                first = json.loads((state_dir / "status.json").read_text())
                self.assertEqual(first["stage"], "downloading")
                self.assertEqual(first["target_version"], "1.2.3")
                self.assertEqual((state_dir / "status.json").stat().st_mode & 0o777, 0o644)
                UPDATER.write_update_state("complete", "Installed", installed_version="1.2.3")
                UPDATER.write_update_state("checking", "Checking signed release metadata")
                second = json.loads((state_dir / "status.json").read_text())
                self.assertEqual(second["stage"], "checking")
                self.assertNotIn("installed_version", second)
                self.assertNotIn("finished_at", second)
                self.assertGreaterEqual(second["started_at"], first["started_at"])

    def test_start_update_queues_oneshot_service_without_waiting_for_install(self):
        results = [
            mock.Mock(returncode=3, stdout="", stderr=""),
            mock.Mock(returncode=0, stdout="", stderr=""),
        ]
        with mock.patch.object(MANAGER, "subprocess") as subprocess_mock:
            subprocess_mock.run.side_effect = results
            MANAGER.start_update()
        self.assertEqual(subprocess_mock.run.call_args_list[1].args[0], [
            "sudo", "-n", "/usr/bin/systemctl", "start", "--no-block", "petcam-update.service"
        ])

    def test_start_update_does_not_queue_a_second_active_install(self):
        with mock.patch.object(UPDATER, "subprocess") as subprocess_mock:
            subprocess_mock.run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            with self.assertRaisesRegex(RuntimeError, "already running"):
                MANAGER.start_update()
        subprocess_mock.run.assert_called_once()

    def test_failed_installer_command_includes_exit_code_and_stderr(self):
        result = mock.Mock(returncode=1, stderr="permission denied", stdout="")
        message = UPDATER.command_failure("Restarting PetCam web service", result)
        self.assertIn("exit 1", message)
        self.assertIn("permission denied", message)

    def test_manager_reports_persisted_active_stage_after_reconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "status.json"
            state_file.write_text(json.dumps({
                "stage": "restarting",
                "message": "Restarting PetCam services",
                "updated_at": 1234,
                "target_version": "1.2.3",
            }))
            responses = [
                mock.Mock(returncode=0, stdout="activating\nsuccess\n0\n", stderr=""),
                mock.Mock(returncode=0, stdout="installer output\n", stderr=""),
            ]
            with mock.patch.object(MANAGER, "UPDATE_STATE_FILE", state_file), \
                    mock.patch.object(MANAGER.subprocess, "run", side_effect=responses):
                status = MANAGER.update_status()
        self.assertTrue(status["active"])
        self.assertEqual(status["stage"], "restarting")
        self.assertEqual(status["target_version"], "1.2.3")
        self.assertEqual(status["message"], "Restarting PetCam services")

    def test_manager_does_not_report_success_when_service_exits_without_terminal_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "status.json"
            state_file.write_text(json.dumps({
                "stage": "installing",
                "message": "Activating release",
                "updated_at": 1234,
            }))
            responses = [
                mock.Mock(returncode=0, stdout="inactive\nsuccess\n0\n", stderr=""),
                mock.Mock(returncode=0, stdout="", stderr=""),
            ]
            with mock.patch.object(MANAGER, "UPDATE_STATE_FILE", state_file), \
                    mock.patch.object(MANAGER.subprocess, "run", side_effect=responses):
                status = MANAGER.update_status()
        self.assertFalse(status["active"])
        self.assertEqual(status["stage"], "failed")
        self.assertEqual(status["result"], "failed")
        self.assertIn("stopped before reporting completion", status["message"])


if __name__ == "__main__":
    unittest.main()
