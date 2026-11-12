import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "petcam_factory_reset",
    "stage-petcam/00-install-files/files/usr/local/lib/petcam/factory_reset.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FactoryResetTests(unittest.TestCase):
    def test_reset_restores_application_defaults_and_preserves_device_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            etc = root / "etc" / "petcam"
            state = root / "var" / "lib" / "petcam"
            etc.mkdir(parents=True)
            (etc / "web-auth.json").write_text("secret")
            (etc / "petcam.conf").write_text("SOURCE=usb\n")
            (etc / "petcam.conf.last-good").write_text("SOURCE=usb\n")
            (etc / "home-assistant.conf").write_text("MQTT_ENABLED=true\n")
            (etc / "web-manager.key").write_text("private key")
            (etc / "update-signing.pub").write_text("public key")
            (etc / "cameras.d").mkdir()
            (etc / "cameras.d" / "extra.conf").write_text("DEVICE=/dev/video0\n")
            (state / "timelapse" / "frames").mkdir(parents=True)
            (state / "timelapse" / "frames" / "frame.jpg").write_bytes(b"frame")
            (state / "motion" / "events").mkdir(parents=True)
            (state / "motion" / "events" / "event.jpg").write_bytes(b"event")
            (state / "health.json").write_text("{}")
            with mock.patch.object(MODULE, "ETC", etc), mock.patch.object(MODULE, "STATE", state), \
                    mock.patch.object(MODULE.os, "geteuid", return_value=0), \
                    mock.patch.object(MODULE.subprocess, "run"), \
                    mock.patch("pwd.getpwnam", return_value=mock.Mock(pw_uid=1000)), \
                    mock.patch("grp.getgrnam", return_value=mock.Mock(gr_gid=1000)):
                MODULE.reset()
            self.assertFalse((etc / "web-auth.json").exists())
            self.assertFalse((etc / "petcam.conf.last-good").exists())
            self.assertIn("SOURCE=auto", (etc / "petcam.conf").read_text())
            self.assertIn("MQTT_ENABLED=false", (etc / "home-assistant.conf").read_text())
            self.assertFalse((etc / "cameras.d" / "extra.conf").exists())
            self.assertFalse((state / "timelapse" / "frames" / "frame.jpg").exists())
            self.assertFalse((state / "motion" / "events" / "event.jpg").exists())
            self.assertFalse((state / "health.json").exists())
            self.assertEqual((etc / "web-manager.key").read_text(), "private key")
            self.assertEqual((etc / "update-signing.pub").read_text(), "public key")

    def test_reset_requires_root(self):
        with mock.patch.object(MODULE.os, "geteuid", return_value=1000):
            with self.assertRaises(PermissionError):
                MODULE.reset()


if __name__ == "__main__":
    unittest.main()
