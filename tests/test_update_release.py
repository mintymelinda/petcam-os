import hashlib
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "stage-petcam/00-install-files/files/usr/local/lib/petcam"))

from create_update_release import build_release
from updater import parse_manifest, verify_signature


class SignedReleasePublisherTests(unittest.TestCase):
    def test_image_stage_copies_every_required_python_module_into_update_release(self):
        repo = Path(__file__).resolve().parents[1]
        stage_script = repo / "stage-petcam/00-install-files/00-run.sh"
        source_dir = repo / "stage-petcam/00-install-files/files/usr/local/lib/petcam"
        script = stage_script.read_text(encoding="utf-8")
        from create_update_release import REQUIRED_FILES
        systemd_source_dir = repo / "stage-petcam/00-install-files/files/etc/systemd/system"
        for relative in REQUIRED_FILES:
            if relative.startswith("lib/") and relative.endswith(".py"):
                source = source_dir / Path(relative).name
                install_line = f'install -m 0644 files/usr/local/lib/petcam/{source.name} "$RELEASE_DIR/{relative}"'
            elif relative.startswith("systemd/") and relative.endswith(".service"):
                source = systemd_source_dir / Path(relative).name
                install_line = f'install -m 0644 files/etc/systemd/system/{source.name} "$RELEASE_DIR/{relative}"'
            else:
                continue
            self.assertTrue(source.is_file(), f"Missing staged source for {relative}")
            self.assertIn(install_line, script, f"Image stage does not install required release asset {relative}")

    def make_release_tree(self, root):
        files = {
            "VERSION": b"1.2.3\n",
            "manifest.json": b'{"application":"petcam-os","version":"1.2.3"}\n',
            "lib/camera.py": b"# camera\n",
            "lib/csi_stream.py": b"# csi\n",
            "lib/web_manager.py": b"# web\n",
            "lib/web_auth.py": b"# authentication\n",
            "lib/health_monitor.py": b"# health monitor\n",
            "lib/config.py": b"# config\n",
            "lib/network_diagnostics.py": b"# network diagnostics\n",
            "lib/factory_reset.py": b"# factory reset\n",
            "lib/camera_profiles.py": b"# camera profiles\n",
            "lib/home_assistant.py": b"# Home Assistant MQTT\n",
            "lib/timelapse.py": b"# timelapse\n",
            "lib/motion_detection.py": b"# motion detection\n",
            "systemd/petcam-home-assistant.service": b"[Service]\nExecStart=petcam\n",
            "systemd/petcam-timelapse.service": b"[Service]\nExecStart=timelapse\n",
            "systemd/petcam-motion.service": b"[Service]\nExecStart=motion\n",
            "etc/petcam/home-assistant.conf": b"MQTT_ENABLED=false\n",
            "lib/start-stream.sh": b"#!/bin/sh\n",
            "vendor/bin/ustreamer": b"fake armhf binary",
        }
        for name, content in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            if name.endswith((".sh", "ustreamer")):
                path.chmod(0o755)
        return files

    def test_builds_archive_manifest_and_valid_ed25519_signature(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            release = root / "release"
            expected_files = self.make_release_tree(release)
            private = root / "private.pem"
            public = root / "public.pem"
            subprocess.run(
                ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(private)],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)],
                check=True, capture_output=True,
            )
            output = root / "assets"
            archive, manifest_path, signature_path = build_release(
                release, "1.2.3",
                "https://github.com/example/petcam/releases/download/v1.2.3/petcam-1.2.3-armhf.tar.gz",
                private, output,
            )
            raw = manifest_path.read_bytes()
            manifest = parse_manifest(raw)
            self.assertEqual(manifest["archive_size"], archive.stat().st_size)
            self.assertEqual(manifest["archive_sha256"], hashlib.sha256(archive.read_bytes()).hexdigest())
            verify_signature(raw, signature_path.read_bytes(), public)
            with tarfile.open(archive, "r:gz") as bundle:
                self.assertEqual(set(bundle.getnames()), set(expected_files))
                for name, expected in expected_files.items():
                    self.assertEqual(bundle.extractfile(name).read(), expected)

    def test_rejects_release_version_mismatch_and_missing_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            release = root / "release"
            self.make_release_tree(release)
            private = root / "private.pem"
            subprocess.run(
                ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(private)],
                check=True, capture_output=True,
            )
            args = (
                "https://github.com/example/petcam/releases/download/v1.2.3/update.tar.gz",
                private, root / "assets",
            )
            with self.assertRaises(ValueError):
                build_release(release, "1.2.4", *args)
            (release / "vendor/bin/ustreamer").unlink()
            with self.assertRaises(ValueError):
                build_release(release, "1.2.3", *args)


if __name__ == "__main__":
    unittest.main()
