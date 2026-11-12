import http.client
import importlib.util
import json
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

LIB = Path("stage-petcam/00-install-files/files/usr/local/lib/petcam")
import sys
sys.path.insert(0, str(LIB))
import web_auth

SPEC = importlib.util.spec_from_file_location("petcam_web_manager_auth_tests", LIB / "web_manager.py")
MANAGER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MANAGER)


class WebManagerRouteAuthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.cert = cls.root / "cert.pem"
        cls.key = cls.root / "key.pem"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=localhost", "-keyout", str(cls.key), "-out", str(cls.cert),
        ], check=True, capture_output=True)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MANAGER.Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cls.cert, cls.key)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.client_context = ssl._create_unverified_context()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.temp.cleanup()

    def setUp(self):
        self.auth_dir = tempfile.TemporaryDirectory()
        self.auth_patch = mock.patch.object(web_auth, "AUTH_FILE", Path(self.auth_dir.name) / "web-auth.json")
        self.auth_patch.start()
        with web_auth._lock:
            web_auth._sessions.clear()
        web_auth.save_password("correct horse battery staple")

    def tearDown(self):
        self.auth_patch.stop()
        self.auth_dir.cleanup()

    def request(self, method, path, body=None, cookie=None, csrf=None, origin=True):
        connection = http.client.HTTPSConnection(
            "127.0.0.1", self.port, context=self.client_context, timeout=5
        )
        headers = {"Host": f"127.0.0.1:{self.port}"}
        if origin:
            headers["Origin"] = f"https://127.0.0.1:{self.port}"
        if cookie:
            headers["Cookie"] = cookie
        if csrf:
            headers["X-PetCam-CSRF"] = csrf
        encoded = json.dumps(body).encode() if body is not None else None
        if encoded is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(encoded))
        connection.request(method, path, body=encoded, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        result = (response.status, response.getheaders(), payload)
        connection.close()
        return result

    def login(self):
        status, headers, payload = self.request(
            "POST", "/api/login", {"password": "correct horse battery staple"}
        )
        self.assertEqual(status, 200)
        cookie = next(value for key, value in headers if key.lower() == "set-cookie").split(";", 1)[0]
        csrf = json.loads(payload)["csrf_token"]
        return cookie, csrf

    def test_management_api_rejects_unauthenticated_clients(self):
        status, _, payload = self.request("GET", "/api/cameras")
        self.assertEqual(status, 401)
        self.assertIn(b"Authentication required", payload)

    def test_login_requires_same_origin_and_sets_secure_http_only_cookie(self):
        status, _, _ = self.request(
            "POST", "/api/login", {"password": "correct horse battery staple"}, origin=False
        )
        self.assertEqual(status, 403)
        status, headers, _ = self.request(
            "POST", "/api/login", {"password": "correct horse battery staple"}
        )
        self.assertEqual(status, 200)
        cookie = next(value for key, value in headers if key.lower() == "set-cookie")
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_existing_session_can_restore_csrf_token_after_page_reload(self):
        cookie, csrf = self.login()
        status, _, payload = self.request("GET", "/api/session", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["csrf_token"], csrf)

    def test_health_api_requires_authentication_and_returns_monitor_state(self):
        status, _, _ = self.request("GET", "/api/health")
        self.assertEqual(status, 401)
        cookie, _ = self.login()
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "health.json"
            state_file.write_text(json.dumps({"state": "camera_disconnected", "reason": "No camera", "recovery_count": 2}))
            with mock.patch.object(MANAGER, "HEALTH_FILE", state_file):
                status, _, payload = self.request("GET", "/api/health", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["recovery_count"], 2)

    def test_network_diagnostics_api_requires_authentication(self):
        status, _, _ = self.request("GET", "/api/diagnostics")
        self.assertEqual(status, 401)
        cookie, _ = self.login()
        with mock.patch.object(MANAGER, "collect_diagnostics", return_value={"problems": []}):
            status, _, payload = self.request("GET", "/api/diagnostics", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload), {"problems": []})

    def test_support_bundle_endpoint_requires_authentication_and_returns_archive(self):
        status, _, _ = self.request("GET", "/api/support-bundle")
        self.assertEqual(status, 401)
        cookie, _ = self.login()
        with mock.patch.object(MANAGER, "create_support_bundle", return_value=b"test archive"):
            status, headers, payload = self.request("GET", "/api/support-bundle", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(payload, b"test archive")
        self.assertTrue(any(key.lower() == "content-type" and value == "application/gzip" for key, value in headers))
        self.assertTrue(any(
            key.lower() == "content-disposition" and "petcam-support.tar.gz" in value
            for key, value in headers
        ))

    def test_state_changes_require_csrf_and_valid_session(self):
        cookie, csrf = self.login()
        with mock.patch.object(MANAGER, "save_selection") as save:
            status, _, _ = self.request(
                "POST", "/api/select", {"kind": "csi"}, cookie=cookie
            )
            self.assertEqual(status, 403)
            save.assert_not_called()
            status, _, _ = self.request(
                "POST", "/api/select", {"kind": "csi"}, cookie=cookie, csrf=csrf
            )
            self.assertEqual(status, 200)
            save.assert_called_once()

    def test_logout_and_expiry_invalidate_sessions(self):
        cookie, csrf = self.login()
        status, _, _ = self.request("POST", "/api/logout", {}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200)
        status, _, _ = self.request("GET", "/api/cameras", cookie=cookie)
        self.assertEqual(status, 401)

        expired_token, _ = web_auth.new_session(now=time.time() - web_auth.SESSION_TTL - 1)
        status, _, _ = self.request("GET", "/api/cameras", cookie=f"petcam_session={expired_token}")
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
