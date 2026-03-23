from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any


def hash_password(password: str, iterations: int = 210000) -> str:
    salt = base64.urlsafe_b64encode(os.urandom(16)).decode("utf-8").rstrip("=")
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    )
    encoded = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
    return f"pbkdf2_sha256${iterations}${salt}${encoded}"


def verify_password(password: str, stored_hash: str) -> bool:
    if not stored_hash:
        return False

    # Bootstrap mode: allow plain text secret for manual setup.
    if stored_hash.startswith("plain$"):
        return hmac.compare_digest(password, stored_hash[6:])

    parts = stored_hash.split("$")
    if len(parts) == 4 and parts[0] == "pbkdf2_sha256":
        try:
            iterations = int(parts[1])
            salt = parts[2]
            expected = parts[3]
        except ValueError:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            iterations,
        )
        candidate = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
        return hmac.compare_digest(candidate, expected)

    # Legacy fallback if caller stored raw string.
    return hmac.compare_digest(password, stored_hash)


def make_session_token(user_id: int, email: str, secret: str, ttl_hours: int) -> str:
    now = int(time.time())
    payload = {
        "uid": int(user_id),
        "email": str(email),
        "iat": now,
        "exp": now + max(ttl_hours, 1) * 3600,
    }
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = _b64_encode(payload_json)
    signature = hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), digestmod=hashlib.sha256).hexdigest()
    return f"{payload_b64}.{signature}"


def parse_session_token(token: str, secret: str) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None

    payload_b64, provided_sig = token.split(".", 1)
    expected_sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), digestmod=hashlib.sha256).hexdigest()
    if not hmac.compare_digest(provided_sig, expected_sig):
        return None

    try:
        payload_bytes = _b64_decode(payload_b64)
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None

    exp = int(payload.get("exp", 0) or 0)
    if exp <= int(time.time()):
        return None

    return payload


def _b64_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("utf-8").rstrip("=")


def _b64_decode(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("utf-8"))
