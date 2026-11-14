import importlib.util
import unittest
from pathlib import Path
from unittest import mock

PATH = Path("stage-petcam/00-install-files/files/usr/local/lib/petcam/wifi_metrics.py")
SPEC = importlib.util.spec_from_file_location("petcam_wifi_metrics", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class WifiMetricsTests(unittest.TestCase):
    def setUp(self):
        MODULE._CACHE = None
        MODULE._CACHE_AT = 0
        MODULE._PREVIOUS.clear()

    def test_parses_wifi_signal_link_rates_frequency_and_retries(self):
        outputs = [
            "Connected to aa:bb:cc:dd:ee:ff\n\tfreq: 5180\n\tsignal: -57 dBm\n\trx bitrate: 433.3 MBit/s\n\ttx bitrate: 390.0 MBit/s\n",
            "Station aa:bb:cc:dd:ee:ff (on wlan0)\n\tsignal avg: -59 dBm\n\ttx retries: 12\n\ttx failed: 2\n",
            "Interface wlan0\n\ttype managed\n\tchannel 36 (5180 MHz)\n",
        ]
        with mock.patch.object(MODULE, "_default_interface", return_value="wlan0"), \
                mock.patch.object(MODULE, "_command", side_effect=outputs), \
                mock.patch.object(MODULE.Path, "exists", return_value=True), \
                mock.patch.object(MODULE, "_read_counter", side_effect=[1000, 2000, 1, 2, 0, 0]):
            result = MODULE.collect_network_performance()
        self.assertEqual(result["interface_name"], "wlan0")
        self.assertEqual(result["wifi"]["signal_dbm"], -57)
        self.assertEqual(result["wifi"]["rx_bitrate_mbps"], 433.3)
        self.assertEqual(result["wifi"]["tx_retries"], 12)
        self.assertEqual(result["wifi"]["channel"], 36)

    def test_reports_wired_link_without_claiming_wifi_data(self):
        with mock.patch.object(MODULE, "_command", return_value='[{"dev":"eth0"}]'), \
                mock.patch.object(MODULE.Path, "exists", return_value=False), \
                mock.patch.object(MODULE, "_read_counter", return_value=0):
            result = MODULE.collect_network_performance()
        self.assertEqual(result["type"], "wired_or_other")
        self.assertIsNone(result["wifi"]["signal_dbm"])


if __name__ == "__main__":
    unittest.main()
