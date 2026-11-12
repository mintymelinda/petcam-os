import subprocess
import io
import json
import hashlib
import tarfile
import tempfile
import unittest
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "stage-petcam/00-install-files/files/usr/local/lib/petcam"))

from updater import (
    MAX_ARCHIVE,
    UpdateError,
    download_archive,
    parse_manifest,
    safe_extract,
    validate_release,
    install_runtime_integration_files,
    restart_services,
    verify_signature,
    version_tuple,
)


class UpdateManifestTests(unittest.TestCase):
    def valid_manifest(self):
        return {
            "schema_version": 1,
            "application": "petcam-os",
            "version": "1.2.3",
            "architecture": "armhf",
            "archive_url": "https://github.com/example/petcam/releases/download/v1.2.3/petcam.tar.gz",
            "archive_size": 123,
            "archive_sha256": "a" * 64,
        }

    def test_accepts_valid_manifest(self):
        result = parse_manifest(json.dumps(self.valid_manifest()).encode())
        self.assertEqual(result["version"], "1.2.3")

    def test_rejects_wrong_architecture_and_non_https_archive(self):
        manifest = self.valid_manifest()
        manifest["architecture"] = "aarch64"
        with self.assertRaises(UpdateError):
            parse_manifest(json.dumps(manifest).encode())
        manifest["architecture"] = "armhf"
        manifest["archive_url"] = "http://example.com/release.tar.gz"
        with self.assertRaises(UpdateError):
            parse_manifest(json.dumps(manifest).encode())

    def test_rejects_invalid_digest_size_and_version(self):
        for key, value in (
            ("archive_sha256", "bad"),
            ("archive_size", MAX_ARCHIVE + 1),
            ("version", "../1.2.3"),
        ):
            manifest = self.valid_manifest()
            manifest[key] = value
            with self.subTest(key=key), self.assertRaises(UpdateError):
                parse_manifest(json.dumps(manifest).encode())


    def test_ed25519_signature_accepts_original_manifest_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            private = root / "private.pem"
            public = root / "public.pem"
            message = root / "manifest.json"
            signature = root / "manifest.sig"
            message.write_bytes(b'{"application":"petcam-os","version":"1.2.3"}')
            subprocess.run(
                ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(private)],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["openssl", "pkeyutl", "-sign", "-inkey", str(private), "-rawin",
                 "-in", str(message), "-out", str(signature)],
                check=True, capture_output=True,
            )
            verify_signature(message.read_bytes(), signature.read_bytes(), public)
            with self.assertRaises(UpdateError):
                verify_signature(message.read_bytes() + b" ", signature.read_bytes(), public)


    def test_download_streams_and_verifies_archive_bytes(self):
        payload = b"release archive bytes"
        digest = hashlib.sha256(payload).hexdigest()

        class Response(io.BytesIO):
            headers = {"Content-Length": str(len(payload))}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

            def geturl(self):
                return "https://example.com/release.tar.gz"

        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "release.tar.gz"
            with mock.patch("updater.urllib.request.urlopen", return_value=Response(payload)):
                download_archive("https://example.com/release.tar.gz", destination, len(payload), digest)
            self.assertEqual(destination.read_bytes(), payload)

        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "release.tar.gz"
            with mock.patch("updater.urllib.request.urlopen", return_value=Response(payload)):
                with self.assertRaises(UpdateError):
                    download_archive("https://example.com/release.tar.gz", destination, len(payload), "0" * 64)
            self.assertFalse(destination.exists())

    def test_semver_comparison_is_numeric(self):
        self.assertGreater(version_tuple("1.10.0"), version_tuple("1.9.9"))
        with self.assertRaises(UpdateError):
            version_tuple("v1.2.3")


