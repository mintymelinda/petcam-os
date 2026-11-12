"""Password hashing and in-memory sessions for the PetCam web manager."""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

AUTH_FILE = Path("/etc/petcam/web-auth.json")
SESSION_TTL = 8 * 60 * 60
PBKDF2_ITERATIONS = 310_000
_sessions = {}
_lock = threading.RLock()


def hash_password(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise ValueError("Password must be at least 12 characters and no more than 1024 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return {"algorithm": "pbkdf2_sha256", "iterations": PBKDF2_ITERATIONS,
            "salt": salt.hex(), "password_hash": digest.hex()}


def load_auth():
    try:
        data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        if data.get("algorithm") == "pbkdf2_sha256":
            return data
    except (OSError, ValueError, TypeError):
        pass
    return None


def verify_password(password):
    auth = load_auth()
    if not auth or not isinstance(password, str):
        return False
    try:
        salt = bytes.fromhex(auth["salt"])
        expected = bytes.fromhex(auth["password_hash"])
        iterations = int(auth["iterations"])
        if len(salt) != 16 or len(expected) != 32 or not 100_000 <= iterations <= 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except (KeyError, TypeError, ValueError):
        return False


def is_configured():
    """Return whether first-run administrator setup has already completed."""
    return AUTH_FILE.exists()


def initialize_password(password):
    """Create the first administrator password without replacing an existing one."""
    data = hash_password(password)
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data) + "\\n"
    with _lock:
        try:
            descriptor = os.open(AUTH_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        except FileExistsError as error:
            raise ValueError("PetCam administrator setup has already been completed.") from error
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(AUTH_FILE, 0o640)
            try:
                import grp
                os.chown(AUTH_FILE, 0, grp.getgrnam("petcam").gr_gid)
            except (ImportError, KeyError, PermissionError):
                pass
            _sessions.clear()
        except Exception:
            try:
                AUTH_FILE.unlink()
            except OSError:
                pass
            raise


def save_password(password):
    data = hash_password(password)
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = AUTH_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o640)
    os.replace(temporary, AUTH_FILE)
    try:
        import grp
        os.chown(AUTH_FILE, 0, grp.getgrnam("petcam").gr_gid)
    except (ImportError, KeyError, PermissionError):
        pass
    with _lock:
        _sessions.clear()


def new_session(now=None):
    now = time.time() if now is None else now
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    with _lock:
        prune_sessions(now)
        _sessions[token] = {"csrf": csrf, "expires_at": now + SESSION_TTL}
    return token, csrf


def prune_sessions(now=None):
    now = time.time() if now is None else now
    with _lock:
        for token, session in list(_sessions.items()):
            if session["expires_at"] <= now:
                del _sessions[token]


def get_session(token, now=None):
    if not token:
        return None
    prune_sessions(now)
    with _lock:
        return _sessions.get(token)


def invalidate_session(token):
    with _lock:
        _sessions.pop(token, None)
