"""Password hashing, API keys, credential encryption and signed tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import get_settings

# --- Passwords -------------------------------------------------------------

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
MIN_PASSWORD_LENGTH = 10


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N,
        _SCRYPT_R,
        _SCRYPT_P,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    salt = base64.b64decode(salt_b64)
    expected = base64.b64decode(digest_b64)
    actual = hashlib.scrypt(password.encode(), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected))
    return hmac.compare_digest(actual, expected)


def password_problem(password: str) -> str | None:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    return None


class LoginThrottle:
    """In-process limiter for failed logins per (client, email). Use a shared store if you run many replicas."""

    def __init__(self, max_failures: int = 8, window_seconds: int = 900):
        self.max_failures = max_failures
        self.window = window_seconds
        self._failures: dict[str, list[float]] = {}

    def _recent(self, key: str) -> list[float]:
        now = time.monotonic()
        recent = [t for t in self._failures.get(key, []) if now - t < self.window]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def blocked(self, key: str) -> bool:
        return len(self._recent(key)) >= self.max_failures

    def failed(self, key: str) -> None:
        self._recent(key)
        self._failures.setdefault(key, []).append(time.monotonic())
        if len(self._failures) > 50_000:  # bound memory under credential-stuffing
            self._failures.clear()

    def succeeded(self, key: str) -> None:
        self._failures.pop(key, None)


login_throttle = LoginThrottle()


# --- API keys and opaque tokens -------------------------------------------

API_KEY_PREFIX = "so_live_"


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """Return (plaintext key, display prefix, hash). Only the hash is stored."""
    key = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return key, key[: len(API_KEY_PREFIX) + 6], sha256_hex(key)


def generate_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, sha256_hex(token)


# --- Credential encryption -------------------------------------------------


def _fernet() -> Fernet:
    return Fernet(get_settings().encryption_key.encode())


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:  # pragma: no cover - indicates key rotation problems
        raise ValueError("Stored credentials cannot be decrypted with the configured ENCRYPTION_KEY") from exc


def generate_encryption_key() -> str:
    return Fernet.generate_key().decode()


# --- Signed, expiring tokens (password reset) ------------------------------


def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt=salt)


def sign_reset_token(user_id: int, password_hash: str) -> str:
    # Binding to the current hash makes the token single-use: it dies once the password changes.
    return _serializer("password-reset").dumps({"uid": user_id, "ph": password_hash[-16:]})


def load_reset_token(token: str, max_age_seconds: int = 3600) -> dict | None:
    try:
        return _serializer("password-reset").loads(token, max_age=max_age_seconds)
    except (BadSignature, SignatureExpired):
        return None


# --- CSRF -----------------------------------------------------------------


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def csrf_matches(expected: str | None, provided: str | None) -> bool:
    return bool(expected) and bool(provided) and hmac.compare_digest(expected, provided)
