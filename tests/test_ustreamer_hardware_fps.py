from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class UStreamerHardwareFpsTests(unittest.TestCase):
    def test_pinned_ustreamer_build_applies_hardware_fps_patch_before_compiling(self):
        build = (
            ROOT / "stage-petcam-base/02-build-ustreamer/00-run-chroot.sh"
        ).read_text(encoding="utf-8")

        check_patch = 'git -C "$SOURCE_DIR" apply --check "$SOURCE_DIR/petcam-hw-fps.patch"'
        apply_patch = 'git -C "$SOURCE_DIR" apply "$SOURCE_DIR/petcam-hw-fps.patch"'
        self.assertIn(check_patch, build)
        self.assertIn(apply_patch, build)
        self.assertLess(
            build.index(apply_patch),
            build.index('make -C "$SOURCE_DIR" -j'),
        )
        self.assertIn('{"hw-fps"', build)
        self.assertIn(
            'OPT_NUMBER("--hw-fps", cap->hw_fps, 0, US_VIDEO_MAX_FPS, 0)',
            build,
        )
        self.assertIn('TPF(denominator) = cap->hw_fps > 0 ? cap->hw_fps : (uint)-1;', build)

    def test_embedded_upstream_patch_has_valid_unified_diff_hunk_lengths(self):
        build = (
            ROOT / "stage-petcam-base/02-build-ustreamer/00-run-chroot.sh"
        ).read_text(encoding="utf-8")
        patch = build.split("<<'PATCH'\\n", 1)[1].split("\\nPATCH\\n", 1)[0]
        lines = patch.splitlines()

        for index, line in enumerate(lines):
            if not line.startswith("@@ "):
                continue
            header = re.match(r"^@@ -\\d+,(\\d+) \\+\\d+,(\\d+) @@", line)
            self.assertIsNotNone(header, line)
            old_expected, new_expected = map(int, header.groups())
            old_actual = 0
            new_actual = 0
            for patch_line in lines[index + 1:]:
                if patch_line.startswith("@@ ") or patch_line.startswith("diff --git "):
                    break
                if patch_line.startswith((" ", "-")):
                    old_actual += 1
                if patch_line.startswith((" ", "+")):
                    new_actual += 1
            self.assertEqual(old_expected, old_actual, line)
            self.assertEqual(new_expected, new_actual, line)

    def test_usb_startup_sets_hardware_rate_and_keeps_output_pacing(self):
        startup = (
            ROOT / "stage-petcam/02-configure-petcam/00-run-chroot.sh"
        ).read_text(encoding="utf-8")

        usb_start = startup.split("start_usb() {", 1)[1].split(
            "\nstart_csi() {", 1
        )[0]
        csi_start = startup.split("start_csi() {", 1)[1].split(
            '\ncase "$SOURCE" in', 1
        )[0]

        self.assertIn('--hw-fps="$fps"', usb_start)
        self.assertIn('--desired-fps="$fps"', usb_start)
        self.assertNotIn("--hw-fps", csi_start)


if __name__ == "__main__":
    unittest.main()
