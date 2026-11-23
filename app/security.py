from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
import hashlib
import hmac
import json
import secrets
import time
from typing import Any


PASSWORD_SCHEME = "scrypt-v1"
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1


class InvalidSessionToken(ValueError):
    pass


def _b64encode(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("password_must_have_at_least_12_characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=32,
    )
    return "$".join(
        [
            PASSWORD_SCHEME,
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            _b64encode(salt),
            _b64encode(digest),
        ]
    )


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        scheme, n, r, p, salt, expected = encoded.split("$", 5)
        if scheme != PASSWORD_SCHEME:
            return False
        parsed = (int(n), int(r), int(p))
        if parsed != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_b64decode(salt),
            n=parsed[0],
            r=parsed[1],
            p=parsed[2],
            dklen=32,
        )
        return hmac.compare_digest(digest, _b64decode(expected))
    except (TypeError, ValueError):
        return False


def create_session_token(
    *,
    student_id: int,
    secret: str,
    ttl_seconds: int,
    session_version: int = 0,
    now: int | None = None,
) -> tuple[str, str]:
    issued_at = int(time.time()) if now is None else now
    csrf_token = secrets.token_urlsafe(32)
    payload = {
        "sid": student_id,
        "ver": session_version,
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
        "csrf": csrf_token,
        "nonce": secrets.token_urlsafe(12),
    }
    encoded = _b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = _b64encode(
        hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{encoded}.{signature}", csrf_token


def decode_session_token(
    token: str, *, secret: str, now: int | None = None
) -> dict[str, Any]:
    try:
        encoded, supplied_signature = token.split(".", 1)
        expected_signature = _b64encode(
            hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise InvalidSessionToken("invalid_signature")
        payload = json.loads(_b64decode(encoded))
        current = int(time.time()) if now is None else now
        if not isinstance(payload.get("sid"), int) or payload.get("sid", 0) <= 0:
            raise InvalidSessionToken("invalid_subject")
        if not isinstance(payload.get("ver"), int) or payload["ver"] < 0:
            raise InvalidSessionToken("invalid_session_version")
        if not isinstance(payload.get("exp"), int) or payload["exp"] <= current:
            raise InvalidSessionToken("session_expired")
        if not isinstance(payload.get("csrf"), str) or not payload["csrf"]:
            raise InvalidSessionToken("invalid_csrf")
        return payload
    except InvalidSessionToken:
        raise
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise InvalidSessionToken("malformed_session") from exc
