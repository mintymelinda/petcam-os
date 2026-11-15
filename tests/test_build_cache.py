from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
PI_GEN_COMMIT = "6e3ff62bfd9c690a6a3d1d7de259d5e0af40cb78"


class BuildPipelineContractTests(unittest.TestCase):
    def test_image_jobs_use_native_arm64_runners(self):
        workflow = (ROOT / ".github/workflows/build-image.yml").read_text(encoding="utf-8")
        self.assertEqual(workflow.count("runs-on: ubuntu-24.04-arm"), 2)

    def test_pi_gen_action_is_pinned_and_keeps_the_armhf_target(self):
        workflow = (ROOT / ".github/workflows/build-image.yml").read_text(encoding="utf-8")
        self.assertIn("uses: usimd/pi-gen-action@v1.11.0", workflow)
        self.assertIn(f"pi-gen-version: {PI_GEN_COMMIT}", workflow)
        self.assertIn("compression: none", workflow)
        self.assertIn("enable-cloud-init: 1", workflow)
        self.assertIn("enable-pigen-cache: false", workflow)
        self.assertIn('"architecture": "armhf"', (ROOT / "tools/create_update_release.py").read_text(encoding="utf-8"))
        self.assertIn('if manifest["architecture"] != "armhf"', (
            ROOT / "stage-petcam/00-install-files/files/usr/local/lib/petcam/updater.py"
        ).read_text(encoding="utf-8"))

    def test_old_pi_gen_cache_patcher_and_cache_jobs_are_removed(self):
        workflow = (ROOT / ".github/workflows/build-image.yml").read_text(encoding="utf-8")
        self.assertFalse((ROOT / "tools/prepare_pigen_build.py").exists())
        self.assertFalse((ROOT / "tests/test_prepare_pigen_build.py").exists())
        self.assertNotIn("warm-base-cache", workflow)
        self.assertNotIn("actions/cache@", workflow)
        self.assertNotIn("APT_ARCHIVE_CACHE", workflow)
        self.assertNotIn("pigen-base-rootfs", workflow)
        self.assertNotIn("pigen-apt-", workflow)

    def test_buildkit_caches_only_the_product_tooling_container(self):
        workflow = (ROOT / ".github/workflows/build-image.yml").read_text(encoding="utf-8")
        compose = (ROOT / "build/compose.yaml").read_text(encoding="utf-8")
        dockerfile = (ROOT / "build/Dockerfile").read_text(encoding="utf-8")
        self.assertIn("docker/setup-buildx-action@v3", workflow)
        self.assertIn("docker/build-push-action@v6", workflow)
        self.assertIn("cache-from: type=gha,scope=petcam-image-assembly", workflow)
        self.assertIn("cache-to: type=gha,scope=petcam-image-assembly,mode=max", workflow)
        self.assertIn("docker compose --file build/compose.yaml run --rm --no-build image-assembly", workflow)
        self.assertIn("image-assembly:", compose)
        self.assertIn("build/requirements.txt", dockerfile)
        self.assertIn("tools/assemble_image.py", dockerfile)

    def test_explicit_package_and_python_manifests_are_enforced(self):
        packages = (ROOT / "build/apt-packages.txt").read_text(encoding="utf-8").splitlines()
        stage_packages = (
            ROOT / "stage-petcam-base/01-install-packages/00-packages"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(packages, stage_packages)
        requirements = (ROOT / "build/requirements.txt").read_text(encoding="utf-8")
        self.assertIn("jsonschema", requirements)
        validator = (ROOT / "tools/assemble_image.py").read_text(encoding="utf-8")
        self.assertIn("assemble(rootfs)", validator)
        self.assertIn('entry.get("architecture") != "armhf"', validator)

    def test_containerized_post_build_validates_image_and_raspberry_pi_schema(self):
        validator = (ROOT / "tools/assemble_image.py").read_text(encoding="utf-8")
        self.assertIn("create_imager_os_list.py", validator)
        self.assertIn("qemu-arm-static", validator)
        self.assertIn("losetup", validator)
        self.assertIn("validate(instance=catalogue, schema=schema)", validator)
        self.assertIn("IMAGE_SCHEMA_PATH", validator)
        self.assertIn("BASE_IMAGE_PATH", validator)
        self.assertIn("OUTPUT_IMAGE_PATH", validator)

    def test_release_signing_and_publish_gates_are_preserved(self):
        workflow = (ROOT / ".github/workflows/build-image.yml").read_text(encoding="utf-8")
        self.assertIn("PETCAM_UPDATE_SIGNING_PRIVATE_KEY", workflow)
        self.assertIn("refusing to publish an unsigned update", workflow)
        self.assertIn("cmp -s", workflow)
        self.assertIn("petcam-update-manifest.json.sig", workflow)
        self.assertIn("gh release edit", workflow)
        self.assertIn("draft=false", workflow)
        self.assertIn("petcam-${VERSION}-armhf.tar.gz", workflow)
        self.assertIn("gh release upload", workflow)

    def test_manual_build_does_not_publish_and_uploads_a_build_artifact(self):
        workflow = (ROOT / ".github/workflows/build-image.yml").read_text(encoding="utf-8")
        self.assertIn("github.event_name == 'workflow_dispatch'", workflow)
        self.assertIn("if: github.event_name == 'workflow_dispatch'", workflow)
        self.assertIn("actions/upload-artifact@v4", workflow)
        self.assertNotIn("warm-base-cache", workflow)


if __name__ == "__main__":
    unittest.main()
