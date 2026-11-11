import importlib.util
import unittest

SPEC = importlib.util.spec_from_file_location("petcam_camera", "stage-petcam/00-install-files/files/usr/local/lib/petcam/camera.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


CSI_SAMPLE = """Available cameras
-----------------
0 : imx477 [4056x3040] (/base/soc/i2c0mux/i2c@1/imx477@1a)
    Modes: 'SRGGB10_CSI2P' : 1332x990 [120.05 fps - (696, 528)/2664x1980 crop]
           'SRGGB12_CSI2P' : 2028x1080 [50.03 fps - (0, 440)/4056x2160 crop]
                             2028x1520 [40.01 fps - (0, 0)/4056x3040 crop]
                             4056x3040 [10.00 fps - (0, 0)/4056x3040 crop]
1 : imx708 [4608x2592] (/base/soc/i2c0mux/i2c@0/imx708@1a)
    Modes: 'SRGGB10_CSI2P' : 1536x864 [120.13 fps - (768, 432)/3072x1728 crop]
                             2304x1296 [56.03 fps - (0, 0)/4608x2592 crop]
                             4608x2592 [14.35 fps - (0, 0)/4608x2592 crop]
"""


SAMPLE = """ioctl: VIDIOC_ENUM_FMT
 Type: Video Capture
 [0]: 'MJPG' (Motion-JPEG, compressed)
   Size: Discrete 1920x1080
     Interval: Discrete 0.033s (30.000 fps)
     Interval: Discrete 0.017s (60.000 fps)
   Size: Discrete 1280x720
     Interval: Discrete 0.017s (60.000 fps)
 [1]: 'YUYV' ( absent )
   Size: Discrete 640x480
     Interval: Discrete 0.033s (30.000 fps)
"""


class CameraDiscoveryTests(unittest.TestCase):
    def test_parses_formats_resolutions_and_frame_rates(self):
        modes = MODULE.parse_modes(SAMPLE)
        self.assertEqual(len(modes), 3)
        self.assertEqual(modes[0]["format"], "MJPG")
        self.assertEqual((modes[0]["width"], modes[0]["height"]), (1920, 1080))
        self.assertEqual(modes[0]["fps"], [60.0, 30.0])
        self.assertEqual(modes[2]["format"], "YUYV")
        self.assertTrue(MODULE.mode_supports_fps(modes[0], 30))
        self.assertFalse(MODULE.mode_supports_fps(modes[0], 45))

    def test_prefers_native_mjpeg_and_uses_least_demanding_when_over_budget(self):
        devices = [{"device": "/dev/video0", "modes": MODULE.parse_modes(SAMPLE)}]
        device, mode, native = MODULE.select_mode(devices, "zero")
        self.assertTrue(native)
        self.assertEqual(mode["format"], "MJPG")
        self.assertEqual((mode["width"], mode["height"]), (1280, 720))
        self.assertEqual(device["device"], "/dev/video0")

    def test_filters_to_user_selected_supported_pixel_format(self):
        devices = [{"device": "/dev/video0", "modes": MODULE.parse_modes(SAMPLE)}]
        selected = MODULE.filter_devices_by_format(devices, "yuyv")
        self.assertEqual([mode["format"] for mode in selected[0]["modes"]], ["YUYV"])
        self.assertEqual(MODULE.filter_devices_by_format(devices, "H264"), [])

    def test_falls_back_to_supported_raw_format_and_ignores_unknown_fourcc(self):
        raw_device = [{"device": "/dev/video1", "modes": [MODULE.parse_modes(SAMPLE)[2]]}]
        device, mode, native = MODULE.select_mode(raw_device, "zero")
        self.assertFalse(native)
        self.assertEqual(mode["format"], "YUYV")
        self.assertEqual(device["device"], "/dev/video1")
        unsupported = [{"device": "/dev/video2", "modes": [{"format": "H264", "width": 1920, "height": 1080, "fps": [30]}]}]
        self.assertIsNone(MODULE.select_mode(unsupported, "pi5"))

    def test_parses_stepwise_sizes_and_frame_intervals(self):
        output = """[0]: 'MJPG' (Motion-JPEG, compressed)
  Size: Stepwise 320x240 - 1920x1080 with step 16/8
    Interval: Stepwise 0.010s - 0.100s with step 0.001s
"""
        mode = MODULE.parse_modes(output)[0]
        self.assertEqual((mode["width"], mode["height"]), (1920, 1080))
        self.assertTrue(MODULE.mode_supports_resolution(mode, 1280, 720))
        self.assertFalse(MODULE.mode_supports_resolution(mode, 1281, 720))
        self.assertAlmostEqual(mode["fps_range"]["min"], 10.0)
        self.assertAlmostEqual(mode["fps_range"]["max"], 100.0)
        self.assertTrue(MODULE.mode_supports_fps(mode, 50))
        self.assertFalse(MODULE.mode_supports_fps(mode, 60))
        self.assertFalse(MODULE.mode_supports_fps(mode, 120))


    def test_parses_csi_camera_sensor_modes(self):
        cameras = MODULE.parse_csi_cameras(CSI_SAMPLE)
        self.assertEqual(len(cameras), 2)
        self.assertEqual(cameras[0]["sensor"], "imx477")
        self.assertEqual((cameras[0]["sensor_width"], cameras[0]["sensor_height"]), (4056, 3040))
        self.assertEqual(len(cameras[0]["modes"]), 4)
        self.assertAlmostEqual(cameras[0]["modes"][0]["fps"], 120.05)

    def test_selects_csi_mode_within_board_budget_and_validates_overrides(self):
        cameras = MODULE.parse_csi_cameras(CSI_SAMPLE)
        selected = MODULE.select_csi_mode(cameras, "zero")
        self.assertTrue(selected["performance_warning"])
        explicit = MODULE.select_csi_mode(cameras, "pi5", requested_camera=1, resolution="2304x1296", fps="56.03")
        self.assertEqual(explicit["sensor"], "imx708")
        self.assertEqual(explicit["resolution"], "2304x1296")
        with self.assertRaises(ValueError):
            MODULE.select_csi_mode(cameras, "pi5", resolution="123x456")

    def test_model_tiers(self):
        self.assertEqual(MODULE.model_tier("Raspberry Pi Zero W Rev 1.1"), "zero")
        self.assertEqual(MODULE.model_tier("Raspberry Pi Zero 2 W Rev 1.0"), "zero2")
        self.assertEqual(MODULE.model_tier("Raspberry Pi 1 Model B+ Rev 1.2"), "pi1")
        self.assertEqual(MODULE.model_tier("Raspberry Pi 2 Model B Rev 1.1"), "pi2")
        self.assertEqual(MODULE.model_tier("Raspberry Pi 5 Model B Rev 1.0"), "pi5")


if __name__ == "__main__":
    unittest.main()
