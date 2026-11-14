import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from prepare_pigen_build import prepare_pigen


class PreparePigenBuildTests(unittest.TestCase):
    def test_patches_pinned_pi_gen_for_shared_build_and_apt_caches(self):
        with tempfile.TemporaryDirectory() as temporary:
            pigen = Path(temporary)
            (pigen / "scripts").mkdir()
            (pigen / "export-image" / "02-set-sources").mkdir(parents=True)
            (pigen / "export-image" / "05-finalise").mkdir(parents=True)
            (pigen / "build-docker.sh").write_text(
                '${DOCKER} build ${PLATFORM:+--platform "${PLATFORM}"} '
                '--build-arg BASE_IMAGE=${BASE_IMAGE} -t pi-gen "${DIR}"\\n',
                encoding="utf-8",
            )
            (pigen / "scripts" / "common").write_text(
                'on_chroot() {\\n\\techo "inside chroot"\\n}\\n'
                'setarch linux32 capsh $CAPSH_ARG "--chroot=${ROOTFS_DIR}/" -- -e "$@"\\n',
                encoding="utf-8",
            )
            cleanup = pigen / "export-image" / "02-set-sources" / "01-run.sh"
            cleanup.write_text("apt-get clean\\nEOF\\n", encoding="utf-8")
            finalise = pigen / "export-image" / "05-finalise" / "01-run.sh"
            finalise.write_text(
                'if [ -f "/usr/lib/systemd/system/apt-listchanges.service" ]; then\\n'
                '\\tpython3 -m apt_listchanges.populate_database --profile apt\\n'
                '\\tsystemctl disable apt-listchanges.timer\\n'
                'fi\\n',
                encoding="utf-8",
            )

            prepare_pigen(pigen)

            docker = (pigen / "build-docker.sh").read_text()
            common = (pigen / "scripts" / "common").read_text()
            export = cleanup.read_text()
            finalise_content = finalise.read_text()
            self.assertIn("buildx build", docker)
            self.assertIn("scope=pigen-builder", docker)
            self.assertIn("APT_ARCHIVE_CACHE", common)
            self.assertIn("mount --bind", common)
            self.assertIn("apt-get clean", export)
            self.assertIn("99-pigen-archive-cache", export)
            self.assertNotIn("apt_listchanges.populate_database", finalise_content)
            self.assertIn("systemctl disable apt-listchanges.timer", finalise_content)

    def test_rejects_unexpected_pi_gen_source_instead_of_silently_skipping_cache_patch(self):
        with tempfile.TemporaryDirectory() as temporary:
            pigen = Path(temporary)
            (pigen / "scripts").mkdir()
            (pigen / "export-image" / "02-set-sources").mkdir(parents=True)
            (pigen / "build-docker.sh").write_text("unexpected build command\\n")
            with self.assertRaises(SystemExit):
                prepare_pigen(pigen)


if __name__ == "__main__":
    unittest.main()
