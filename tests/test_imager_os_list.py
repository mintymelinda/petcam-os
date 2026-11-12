import hashlib
import json
import lzma
import tempfile
import unittest
from pathlib import Path

from tools.create_imager_os_list import create_os_list


class ImagerOSListTests(unittest.TestCase):
    def test_release_entry_contains_public_url_hashes_and_customisation_format(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "petcam-os-v0.1.0.img.xz"
            raw = b"petcam-release-image-" * 4096
            image.write_bytes(lzma.compress(raw))
            output = root / "petcam-os-os-list.json"
            create_os_list(image, "https://example.com/petcam.img.xz", output)
            entry = json.loads(output.read_text(encoding="utf-8"))["os_list"][0]
            self.assertEqual(entry["name"], "PetCam OS")
            self.assertTrue(entry["url"].startswith("https://"))
            self.assertEqual(entry["init_format"], "cloudinit-rpi")
            self.assertEqual(entry["architecture"], "armhf")
            self.assertEqual(
                entry["devices"],
                ["pi5-32bit", "pi4-32bit", "pi3-32bit", "pi2-32bit", "pi1-32bit"],
            )
            self.assertEqual(entry["extract_size"], len(raw))
            self.assertEqual(entry["extract_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(entry["image_download_size"], image.stat().st_size)
            self.assertEqual(entry["image_download_sha256"], hashlib.sha256(image.read_bytes()).hexdigest())

    def test_rejects_non_https_image_url(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "petcam.img.xz"
            image.write_bytes(lzma.compress(b"image"))
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                create_os_list(image, "file:///tmp/image.img.xz", Path(directory) / "out.json")


if __name__ == "__main__":
    unittest.main()
