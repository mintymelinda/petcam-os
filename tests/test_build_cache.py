from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BuildCacheContractTests(unittest.TestCase):
    def test_release_workflow_does_not_run_pishrink_or_recompress_the_image(self):
        workflow = (ROOT / ".github/workflows/build-image.yml").read_text(encoding="utf-8")
        self.assertNotIn("Shrink and recompress", workflow)
        self.assertNotIn("tools/shrink_image.sh", workflow)
        self.assertFalse((ROOT / "tools/shrink_image.sh").exists())

    def test_upstream_stage2_rootfs_is_cached_with_a_stable_explicit_epoch(self):
        workflow = (ROOT / ".github/workflows/build-image.yml").read_text(encoding="utf-8")
        self.assertIn("id: base-rootfs-cache", workflow)
        self.assertIn("pigen-base-rootfs-v1-", workflow)
        self.assertIn("hashFiles('config', 'pi-gen/stage0/**', 'pi-gen/stage1/**', 'pi-gen/stage2/**', 'pi-gen/scripts/**', 'pi-gen/Dockerfile')", workflow)
        self.assertIn("pigen-base-rootfs.tar", workflow)
        self.assertIn("sudo tar --numeric-owner --xattrs --acls -xpf", workflow)
        self.assertIn("sudo tar --numeric-owner --xattrs --acls -cpf", workflow)
        self.assertIn("touch pi-gen/stage0/SKIP pi-gen/stage1/SKIP pi-gen/stage2/SKIP", workflow)

    def test_cached_rootfs_is_mounted_as_pi_gen_work_dir(self):
        workflow = (ROOT / ".github/workflows/build-image.yml").read_text(encoding="utf-8")
        self.assertIn("src=${RUNNER_TEMP}/pigen-work,dst=/pigen-work", workflow)
        self.assertIn("-e WORK_DIR=/pigen-work", workflow)
        self.assertIn("stage2/rootfs", workflow)
        self.assertIn("Base rootfs cache hit:", workflow)
        self.assertIn("Base rootfs cache archive bytes:", workflow)

    def test_debian_archives_and_docker_layers_remain_cached(self):
        workflow = (ROOT / ".github/workflows/build-image.yml").read_text(encoding="utf-8")
        self.assertIn("docker/setup-buildx-action@v3", workflow)
        self.assertIn("--cache-from type=gha", workflow)
        self.assertIn("--cache-to type=gha,mode=max", workflow)
        self.assertIn("uses: actions/cache@v4", workflow)
        self.assertIn("APT_ARCHIVE_CACHE=/apt-cache", workflow)
        self.assertIn("--mount type=bind", workflow)

    def test_manual_image_build_remains_non_publishing_and_emits_timing_metrics(self):
        workflow = (ROOT / ".github/workflows/build-image.yml").read_text(encoding="utf-8")
        self.assertNotIn("build_image:", workflow)
        self.assertIn("if: github.ref_type == 'tag' || github.event_name == 'workflow_dispatch'", workflow)
        for step in (
            "name: Prepare release",
            "name: Prepare and publish signed application update",
        ):
            start = workflow.index(step)
            following = workflow.find("\n      - name:", start + len(step))
            block = workflow[start:] if following < 0 else workflow[start:following]
            self.assertIn("if: github.ref_type == 'tag'", block)
        self.assertIn("pi-gen build duration seconds:", workflow)
        self.assertIn("actions/upload-artifact@v4", workflow)
        self.assertIn("pigen-build-diagnostics-", workflow)

    def test_shared_apt_archive_cache_is_detached_before_target_cleanup(self):
        cleanup = (ROOT / "stage-petcam/02-configure-petcam/00-run-chroot.sh").read_text(encoding="utf-8")
        self.assertIn("umount /var/cache/apt/archives", cleanup)
        self.assertLess(cleanup.index("umount /var/cache/apt/archives"), cleanup.index("apt-get clean"))

    def test_runtime_library_cleanup_resolves_symlinks_and_fails_closed(self):
        cleanup = (ROOT / "stage-petcam/02-configure-petcam/00-run-chroot.sh").read_text(encoding="utf-8")
        self.assertIn('readlink -f "$library"', cleanup)
        self.assertIn("Unable to identify the Debian package owning runtime library", cleanup)
        self.assertIn('apt-mark manual "$package"', cleanup)


if __name__ == "__main__":
    unittest.main()
