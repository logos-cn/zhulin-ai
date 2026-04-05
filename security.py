from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    import bcrypt
except ImportError as exc:  # pragma: no cover - dependency guard
    bcrypt = None
    BCRYPT_IMPORT_ERROR = exc
else:
    BCRYPT_IMPORT_ERROR = None

from config import settings


class TokenError(ValueError):
    pass


PASSWORD_TIMING_PADDING_HASH = (
    "$2b$12$jjlU8Q28a4n.6jORbFd0xO7x2r5EbFLroP/aS6Wto/OMBLjNh8liq"
)
RESERVED_TOKEN_CLAIMS = frozenset({"sub", "exp", "iat", "nbf", "iss", "aud", "jti"})


def ensure_security_dependencies() -> None:
    if BCRYPT_IMPORT_ERROR is not None:
        raise RuntimeError(
            "Missing required package: bcrypt. Install it with `pip install bcrypt`."
        )


def hash_password(raw_password: str) -> str:
    ensure_security_dependencies()
    if not raw_password:
        raise ValueError("Password cannot be empty.")
    return bcrypt.hashpw(raw_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(raw_password: str, password_hash: str) -> bool:
    ensure_security_dependencies()
    if not raw_password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(raw_password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _json_dumps(data: dict[str, Any]) -> bytes:
    return json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sign(signing_input: bytes, secret_key: str, algorithm: str) -> bytes:
    if algorithm != "HS256":
        raise TokenError("Only HS256 JWT signing is currently supported.")
    return hmac.new(secret_key.encode("utf-8"), signing_input, hashlib.sha256).digest()


def create_access_token(
    subject: str,
    extra_claims: dict[str, Any] | None = None,
    expires_delta: timedelta | None = None,
) -> str:
    if extra_claims:
        overlapping_claims = RESERVED_TOKEN_CLAIMS.intersection(extra_claims)
        if overlapping_claims:
            reserved_text = ", ".join(sorted(overlapping_claims))
            raise ValueError(f"extra_claims may not override reserved claims: {reserved_text}")

    now = datetime.now(timezone.utc)
    expires_at = now + (expires_delta or timedelta(minutes=settings.access_token_expire_minutes))

    header = {"alg": settings.jwt_algorithm, "typ": "JWT"}
    payload: dict[str, Any] = {
        "sub": str(subject),
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    if extra_claims:
        payload.update(extra_claims)

    encoded_header = _b64url_encode(_json_dumps(header))
    encoded_payload = _b64url_encode(_json_dumps(payload))
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    signature = _b64url_encode(_sign(signing_input, settings.jwt_secret_key, settings.jwt_algorithm))
    return f"{encoded_header}.{encoded_payload}.{signature}"


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        encoded_header, encoded_payload, encoded_signature = token.split(".")
    except ValueError as exc:
        raise TokenError("Malformed token.") from exc

    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    expected_signature = _sign(signing_input, settings.jwt_secret_key, settings.jwt_algorithm)

    try:
        signature = _b64url_decode(encoded_signature)
    except (ValueError, TypeError) as exc:
        raise TokenError("Malformed token signature.") from exc

    if not hmac.compare_digest(signature, expected_signature):
        raise TokenError("Invalid token signature.")

    try:
        header = json.loads(_b64url_decode(encoded_header))
        payload = json.loads(_b64url_decode(encoded_payload))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise TokenError("Malformed token payload.") from exc

    if header.get("alg") != settings.jwt_algorithm:
        raise TokenError("Unexpected token algorithm.")

    exp = payload.get("exp")
    if exp is None:
        raise TokenError("Token is missing exp claim.")

    try:
        exp_timestamp = int(exp)
    except (TypeError, ValueError) as exc:
        raise TokenError("Invalid exp claim.") from exc

    now_timestamp = int(datetime.now(timezone.utc).timestamp())
    if exp_timestamp <= now_timestamp:
        raise TokenError("Token has expired.")

    return payload