class UpdateArchiveTests(unittest.TestCase):
    @staticmethod
    def archive(files):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as bundle:
            for name, content in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mode = 0o755 if name.endswith((".sh", "ustreamer")) else 0o644
                bundle.addfile(info, io.BytesIO(content))
        return stream.getvalue()

    def test_extracts_regular_files_and_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            safe_extract(self.archive({"lib/camera.py": b"print('ok')"}), root)
            self.assertEqual((root / "lib/camera.py").read_bytes(), b"print('ok')")
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(UpdateError):
                safe_extract(self.archive({"../escape": b"no"}), Path(temp))

    def test_rejects_symbolic_links(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as bundle:
            link = tarfile.TarInfo("lib/evil")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            bundle.addfile(link)
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(UpdateError):
                safe_extract(stream.getvalue(), Path(temp))

    def test_installs_signed_home_assistant_unit_and_preserves_existing_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            release = root / "release"
            etc_root = root / "etc"
            (release / "systemd").mkdir(parents=True)
            (release / "etc/petcam").mkdir(parents=True)
            (release / "systemd/petcam-home-assistant.service").write_text("[Service]\nExecStart=petcam\n")
            (release / "systemd/petcam-timelapse.service").write_text("[Service]\nExecStart=timelapse\n")
            (release / "etc/petcam/home-assistant.conf").write_text("MQTT_ENABLED=false\n")
            config_path = etc_root / "petcam/home-assistant.conf"
            config_path.parent.mkdir(parents=True)
            config_path.write_text("MQTT_ENABLED=true\nMQTT_PASSWORD=preserve-me\n")
            with mock.patch("updater.grp.getgrnam", return_value=mock.Mock(gr_gid=1000)), \
                    mock.patch("updater.os.geteuid", return_value=1000):
                install_runtime_integration_files(release, etc_root)
            self.assertEqual(
                (etc_root / "systemd/system/petcam-home-assistant.service").read_text(),
                "[Service]\nExecStart=petcam\n",
            )
            self.assertEqual(config_path.read_text(), "MQTT_ENABLED=true\nMQTT_PASSWORD=preserve-me\n")
            self.assertEqual((etc_root / "systemd/system/petcam-timelapse.service").read_text(), "[Service]\nExecStart=timelapse\n")
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o640)

    def test_installs_default_home_assistant_config_when_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            release = root / "release"
            (release / "systemd").mkdir(parents=True)
            (release / "etc/petcam").mkdir(parents=True)
            (release / "systemd/petcam-home-assistant.service").write_text("[Service]\n")
            (release / "systemd/petcam-timelapse.service").write_text("[Service]\n")
            (release / "etc/petcam/home-assistant.conf").write_text("MQTT_ENABLED=false\n")
            with mock.patch("updater.grp.getgrnam", return_value=mock.Mock(gr_gid=1000)), \
                    mock.patch("updater.os.geteuid", return_value=1000):
                install_runtime_integration_files(release, root / "etc")
            config = root / "etc/petcam/home-assistant.conf"
            self.assertEqual(config.read_text(), "MQTT_ENABLED=false\n")
            self.assertEqual(config.stat().st_mode & 0o777, 0o640)

    def test_restart_services_restarts_optional_mqtt_only_when_active(self):
        results = [
            mock.Mock(returncode=0, stderr=""),
            mock.Mock(returncode=0, stderr=""),
            mock.Mock(returncode=0),
            mock.Mock(returncode=0, stderr=""),
            mock.Mock(returncode=0),
            mock.Mock(returncode=0, stderr=""),
        ]
        with mock.patch("updater.subprocess.run", side_effect=results) as run:
            self.assertEqual(restart_services(), "")
        self.assertEqual(run.call_count, 6)
        self.assertEqual(run.call_args_list[-1].args[0], ["systemctl", "restart", "petcam-timelapse.service"])

    def test_validates_required_release_files_and_embedded_version(self):
        files = {
            "VERSION": b"1.2.3\n",
            "manifest.json": json.dumps({"application": "petcam-os", "version": "1.2.3"}).encode(),
            "lib/camera.py": b"# camera",
            "lib/csi_stream.py": b"# csi",
            "lib/web_manager.py": b"# web",
            "lib/web_auth.py": b"# authentication",
            "lib/health_monitor.py": b"# health monitor",
            "lib/config.py": b"# config",
            "lib/network_diagnostics.py": b"# network diagnostics",
            "lib/camera_profiles.py": b"# camera profiles",
            "lib/home_assistant.py": b"# Home Assistant MQTT",
            "lib/timelapse.py": b"# Timelapse",
            "systemd/petcam-home-assistant.service": b"[Service]\nExecStart=/usr/bin/python3 /opt/petcam/current/lib/home_assistant.py\n",
            "systemd/petcam-timelapse.service": b"[Service]\nExecStart=/usr/bin/python3 /opt/petcam/current/lib/timelapse.py\n",
            "etc/petcam/home-assistant.conf": b"MQTT_ENABLED=false\n",
            "lib/start-stream.sh": b"#!/bin/sh\n",
            "vendor/bin/ustreamer": b"binary",
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            safe_extract(self.archive(files), root)
            validate_release(root, "1.2.3")
            self.assertTrue((root / "vendor/bin/ustreamer").stat().st_mode & 0o111)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            safe_extract(self.archive({"VERSION": b"9.9.9"}), root)
            with self.assertRaises(UpdateError):
                validate_release(root, "1.2.3")


if __name__ == "__main__":
    unittest.main()
