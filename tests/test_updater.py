import hashlib
import io
import json
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
    parse_manifest,
    safe_extract,
    validate_release,
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

    def test_validates_required_release_files_and_embedded_version(self):
        files = {
            "VERSION": b"1.2.3\n",
            "manifest.json": json.dumps({"application": "petcam-os", "version": "1.2.3"}).encode(),
            "lib/camera.py": b"# camera",
            "lib/csi_stream.py": b"# csi",
            "lib/web_manager.py": b"# web",
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
