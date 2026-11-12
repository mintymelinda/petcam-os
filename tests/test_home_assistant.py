import importlib.util
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(
    "stage-petcam/00-install-files/files/usr/local/lib/petcam/home_assistant.py"
)
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("petcam_home_assistant", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeSocket:
    def __init__(self, incoming=b"\x20\x02\x00\x00"):
        self.incoming = bytearray(incoming)
        self.sent = []
        self.closed = False
        self.timeout = None

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, value):
        self.sent.append(bytes(value))

    def recv(self, count):
        value = bytes(self.incoming[:count])
        del self.incoming[:count]
        return value

    def close(self):
        self.closed = True


class FakePublisher:
    def __init__(self):
        self.messages = []
        self.discovery = None

    def _topic(self, suffix):
        return f"petcam/test/{suffix}"

    def publish(self, topic, payload, retain=True):
        self.messages.append((topic, payload, retain))

    def connect(self):
        pass

    def close(self, graceful=True):
        pass

    def publish_availability(self, state):
        self.publish(self._topic("broker"), state)

    def publish_discovery(self, node_id, hostname, model, entities):
        self.discovery = (node_id, hostname, model, entities)

    def publish_state(self, snapshot):
        for key, value in snapshot.items():
            self.publish(self._topic(f"state/{key}"), value)


class HomeAssistantMQTTTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config_file = Path(self.directory.name) / "home-assistant.conf"

    def tearDown(self):
        self.directory.cleanup()

    def test_missing_configuration_disables_integration(self):
        config = MODULE.load_config(self.config_file)
        self.assertFalse(config["MQTT_ENABLED"])
        self.assertFalse(config["MQTT_TLS"])
        self.assertFalse(config["MQTT_PUBLISH_STREAM_URL"])

    def test_loads_optional_mqtt_credentials_and_tls(self):
        self.config_file.write_text(
            "MQTT_ENABLED=true\nMQTT_HOST=mqtt.local\nMQTT_PORT=8883\n"
            "MQTT_USERNAME=petcam\nMQTT_PASSWORD=secret\nMQTT_TLS=true\n"
        )
        config = MODULE.load_config(self.config_file)
        self.assertTrue(config["MQTT_ENABLED"])
        self.assertTrue(config["MQTT_TLS"])
        self.assertEqual(config["MQTT_PORT"], 8883)
        self.assertEqual(config["MQTT_HOST"], "mqtt.local")

    def test_rejects_invalid_settings_and_secret_bearing_stream_urls(self):
        cases = [
            ("MQTT_ENABLED=true\n", "MQTT_HOST is required"),
            ("MQTT_ENABLED=true\nMQTT_HOST=mqtt.local\nMQTT_PORT=0\n", "MQTT_PORT"),
            ("MQTT_ENABLED=true\nMQTT_HOST=mqtt.local\nUNKNOWN=value\n", "Unknown"),
            ("MQTT_ENABLED=true\nMQTT_HOST=mqtt.local\nMQTT_PASSWORD=secret\n", "requires MQTT_USERNAME"),
            ("MQTT_PUBLISH_STREAM_URL=true\nMQTT_STREAM_URL=http://user:pass@camera.local/stream\n", "must not contain credentials"),
            ("MQTT_PUBLISH_STREAM_URL=true\nMQTT_STREAM_URL=http://camera.local/stream?token=x\n", "must not contain credentials"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.config_file.write_text(value)
                with self.assertRaisesRegex(MODULE.MQTTError, expected):
                    MODULE.load_config(self.config_file)

    def test_stream_url_is_not_published_without_explicit_opt_in(self):
        config = MODULE.load_config(self.config_file)
        self.assertFalse(config["MQTT_PUBLISH_STREAM_URL"])
        self.assertEqual(config["MQTT_STREAM_URL"], "")

    def test_mqtt_remaining_length_encoding(self):
        self.assertEqual(MODULE._remaining_length(0), b"\x00")
        self.assertEqual(MODULE._remaining_length(127), b"\x7f")
        self.assertEqual(MODULE._remaining_length(128), b"\x80\x01")
        self.assertEqual(MODULE._remaining_length(321), b"\xc1\x02")
        with self.assertRaises(MODULE.MQTTError):
            MODULE._remaining_length(268435456)

    def test_node_identifier_is_safe_for_mqtt_topics(self):
        config = {**MODULE.DEFAULTS, "MQTT_CLIENT_ID": "petcam camera/one"}
        self.assertEqual(MODULE._node_identifier(config), "petcam_camera_one")

    def test_connect_uses_retained_last_will_and_optional_credentials(self):
        fake = FakeSocket()
        config = {
            **MODULE.DEFAULTS,
            "MQTT_ENABLED": True,
            "MQTT_TLS": False,
            "MQTT_HOST": "broker.local",
            "MQTT_PORT": 1883,
            "MQTT_USERNAME": "petcam",
            "MQTT_PASSWORD": "secret",
            "MQTT_CLIENT_ID": "petcam-test",
            "MQTT_TOPIC_PREFIX": "petcam",
        }
        with mock.patch.object(MODULE.socket, "create_connection", return_value=fake):
            client = MODULE.MQTTClient(config)
            client.connect()
        connect_packet = fake.sent[0]
        self.assertEqual(connect_packet[0], 0x10)
        self.assertIn(b"petcam/petcam-test/broker", connect_packet)
        self.assertIn(b"offline", connect_packet)
        self.assertIn(b"petcam", connect_packet)
        self.assertIn(b"secret", connect_packet)
        client.close()
        self.assertTrue(fake.closed)

    def test_discovery_payloads_include_health_and_broker_availability(self):
        config = {
            **MODULE.DEFAULTS,
            "MQTT_DISCOVERY_PREFIX": "homeassistant",
            "MQTT_TOPIC_PREFIX": "petcam",
        }
        fake = FakeSocket(incoming=b"")
        client = MODULE.MQTTClient(config)
        client.connection = fake
        entities = [MODULE.ENTITIES[0], MODULE.ENTITIES[2]]
        client.publish_discovery("petcam_test", "petcam", "Raspberry Pi", entities)
        self.assertEqual(len(fake.sent), 2)
        self.assertIn(b"availability_mode", fake.sent[0])
        self.assertIn(b"health_availability", fake.sent[0])
        self.assertIn(b"petcam_test", fake.sent[0])
        self.assertIn(b"payload_on", fake.sent[0])

    def test_collect_snapshot_uses_actual_health_and_service_state(self):
        config = {
            "SOURCE": "auto",
            "RESOLUTION": "1280x720",
            "FPS": "30",
            "FORMAT": "MJPG",
            "MQTT_PUBLISH_STREAM_URL": True,
            "MQTT_STREAM_URL": "http://petcam.local:8080/stream",
        }
        completed = mock.Mock(returncode=0, stdout="active\n", stderr="")
        with mock.patch.object(MODULE.subprocess, "run", side_effect=[
            completed, mock.Mock(returncode=0, stdout="PetCam backend: usb\n", stderr="")
        ]):
            snapshot = MODULE.collect_snapshot(config, {
                "state": "healthy",
                "camera_connected": True,
                "backend_stream_ready": True,
                "public_stream_ready": True,
            })
        self.assertEqual(snapshot["camera_connected"], "ON")
        self.assertEqual(snapshot["stream_ready"], "ON")
        self.assertEqual(snapshot["health_available"], "ON")
        self.assertEqual(snapshot["active_backend"], "usb")
        self.assertEqual(snapshot["active_mode"], "1280x720 @ 30 fps · MJPG")
        self.assertEqual(snapshot["stream_url"], "http://petcam.local:8080/stream")

    def test_disconnected_camera_remains_available_but_stream_is_not_ready(self):
        config = {"SOURCE": "usb", "RESOLUTION": "auto", "FPS": "auto", "FORMAT": "auto"}
        with mock.patch.object(MODULE.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="active\n")):
            snapshot = MODULE.collect_snapshot(config, {
                "state": "camera_disconnected",
                "camera_connected": False,
                "backend_stream_ready": False,
                "public_stream_ready": False,
            })
        self.assertEqual(snapshot["health_available"], "ON")
        self.assertEqual(snapshot["camera_connected"], "OFF")
        self.assertEqual(snapshot["stream_ready"], "OFF")

    def test_publish_once_omits_private_stream_url_by_default(self):
        fake = FakePublisher()
        config = {
            **MODULE.DEFAULTS,
            "MQTT_PUBLISH_STREAM_URL": False,
            "MQTT_DISCOVERY_PREFIX": "homeassistant",
            "MQTT_TOPIC_PREFIX": "petcam",
            "MQTT_CLIENT_ID": "petcam-test",
        }
        with mock.patch.object(MODULE, "parse_config", return_value={"SOURCE": "usb"}), \
                mock.patch.object(MODULE.socket, "gethostname", return_value="petcam"), \
                mock.patch.object(MODULE.Path, "read_text", side_effect=OSError), \
                mock.patch.object(MODULE.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="")):
            MODULE.publish_once(fake, config)
        self.assertNotIn("stream_url", [item["object_id"] for item in fake.discovery[3]])
        self.assertNotIn("stream_url", [topic.rsplit("/", 1)[-1] for topic, _, _ in fake.messages if "/state/" in topic])
        self.assertIn(("petcam/test/health_availability", "unhealthy", True), fake.messages)

    def test_remove_discovery_clears_all_retained_entities(self):
        fake = FakePublisher()
        config = {**MODULE.DEFAULTS, "MQTT_HOST": "broker.local"}
        with mock.patch.object(MODULE, "MQTTClient", return_value=fake):
            MODULE.remove_discovery(config)
        cleared = [topic for topic, payload, retain in fake.messages if payload == "" and retain]
        self.assertEqual(len(cleared), len(MODULE.ENTITIES) * 2)
        self.assertIn(("petcam/test/health_availability", "unhealthy", True), fake.messages)
        self.assertIn(("petcam/test/broker", "offline", True), fake.messages)

    def test_reconnects_after_broker_loss_with_bounded_backoff(self):
        attempts = {"connect": 0, "ping": 0, "sleep": 0}
        stopped = {"value": False}
        clients = []

        class Client:
            def __init__(self, config):
                clients.append(self)

            def connect(self):
                attempts["connect"] += 1
                if attempts["connect"] == 1:
                    raise OSError("broker unavailable")

            def publish_availability(self, state):
                pass

            def ping(self):
                attempts["ping"] += 1
                if attempts["ping"] == 1:
                    raise OSError("broker disconnected")

            def close(self, graceful=True):
                pass

        def sleep(_seconds):
            attempts["sleep"] += 1
            if attempts["sleep"] >= 3:
                stopped["value"] = True

        config = {**MODULE.DEFAULTS, "MQTT_ENABLED": True}
        with mock.patch.object(MODULE, "MQTTClient", Client), \
                mock.patch.object(MODULE, "publish_once"):
            self.assertEqual(
                MODULE.run(config, stop=lambda: stopped["value"], sleep=sleep),
                0,
            )
        self.assertEqual(attempts["connect"], 2)
        self.assertEqual(attempts["ping"], 1)
        self.assertEqual(len(clients), 2)


if __name__ == "__main__":
    unittest.main()
