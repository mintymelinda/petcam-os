import importlib.util
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(
    "stage-petcam/00-install-files/files/usr/local/lib/petcam/network_diagnostics.py"
)
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("petcam_network_diagnostics", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class NetworkDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.common = [
            mock.patch.object(MODULE.socket, "gethostname", return_value="petcam"),
            mock.patch.object(MODULE, "_mdns_resolution", return_value=["192.168.1.20"]),
            mock.patch.object(MODULE, "collect_interfaces", return_value=[
                {"name": "wlan0", "type": "wireless", "state": "up", "carrier": True}
            ]),
            mock.patch.object(MODULE, "command_output", side_effect=[
                json.dumps([{"ifname": "wlan0", "addr_info": [
                    {"family": "inet", "local": "192.168.1.20", "scope": "global"}
                ]}]),
                json.dumps([{"dev": "wlan0", "gateway": "192.168.1.1"}]),
                "State Recv-Q Send-Q Local Address:Port Peer Address:Port\nLISTEN 0 128 0.0.0.0:8080 0.0.0.0:*\nLISTEN 0 128 0.0.0.0:8443 0.0.0.0:*\nLISTEN 0 128 127.0.0.1:8081 0.0.0.0:*\n",
            ]),
            mock.patch.object(MODULE, "probe_http", return_value={"reachable": True, "status": 200, "frame_byte_received": True}),
        ]
        for patcher in self.common:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.common):
            patcher.stop()

    def test_healthy_operation_reports_all_local_layers_and_obs_urls(self):
        report = MODULE.collect_diagnostics()
        self.assertEqual(report["obs_browser_source_urls"][0], "http://petcam.local:8080/stream")
        self.assertTrue(report["layers"]["capture_backend"]["healthy"])
        self.assertTrue(report["layers"]["manager_https"]["reachable"])
        self.assertTrue(report["layers"]["obs_stream_proxy"]["reachable"])
        self.assertTrue(report["layers"]["hostname_resolution"]["resolved"])
        self.assertTrue(report["layers"]["network_link"]["healthy"])
        self.assertTrue(report["layers"]["default_route"]["available"])
        self.assertFalse(report["remote_client_tested"])

    def test_missing_mdns_is_reported_without_claiming_remote_failure(self):
        with mock.patch.object(MODULE, "_mdns_resolution", return_value=[]):
            report = MODULE.collect_diagnostics()
        self.assertFalse(report["layers"]["hostname_resolution"]["resolved"])
        self.assertTrue(any("mDNS name" in item for item in report["problems"]))
        self.assertIn("cannot prove", report["remote_client_note"])

    def test_unavailable_interfaces_and_default_route_are_reported(self):
        with mock.patch.object(MODULE, "collect_interfaces", return_value=[]), \
                mock.patch.object(MODULE, "command_output", side_effect=["[]", "[]", "State Recv-Q Send-Q Local Address:Port Peer Address:Port\n"]):
            report = MODULE.collect_diagnostics()
        self.assertFalse(report["layers"]["network_link"]["healthy"])
        self.assertFalse(report["layers"]["default_route"]["available"])
        self.assertFalse(report["layers"]["listeners"]["healthy"])
        self.assertTrue(any("network interface" in item for item in report["problems"]))
        self.assertTrue(any("default network route" in item for item in report["problems"]))

    def test_failed_obs_proxy_is_distinguished_from_healthy_backend(self):
        def probe(url, **kwargs):
            if ":8080/" in url:
                return {"reachable": False, "status": None}
            return {"reachable": True, "status": 200, "frame_byte_received": True}
        with mock.patch.object(MODULE, "probe_http", side_effect=probe):
            report = MODULE.collect_diagnostics()
        self.assertTrue(report["layers"]["capture_backend"]["healthy"])
        self.assertFalse(report["layers"]["obs_stream_proxy"]["reachable"])
        self.assertTrue(any("OBS stream proxy" in item for item in report["problems"]))

    def test_custom_backend_port_is_used_for_probe_and_listener_check(self):
        with mock.patch.object(MODULE, "parse_config", return_value={"PORT": "8082"}), \
                mock.patch.object(MODULE, "command_output", side_effect=[
                    "[]", "[]",
                    "State Recv-Q Send-Q Local Address:Port Peer Address:Port\nLISTEN 0 128 0.0.0.0:8080 0.0.0.0:*\nLISTEN 0 128 0.0.0.0:8443 0.0.0.0:*\nLISTEN 0 128 127.0.0.1:8082 0.0.0.0:*\n",
                ]), \
                mock.patch.object(MODULE, "probe_http", return_value={"reachable": True, "status": 200, "frame_byte_received": True}):
            report = MODULE.collect_diagnostics()
        self.assertEqual(report["backend_port"], 8082)
        self.assertTrue(report["layers"]["listeners"]["ports"]["8082"]["listening"])

    def test_support_bundle_redacts_credentials_and_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "petcam.conf"
            config.write_text("SOURCE=usb\nWIFI_PASSWORD=topsecret\nACCESS_TOKEN=private-token\n")
            with mock.patch.object(MODULE, "CONFIG_FILE", config), \
                    mock.patch.object(MODULE, "collect_diagnostics", return_value={"ok": True}), \
                    mock.patch.object(MODULE, "command_output", side_effect=[
                        "password=another-secret\n",
                        "Authorization: Bearer journal-secret\n",
                    ]):
                bundle = MODULE.create_support_bundle()
        with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as archive:
            contents = "\n".join(
                archive.extractfile(member).read().decode()
                for member in archive.getmembers()
            )
        for secret in ("topsecret", "private-token", "another-secret", "journal-secret"):
            self.assertNotIn(secret, contents)
        self.assertIn("[REDACTED]", contents)


if __name__ == "__main__":
    unittest.main()
