import importlib.util
import tempfile
import unittest
from pathlib import Path

MODULE_PATH=Path("stage-petcam/00-install-files/files/usr/local/lib/petcam/light_sensor.py")
SPEC=importlib.util.spec_from_file_location("petcam_light_sensor",MODULE_PATH)
MODULE=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

class LightSensorTests(unittest.TestCase):
    def test_bright_dark_thresholds_and_hysteresis(self):
        self.assertEqual(MODULE.classify(120,"unknown",100,30),"bright")
        self.assertEqual(MODULE.classify(20,"unknown",100,30),"dark")
        self.assertEqual(MODULE.classify(60,"bright",100,30),"bright")
        self.assertEqual(MODULE.classify(60,"dark",100,30),"dark")
        self.assertEqual(MODULE.classify(60,"unknown",100,30),"unknown")

    def test_rejects_invalid_threshold_order(self):
        for bright,dark in ((30,30),(20,30)):
            with self.subTest(bright=bright,dark=dark):
                with self.assertRaisesRegex(MODULE.SensorError,"dark < bright"):
                    MODULE.validate({**MODULE.DEFAULTS,"BRIGHT_THRESHOLD_LUX":str(bright),"DARK_THRESHOLD_LUX":str(dark)})

    def test_save_and_reload_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"light-sensor.conf"
            saved=MODULE.save_config({"ENABLED":True,"BRIGHT_THRESHOLD_LUX":250,"DARK_THRESHOLD_LUX":15},path)
            self.assertTrue(saved["ENABLED"])
            self.assertEqual(saved["BRIGHT_THRESHOLD_LUX"],250)
            self.assertEqual(saved["DARK_THRESHOLD_LUX"],15)
            self.assertEqual(MODULE.load_config(path),saved)

    def test_rejects_unknown_configuration_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(MODULE.SensorError,"Unknown"):
                MODULE.save_config({"UNSAFE_COMMAND":"anything"},Path(directory)/"config")

if __name__=="__main__": unittest.main()
