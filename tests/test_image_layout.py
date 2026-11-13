import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "stage-petcam"
BASE_STAGE = ROOT / "stage-petcam-base"
BASE_PACKAGES = BASE_STAGE / "01-install-packages" / "00-packages"
USTREAMER_BUILD = BASE_STAGE / "02-build-ustreamer" / "00-run-chroot.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "build-image.yml"
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
        self.assertNotIn("camera_profiles.py", script)
        self.assertIn('"$RELEASE_DIR/lib/home_assistant.py"', script)
        self.assertIn('"$RELEASE_DIR/lib/light_sensor.py"', script)
        self.assertIn('"$RELEASE_DIR/systemd/petcam-light-sensor.service"', script)
        self.assertIn('"${ROOTFS_DIR}/etc/systemd/system/petcam-light-sensor.service"', script)
        self.assertIn('"$RELEASE_DIR/etc/petcam/light-sensor.conf"', script)
        self.assertIn('"${ROOTFS_DIR}/etc/petcam/light-sensor.conf"', script)
        self.assertIn('"$RELEASE_DIR/systemd/petcam-home-assistant.service"', script)
        self.assertIn('"$RELEASE_DIR/etc/petcam/home-assistant.conf"', script)
        self.assertIn('"architecture":"armhf"', script)

    def test_runtime_uses_stable_wrappers_and_active_release(self):
        script = CONFIGURE.read_text()
        self.assertIn('ln -sfn "releases/$APP_VERSION" /opt/petcam/current', script)
        self.assertIn('/opt/petcam/current/lib/camera.py', script)
        self.assertIn('/opt/petcam/current/lib/start-stream.sh', script)
        self.assertIn('args+=(--physical-path "$USB_PHYSICAL_PATH")', script)
        self.assertIn('args+=(--device-path "$CSI_DEVICE_PATH")', script)
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

    def test_ustreamer_build_is_part_of_the_cached_base_rootfs(self):
        build_script = USTREAMER_BUILD.read_text()
        configure_script = CONFIGURE.read_text()
        packages = BASE_PACKAGES.read_text().splitlines()
        self.assertIn(
            "USTREAMER_COMMIT=\"11fd828ab00c01a39bd0843ef442ef0b6d3cac41\"",
            build_script,
        )
        self.assertIn(
            "git -C \"$SOURCE_DIR\" fetch --depth 1 "
            "https://github.com/pikvm/ustreamer.git \"$USTREAMER_COMMIT\"",
            build_script,
        )
        self.assertIn('make -C "$SOURCE_DIR" -j"$(nproc)"', build_script)
        self.assertIn('PREFIX="/opt/petcam/ustreamer"', build_script)
        self.assertNotIn("build-essential", packages)
        self.assertNotIn("pkg-config", packages)
        self.assertIn('cp -a /opt/petcam/ustreamer/.', configure_script)
        self.assertNotIn("git -C /opt/ustreamer fetch", configure_script)
        self.assertIn("apt-get purge -y --auto-remove", build_script)
        self.assertIn("umount /var/cache/apt/archives", build_script)
        self.assertNotIn("apt-get purge -y --auto-remove", configure_script)
        script = configure_script
        self.assertIn("chown root:petcam /etc/petcam", script)
        self.assertIn("chmod 1770 /etc/petcam", script)
        self.assertNotIn("chmod 0750 /etc/petcam", script)
        self.assertIn("chown petcam:petcam /etc/petcam/petcam.conf", script)
        self.assertIn("chmod 0640 /etc/petcam/petcam.conf", script)
        self.assertIn("SupplementaryGroups=petcam video i2c", script)
        self.assertIn("petcam-stream@.service", script)
        template_at = script.index('cat > /etc/systemd/system/petcam-stream@.service')
        self.assertGreater(script.index("systemctl daemon-reload", template_at), template_at)
        self.assertIn("install -d -o petcam -g petcam -m 0750 /etc/petcam/cameras.d", script)
        self.assertIn('case "$action" in\n    start|restart|enable)', script)
        self.assertNotIn(r"\nchmod", script)

    def test_main_branch_seeds_cache_shared_by_release_tags(self):
        workflow = WORKFLOW.read_text()
        self.assertIn("warm-base-cache:", workflow)
        self.assertIn("github.ref == 'refs/heads/main' || github.ref_type == 'tag'", workflow)
        self.assertIn("needs: validate", workflow)
        self.assertIn("actions/cache/restore@v4", workflow)
        self.assertIn("lookup-only: true", workflow)
        self.assertIn("actions/cache/save@v4", workflow)
        self.assertIn("pigen-base-rootfs-v2-", workflow)
        self.assertIn('STAGE_LIST="stage0 stage1 stage2 stage-petcam-base"', workflow)

    def test_home_assistant_mqtt_is_optional_and_disabled_by_default(self):
        script = CONFIGURE.read_text()
        install_script = INSTALL.read_text()
        service = (STAGE / "00-install-files/files/etc/systemd/system/petcam-home-assistant.service").read_text()
        config = (STAGE / "00-install-files/files/etc/petcam/home-assistant.conf").read_text()
        self.assertIn("MQTT_ENABLED=false", config)
        self.assertIn("chmod 0640 /etc/petcam/home-assistant.conf", script)
        self.assertIn("chown root:petcam /etc/petcam/light-sensor.conf", script)
        self.assertIn("systemctl enable petcam-light-sensor.service", script)
        self.assertIn("petcam-home-assistant.service", install_script)
        self.assertIn("/opt/petcam/current/lib/home_assistant.py", service)
        self.assertNotIn("systemctl enable petcam-home-assistant.service", script)
        self.assertIn("RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6", service)

    def test_release_manifest_schema_is_versioned(self):
        script = INSTALL.read_text()
        self.assertIn('"schema_version":1', script)
        self.assertIn('"version":"%s"', script)
        self.assertIn('"ustreamer_commit"', script)


if __name__ == "__main__":
    unittest.main()
