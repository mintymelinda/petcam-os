import hashlib
import json
import lzma
import tempfile
import unittest
from pathlib import Path

from tools.create_imager_manifest import create_manifest


class ImagerManifestTests(unittest.TestCase):
    def test_manifest_describes_local_image_and_enables_cloudinit_rpi_customisation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "petcam.img.xz"
            raw = (b"petcam-image-data-" * 4096)
            image.write_bytes(lzma.compress(raw))
            manifest = root / "petcam-os.rpi-imager-manifest"

            create_manifest(image, manifest)

            data = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(len(data["os_list"]), 1)
            entry = data["os_list"][0]
            self.assertEqual(entry["name"], "PetCam OS")
            self.assertEqual(entry["url"], str(image.resolve()))
            self.assertEqual(entry["init_format"], "cloudinit-rpi")
            self.assertIn("pi0w", entry["devices"])
            self.assertEqual(entry["extract_size"], len(raw))
            self.assertEqual(entry["extract_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(entry["image_download_size"], image.stat().st_size)
            self.assertEqual(
                entry["image_download_sha256"],
                hashlib.sha256(image.read_bytes()).hexdigest(),
            )


if __name__ == "__main__":
    unittest.main()
