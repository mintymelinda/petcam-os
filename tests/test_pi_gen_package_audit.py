from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
PI_GEN_COMMIT = "6e3ff62bfd9c690a6a3d1d7de259d5e0af40cb78"


class PiGenPackageAuditTests(unittest.TestCase):
    def test_petcam_package_list_avoids_packages_already_installed_by_pi_gen(self):
        packages = set(
            (ROOT / "stage-petcam/01-install-packages/00-packages")
            .read_text(encoding="utf-8")
            .split()
        )
        upstream_duplicates = {
            "avahi-daemon",
            "build-essential",
            "pkg-config",
            "v4l-utils",
            "rpicam-apps-lite",
            "python3-minimal",
        }
        self.assertFalse(packages & upstream_duplicates)
        # Keep discovery utilities available for future network diagnostics.
        self.assertIn("avahi-utils", packages)

    def test_release_build_uses_a_fixed_pi_gen_commit(self):
        workflow = (ROOT / ".github/workflows/build-image.yml").read_text(encoding="utf-8")
        self.assertIn(f"ref: {PI_GEN_COMMIT}", workflow)
        self.assertNotIn("ref: master", workflow)

    def test_release_exports_final_installed_package_inventory(self):
        workflow = (ROOT / ".github/workflows/build-image.yml").read_text(encoding="utf-8")
        self.assertIn("PACKAGE_INVENTORY=", workflow)
        self.assertIn("dpkg-query --admindir=", workflow)
        self.assertIn("${PACKAGE_INVENTORY}", workflow)
        self.assertIn("installed-packages.tsv", workflow)
        self.assertIn("build-info.txt", workflow)


if __name__ == "__main__":
    unittest.main()
