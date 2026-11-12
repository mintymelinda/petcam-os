import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "stage-petcam"
INSTALL = STAGE / "00-install-files" / "00-run.sh"
CONFIGURE = STAGE / "02-configure-petcam" / "00-run-chroot.sh"
VERSION = (ROOT / "VERSION").read_text().strip()
STAGE_VERSION = (
    STAGE / "00-install-files" / "files/usr/local/lib/petcam/VERSION"
).read_text().strip()


class ImageLayoutTests(unittest.TestCase):
    def test_image_and_stage_versions_match_semver(self):
        self.assertEqual(VERSION, STAGE_VERSION)
        self.assertRegex(VERSION, re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$"))

    def test_install_stage_creates_versioned_release_and_manifest(self):
        script = INSTALL.read_text()
        self.assertIn('/opt/petcam/releases/${APP_VERSION}', script)
        self.assertIn('"$RELEASE_DIR/lib/camera.py"', script)
        self.assertIn('"$RELEASE_DIR/lib/csi_stream.py"', script)
        self.assertIn('"$RELEASE_DIR/lib/web_manager.py"', script)
        self.assertIn('"$RELEASE_DIR/manifest.json"', script)
        self.assertIn('"architecture":"armhf"', script)

    def test_runtime_uses_stable_wrappers_and_active_release(self):
        script = CONFIGURE.read_text()
        self.assertIn('ln -sfn "releases/$APP_VERSION" /opt/petcam/current', script)
        self.assertIn('/opt/petcam/current/lib/camera.py', script)
        self.assertIn('/opt/petcam/current/lib/start-stream.sh', script)
        self.assertIn('/opt/petcam/current/lib/web_manager.py', script)
        self.assertIn('/opt/petcam/current/lib/csi_stream.py', script)

    def test_stream_backend_uses_loopback_port_separate_from_web_ui(self):
        script = CONFIGURE.read_text()
        self.assertIn('PORT=8081', script)
        self.assertIn('--host=127.0.0.1', script)
        self.assertIn('/opt/petcam/current/vendor/bin/ustreamer', script)
        self.assertIn('ThreadingHTTPServer(("0.0.0.0", 8080)', (
            STAGE / "00-install-files/files/usr/local/lib/petcam/web_manager.py"
        ).read_text())

    def test_ustreamer_source_is_pinned_and_config_permissions_are_valid(self):
        script = CONFIGURE.read_text()
        self.assertIn(
            "git -C /opt/ustreamer fetch --depth 1 "
            "https://github.com/pikvm/ustreamer.git "
            "11fd828ab00c01a39bd0843ef442ef0b6d3cac41",
            script,
        )
        self.assertIn("chown root:petcam /etc/petcam", script)
        self.assertIn("chmod 1770 /etc/petcam", script)
        self.assertNotIn("chmod 0750 /etc/petcam", script)
        self.assertIn("chown petcam:petcam /etc/petcam/petcam.conf", script)
        self.assertIn("chmod 0640 /etc/petcam/petcam.conf", script)
        self.assertNotIn(r"\nchmod", script)

    def test_release_manifest_schema_is_versioned(self):
        script = INSTALL.read_text()
        self.assertIn('"schema_version":1', script)
        self.assertIn('"version":"%s"', script)
        self.assertIn('"ustreamer_commit"', script)


if __name__ == "__main__":
    unittest.main()
