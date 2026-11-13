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


class BuildCacheContractTests(unittest.TestCase):
    def test_release_build_uses_buildkit_and_persistent_apt_archive_caches(self):
        workflow = (ROOT / ".github/workflows/build-image.yml").read_text(encoding="utf-8")
        self.assertIn("docker/setup-buildx-action@v3", workflow)
        self.assertIn("--cache-from type=gha", workflow)
        self.assertIn("--cache-to type=gha,mode=max", workflow)
        self.assertIn("uses: actions/cache@v4", workflow)
        self.assertIn("pigen-apt-v2-${{ runner.os }}-trixie-6e3ff62bfd9c690a6a3d1d7de259d5e0af40cb78-", workflow)
        self.assertIn("APT_ARCHIVE_CACHE=/apt-cache", workflow)
        self.assertIn("--mount type=bind", workflow)

    def test_manual_image_build_is_non_publishing_and_emits_timing_metrics(self):
        workflow = (ROOT / ".github/workflows/build-image.yml").read_text(encoding="utf-8")

        # Manual dispatch starts a build directly; it must not require the
        # removed build_image opt-in checkbox.
        self.assertNotIn("build_image:", workflow)
        self.assertIn("if: github.ref_type == 'tag' || github.event_name == 'workflow_dispatch'", workflow)

        # Release preparation, image shrinking, and signed updater publication
        # are tag-only. A manual build produces diagnostics but publishes no release.
        for step in (
            "name: Prepare release",
            "name: Shrink and recompress the release image",
            "name: Prepare and publish signed application update",
        ):
            start = workflow.index(step)
            following = workflow.find("\n      - name:", start + len(step))
            block = workflow[start:] if following < 0 else workflow[start:following]
            self.assertIn("if: github.ref_type == 'tag'", block)

        self.assertIn("pi-gen build duration seconds:", workflow)
        self.assertIn("actions/upload-artifact@v4", workflow)
        self.assertIn("pigen-build-diagnostics-", workflow)

    def test_pigen_final_cleanup_preserves_external_apt_archive_cache(self):
        workflow = (ROOT / ".github/workflows/build-image.yml").read_text(encoding="utf-8")
        self.assertIn('export_cleanup = Path("pi-gen/export-image/02-set-sources/01-run.sh")', workflow)
        self.assertIn('clean_command = "apt-get clean\\n"', workflow)
        self.assertIn('if [ -z "${APT_ARCHIVE_CACHE:-}" ]; then', workflow)
        self.assertIn("cache_config_cleanup =", workflow)
        self.assertIn("99-pigen-archive-cache", workflow)
        self.assertIn("content += cache_config_cleanup", workflow)

    def test_shared_apt_archive_cache_is_detached_before_target_cleanup(self):
        cleanup = (ROOT / "stage-petcam/02-configure-petcam/00-run-chroot.sh").read_text(encoding="utf-8")
        self.assertIn('umount /var/cache/apt/archives', cleanup)
        self.assertLess(cleanup.index('umount /var/cache/apt/archives'), cleanup.index("apt-get clean"))


if __name__ == "__main__":
    unittest.main()
