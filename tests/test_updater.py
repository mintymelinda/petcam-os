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

import updater

from updater import (
    MAX_ARCHIVE,
    UpdateError,
    download_archive,
    parse_manifest,
    safe_extract,
    validate_release,
    install_runtime_integration_files,
    repair_config_directory_permissions,
    remove_inactive_release,
    restart_services,
    verify_signature,
    version_tuple,
    release_manifest_candidates,
    install_update,
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
            (release / "systemd/petcam-motion.service").write_text("[Service]\nExecStart=motion\n")
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
            self.assertEqual((etc_root / "systemd/system/petcam-motion.service").read_text(), "[Service]\nExecStart=motion\n")
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o640)

    def test_installs_default_home_assistant_config_when_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            release = root / "release"
            (release / "systemd").mkdir(parents=True)
            (release / "etc/petcam").mkdir(parents=True)
            (release / "systemd/petcam-home-assistant.service").write_text("[Service]\n")
            (release / "systemd/petcam-timelapse.service").write_text("[Service]\n")
            (release / "systemd/petcam-motion.service").write_text("[Service]\n")
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
            mock.Mock(returncode=0),
            mock.Mock(returncode=0, stderr=""),
        ]
        with mock.patch("updater.subprocess.run", side_effect=results) as run:
            self.assertEqual(restart_services(), "")
        self.assertEqual(run.call_count, 8)
        self.assertEqual(run.call_args_list[-1].args[0], ["systemctl", "restart", "petcam-motion.service"])

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
            "lib/factory_reset.py": b"# Factory reset",
            "lib/home_assistant.py": b"# Home Assistant MQTT",
            "lib/timelapse.py": b"# Timelapse",
            "lib/motion_detection.py": b"# Motion detection",
            "systemd/petcam-home-assistant.service": b"[Service]\nExecStart=/usr/bin/python3 /opt/petcam/current/lib/home_assistant.py\n",
            "systemd/petcam-timelapse.service": b"[Service]\nExecStart=/usr/bin/python3 /opt/petcam/current/lib/timelapse.py\n",
            "systemd/petcam-motion.service": b"[Service]\nExecStart=/usr/bin/python3 /opt/petcam/current/lib/motion_detection.py\n",
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


