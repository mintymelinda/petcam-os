from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ImageShrinkContractTests(unittest.TestCase):
    def test_release_image_is_shrunk_before_release_metadata_is_generated(self):
        workflow = (ROOT / ".github/workflows/build-image.yml").read_text(encoding="utf-8")
        shrink_step = workflow.index("name: Shrink and recompress the release image")
        metadata_step = workflow.index("name: Prepare and publish signed application update")
        self.assertLess(shrink_step, metadata_step)
        self.assertIn('bash tools/shrink_image.sh "${IMAGES[0]}"', workflow)

    def test_pishrink_is_pinned_and_first_boot_expansion_is_preserved(self):
        script = (ROOT / "tools/shrink_image.sh").read_text(encoding="utf-8")
        self.assertRegex(script, r'PISHRINK_COMMIT="[0-9a-f]{40}"')
        self.assertIn(' -n -a -Z "$raw_image"', script)
        self.assertNotIn('pishrink.sh" -s', script)

    def test_build_only_packages_and_caches_are_removed_after_install(self):
        cleanup = (ROOT / "stage-petcam/02-configure-petcam/00-run-chroot.sh").read_text(encoding="utf-8")
        self.assertIn("apt-get purge -y --auto-remove", cleanup)
        self.assertIn("build-essential pkg-config git", cleanup)
        self.assertIn("apt-get clean", cleanup)
        self.assertIn("rm -rf /var/lib/apt/lists/*", cleanup)
        self.assertIn("apt-mark manual", cleanup)

    def test_runtime_library_lookup_ignores_dpkg_diversion_records(self):
        import subprocess

        parser = r"""$1 !~ /^(diversion|local diversion)( |$)/ && NF >= 2 { print $1; exit }"""
        result = subprocess.run(
            ["awk", "-F", ": ", parser],
            input=(
                "diversion by libc6 from: /lib/arm-linux-gnueabihf/libc.so.6\n"
                "libc6:armhf: /usr/lib/arm-linux-gnueabihf/libc.so.6\n"
            ),
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "libc6:armhf")

    def test_runtime_library_cleanup_resolves_symlinks_and_fails_closed(self):
        cleanup = (ROOT / "stage-petcam/02-configure-petcam/00-run-chroot.sh").read_text(encoding="utf-8")
        self.assertIn('readlink -f "$library"', cleanup)
        self.assertIn('Unable to identify the Debian package owning runtime library', cleanup)
        self.assertIn('apt-mark manual "$package"', cleanup)


if __name__ == "__main__":
    unittest.main()
