import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AUTH_MODULE_PATH = Path(
    "stage-petcam/00-install-files/files/usr/local/lib/petcam/web_auth.py"
)
SPEC = importlib.util.spec_from_file_location("petcam_web_auth", AUTH_MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class WebAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.auth_file = Path(self.directory.name) / "web-auth.json"
        self.file_patch = mock.patch.object(MODULE, "AUTH_FILE", self.auth_file)
        self.file_patch.start()
        with MODULE._lock:
            MODULE._sessions.clear()

    def tearDown(self):
        self.file_patch.stop()
        self.directory.cleanup()

    def test_first_run_setup_creates_password_once(self):
        self.assertFalse(MODULE.is_configured())
        MODULE.initialize_password("first administrator password")
        self.assertTrue(MODULE.is_configured())
        self.assertTrue(MODULE.verify_password("first administrator password"))

    def test_first_run_setup_cannot_replace_existing_password(self):
        MODULE.initialize_password("first administrator password")
        with self.assertRaisesRegex(ValueError, "already been completed"):
            MODULE.initialize_password("replacement administrator password")
        self.assertTrue(MODULE.verify_password("first administrator password"))
        self.assertFalse(MODULE.verify_password("replacement administrator password"))

    def test_first_run_setup_rejects_weak_password_without_creating_auth_file(self):
        with self.assertRaisesRegex(ValueError, "at least 12"):
            MODULE.initialize_password("too short")
        self.assertFalse(MODULE.is_configured())
        self.assertFalse(self.auth_file.exists())

    def test_password_hash_is_salted_and_not_plaintext(self):
        first = MODULE.hash_password("correct horse battery staple")
        second = MODULE.hash_password("correct horse battery staple")
        self.assertNotEqual(first["salt"], second["salt"])
        self.assertNotEqual(first["password_hash"], "correct horse battery staple")
        self.assertNotIn("correct horse battery staple", str(first))

    def test_password_verification_survives_process_style_reload(self):
        MODULE.save_password("correct horse battery staple")
        self.assertTrue(MODULE.verify_password("correct horse battery staple"))
        self.assertFalse(MODULE.verify_password("incorrect password"))
        self.assertNotIn("correct horse battery staple", self.auth_file.read_text())

    def test_password_requires_minimum_length(self):
        with self.assertRaisesRegex(ValueError, "at least 12"):
            MODULE.hash_password("short")

    def test_sessions_expire_after_eight_hours(self):
        token, csrf = MODULE.new_session(now=1000)
        self.assertEqual(MODULE.get_session(token, now=1001)["csrf"], csrf)
        self.assertIsNone(MODULE.get_session(token, now=1000 + MODULE.SESSION_TTL))

    def test_password_change_invalidates_all_existing_sessions(self):
        MODULE.save_password("first password value")
        token, _ = MODULE.new_session(now=1000)
        MODULE.save_password("second password value")
        self.assertIsNone(MODULE.get_session(token, now=1001))
        self.assertTrue(MODULE.verify_password("second password value"))
        self.assertFalse(MODULE.verify_password("first password value"))

    def test_expired_sessions_are_pruned(self):
        old, _ = MODULE.new_session(now=1000)
        fresh, _ = MODULE.new_session(now=2000)
        MODULE.prune_sessions(now=1000 + MODULE.SESSION_TTL)
        self.assertIsNone(MODULE.get_session(old, now=1000 + MODULE.SESSION_TTL))
        self.assertIsNotNone(MODULE.get_session(fresh, now=2000 + 1))


if __name__ == "__main__":
    unittest.main()