class FailedReleaseCleanupTests(unittest.TestCase):
    def test_removes_failed_release_when_previous_release_is_active(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            releases = root / "releases"
            releases.mkdir()
            active = releases / "1.2.2"
            failed = releases / "1.2.3"
            active.mkdir()
            failed.mkdir()
            current = root / "current"
            current.symlink_to(active)
            with mock.patch("updater.CURRENT", current):
                self.assertTrue(remove_inactive_release(failed))
            self.assertTrue(active.is_dir())
            self.assertFalse(failed.exists())

    def test_never_removes_the_active_release(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            release = root / "releases/1.2.3"
            release.mkdir(parents=True)
            current = root / "current"
            current.symlink_to(release)
            with mock.patch("updater.CURRENT", current):
                self.assertFalse(remove_inactive_release(release))
            self.assertTrue(release.is_dir())


class ConfigDirectoryMigrationTests(unittest.TestCase):
    def test_repairs_existing_directory_owner_and_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "etc/petcam"
            directory.mkdir(parents=True)
            with mock.patch("updater.grp.getgrnam", return_value=mock.Mock(gr_gid=985)), \
                    mock.patch("updater.os.chown") as chown, \
                    mock.patch("updater.os.chmod") as chmod:
                repair_config_directory_permissions(directory)
            chown.assert_called_once_with(directory, 0, 985)
            chmod.assert_called_once_with(directory, 0o1770)

    def test_creates_missing_directory_with_restricted_permissions(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "etc/petcam"
            with mock.patch("updater.grp.getgrnam", return_value=mock.Mock(gr_gid=985)), \
                    mock.patch("updater.os.chown") as chown, \
                    mock.patch("updater.os.chmod") as chmod:
                repair_config_directory_permissions(directory)
            self.assertTrue(directory.is_dir())
            chown.assert_called_once_with(directory, 0, 985)
            chmod.assert_called_once_with(directory, 0o1770)

    def test_refuses_to_follow_a_symlinked_config_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target"
            target.mkdir()
            directory = root / "petcam"
            directory.symlink_to(target, target_is_directory=True)
            with mock.patch("updater.grp.getgrnam") as getgrnam:
                with self.assertRaisesRegex(UpdateError, "symlinked"):
                    repair_config_directory_permissions(directory)
            getgrnam.assert_not_called()


class UpdaterServiceSandboxTests(unittest.TestCase):
    def test_updater_service_can_write_only_required_integration_directories(self):
        script = Path("stage-petcam/02-configure-petcam/00-run-chroot.sh").read_text(encoding="utf-8")
        self.assertIn(
            "ReadWritePaths=/opt/petcam /var/lib/petcam/update /etc/systemd/system /etc/petcam",
            script,
        )
        self.assertIn("ProtectSystem=strict", script)



class RecentReleaseDiscoveryTests(unittest.TestCase):
    @staticmethod
    def manifest(version, archive_url="https://assets/release/archive.tar.gz"):
        return json.dumps({
            "schema_version": 1,
            "application": "petcam-os",
            "version": version,
            "architecture": "armhf",
            "archive_url": archive_url,
            "archive_size": 123,
            "archive_sha256": "a" * 64,
            "release_notes": f"Release {version}",
        }).encode()

    def test_skips_bad_recent_release_and_returns_valid_releases_newest_first(self):
        api_url = "https://api.github.com/repos/mintymelinda/petcam-os/releases?per_page=30"
        releases = [
            {"tag_name": "v1.5.0", "draft": False, "prerelease": False, "published_at": "2026-11-13T00:00:00Z",
             "assets": [{"name": "petcam-update-manifest.json", "browser_download_url": "https://assets/bad/manifest"},
                        {"name": "petcam-update-manifest.json.sig", "browser_download_url": "https://assets/bad/signature"},
                        {"name": "archive.tar.gz", "browser_download_url": "https://assets/bad/archive", "size": 123}]},
            {"tag_name": "v1.4.0", "draft": False, "prerelease": False, "published_at": "2026-11-12T00:00:00Z",
             "assets": [{"name": "petcam-update-manifest.json", "browser_download_url": "https://assets/good/manifest"},
                        {"name": "petcam-update-manifest.json.sig", "browser_download_url": "https://assets/good/signature"},
                        {"name": "archive.tar.gz", "browser_download_url": "https://assets/good/archive.tar.gz", "size": 123}]},
            {"tag_name": "v1.3.0", "draft": False, "prerelease": False, "published_at": "2026-11-11T00:00:00Z",
             "assets": [{"name": "petcam-update-manifest.json", "browser_download_url": "https://assets/older/manifest"},
                        {"name": "petcam-update-manifest.json.sig", "browser_download_url": "https://assets/older/signature"},
                        {"name": "archive.tar.gz", "browser_download_url": "https://assets/older/archive.tar.gz", "size": 123}]},
        ]
        payloads = {
            api_url: json.dumps(releases).encode(),
            "https://assets/bad/manifest": b"not json",
            "https://assets/bad/signature": b"bad",
            "https://assets/good/manifest": self.manifest("1.4.0", "https://assets/good/archive.tar.gz"),
            "https://assets/good/signature": b"valid-signature",
            "https://assets/good/archive.tar.gz": b"archive",
            "https://assets/older/manifest": self.manifest("1.3.0", "https://assets/older/archive.tar.gz"),
            "https://assets/older/signature": b"valid-signature",
            "https://assets/older/archive.tar.gz": b"archive",
        }

        def fake_fetch(url, limit):
            if url not in payloads:
                raise AssertionError(f"Unexpected URL: {url}")
            return payloads[url]

        with mock.patch("updater.fetch", side_effect=fake_fetch), mock.patch("updater.verify_signature") as verify:
            found = release_manifest_candidates(
                "https://github.com/mintymelinda/petcam-os/releases/latest/download/petcam-update-manifest.json",
                Path("/trusted/key.pem"),
            )

        self.assertEqual([item["manifest"]["version"] for item in found], ["1.4.0", "1.3.0"])
        self.assertEqual(found[0]["manifest_url"], "https://assets/good/manifest")
        self.assertEqual(verify.call_count, 3)

    def test_skips_release_when_signed_archive_asset_is_missing(self):
        api_url = "https://api.github.com/repos/mintymelinda/petcam-os/releases?per_page=30"
        releases = [{
            "tag_name": "v1.6.0", "draft": False, "prerelease": False,
            "assets": [
                {"name": "petcam-update-manifest.json", "browser_download_url": "https://assets/new/manifest"},
                {"name": "petcam-update-manifest.json.sig", "browser_download_url": "https://assets/new/signature"},
            ],
        }, {
            "tag_name": "v1.5.0", "draft": False, "prerelease": False,
            "assets": [
                {"name": "petcam-update-manifest.json", "browser_download_url": "https://assets/old/manifest"},
                {"name": "petcam-update-manifest.json.sig", "browser_download_url": "https://assets/old/signature"},
                {"name": "archive.tar.gz", "browser_download_url": "https://assets/old/archive.tar.gz", "size": 123},
            ],
        }]
        payloads = {
            api_url: json.dumps(releases).encode(),
            "https://assets/new/manifest": self.manifest("1.6.0", "https://assets/new/archive.tar.gz"),
            "https://assets/new/signature": b"valid-signature",
            "https://assets/old/manifest": self.manifest("1.5.0", "https://assets/old/archive.tar.gz"),
            "https://assets/old/signature": b"valid-signature",
        }
        with mock.patch("updater.fetch", side_effect=lambda url, limit: payloads[url]):
            with mock.patch("updater.verify_signature"):
                found = release_manifest_candidates(
                    "https://github.com/mintymelinda/petcam-os/releases/latest/download/petcam-update-manifest.json",
                    Path("/trusted/key.pem"),
                )
        self.assertEqual([item["manifest"]["version"] for item in found], ["1.5.0"])

    def test_http_404_for_one_release_does_not_block_older_complete_release(self):
        api_url = "https://api.github.com/repos/mintymelinda/petcam-os/releases?per_page=30"
        releases = [
            {
                "tag_name": "v1.6.0", "draft": False, "prerelease": False,
                "assets": [
                    {"name": "petcam-update-manifest.json", "browser_download_url": "https://assets/new/manifest"},
                    {"name": "petcam-update-manifest.json.sig", "browser_download_url": "https://assets/new/signature"},
                    {"name": "archive.tar.gz", "browser_download_url": "https://assets/new/archive.tar.gz", "size": 123},
                ],
            },
            {
                "tag_name": "v1.5.0", "draft": False, "prerelease": False,
                "assets": [
                    {"name": "petcam-update-manifest.json", "browser_download_url": "https://assets/old/manifest"},
                    {"name": "petcam-update-manifest.json.sig", "browser_download_url": "https://assets/old/signature"},
                    {"name": "archive.tar.gz", "browser_download_url": "https://assets/old/archive.tar.gz", "size": 123},
                ],
            },
        ]

        def fake_fetch(url, limit):
            if url == api_url:
                return json.dumps(releases).encode()
            if url == "https://assets/new/manifest":
                return self.manifest("1.6.0", "https://assets/new/archive.tar.gz")
            if url == "https://assets/new/signature":
                raise UpdateError("Unable to download update data: HTTP Error 404: Not Found")
            if url == "https://assets/old/manifest":
                return self.manifest("1.5.0", "https://assets/old/archive.tar.gz")
            if url == "https://assets/old/signature":
                return b"valid-signature"
            raise AssertionError(f"Unexpected URL: {url}")

        with mock.patch("updater.fetch", side_effect=fake_fetch):
            with mock.patch("updater.verify_signature"):
                found = release_manifest_candidates(
                    "https://github.com/mintymelinda/petcam-os/releases/latest/download/petcam-update-manifest.json",
                    Path("/trusted/key.pem"),
                )
        self.assertEqual([item["manifest"]["version"] for item in found], ["1.5.0"])

    def test_no_complete_release_is_a_nonfatal_empty_result(self):
        config = {
            "MANIFEST_URL": "https://github.com/mintymelinda/petcam-os/releases/latest/download/petcam-update-manifest.json",
        }
        with mock.patch("updater.platform.machine", return_value="armv7l"):
            with mock.patch("updater.release_manifest_candidates", return_value=[]):
                with mock.patch("updater.current_version", return_value="1.4.0"):
                    result = updater.check_update(config)
        self.assertEqual(result["status"], "no-complete-release")
        self.assertFalse(result["update_available"])
        self.assertIsNone(result["available_version"])
        self.assertEqual(result["releases"], [])

    def test_install_only_discovers_the_newest_valid_release(self):
        config = {"MANIFEST_URL": "https://github.com/mintymelinda/petcam-os/releases/latest/download/petcam-update-manifest.json"}
        status = {
            "status": "up-to-date",
            "update_available": False,
            "available_version": "1.2.3",
        }
        with mock.patch("updater.check_update", return_value=status) as check:
            result = install_update(config)
        self.assertEqual(result["status"], "up-to-date")
        check.assert_called_once_with(config, max_releases=1)

    def test_stops_fetching_after_ten_valid_signed_releases(self):
        api_url = "https://api.github.com/repos/mintymelinda/petcam-os/releases?per_page=30"
        releases = []
        payloads = {api_url: None}
        for index in range(11):
            version = f"1.{10 - index}.0"
            manifest_url = f"https://assets/release-{index}/manifest"
            signature_url = f"https://assets/release-{index}/signature"
            releases.append({
                "tag_name": f"v{version}",
                "draft": False,
                "prerelease": False,
                "assets": [
                    {"name": "petcam-update-manifest.json", "browser_download_url": manifest_url},
                    {"name": "petcam-update-manifest.json.sig", "browser_download_url": signature_url},
                    {"name": "archive.tar.gz", "browser_download_url": f"https://assets/release-{index}/archive.tar.gz", "size": 123},
                ],
            })
            payloads[manifest_url] = self.manifest(version, f"https://assets/release-{index}/archive.tar.gz")
            payloads[signature_url] = b"valid-signature"
        payloads[api_url] = json.dumps(releases).encode()

        def fake_fetch(url, limit, timeout=10):
            if url not in payloads:
                raise AssertionError(f"Unexpected URL fetched after reaching the release limit: {url}")
            return payloads[url]

        with mock.patch("updater.fetch", side_effect=fake_fetch) as fetch_mock:
            with mock.patch("updater.verify_signature"):
                found = release_manifest_candidates(
                    "https://github.com/mintymelinda/petcam-os/releases/latest/download/petcam-update-manifest.json",
                    Path("/trusted/key.pem"),
                )

        self.assertEqual(len(found), 10)
        self.assertEqual(fetch_mock.call_count, 21)

    def test_ignores_drafts_and_prereleases(self):
        api_url = "https://api.github.com/repos/mintymelinda/petcam-os/releases?per_page=30"
        releases = [
            {"tag_name": "v9.0.0", "draft": True, "prerelease": False, "assets": []},
            {"tag_name": "v8.0.0", "draft": False, "prerelease": True, "assets": []},
        ]
        with mock.patch("updater.fetch", return_value=json.dumps(releases).encode()):
            found = release_manifest_candidates(
                "https://github.com/mintymelinda/petcam-os/releases/latest/download/petcam-update-manifest.json",
                Path("/trusted/key.pem"),
            )
        self.assertEqual(found, [])

if __name__ == "__main__":
    unittest.main()
