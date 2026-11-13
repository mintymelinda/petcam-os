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
