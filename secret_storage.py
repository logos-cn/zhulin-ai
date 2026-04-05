from __future__ import annotations

import base64
import hashlib
from typing import Optional

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError as exc:  # pragma: no cover - dependency guard
    Fernet = None
    InvalidToken = Exception
    CRYPTOGRAPHY_IMPORT_ERROR = exc
else:
    CRYPTOGRAPHY_IMPORT_ERROR = None

from config import settings


SECRET_PREFIX = "enc:v1:"


class SecretStorageError(RuntimeError):
    pass


def ensure_secret_storage_dependencies() -> None:
    if CRYPTOGRAPHY_IMPORT_ERROR is not None:
        raise SecretStorageError(
            "Missing required package: cryptography. Install it with `pip install cryptography`."
        )


def _primary_secret_seed() -> str:
    seed = settings.ai_config_secret_key
    if not seed:
        raise SecretStorageError("AI config secret key is not configured.")
    return seed


def _secret_seed_candidates() -> tuple[str, ...]:
    seeds: list[str] = []
    for candidate in (settings.ai_config_secret_key, settings.jwt_secret_key):
        seed = str(candidate or "").strip()
        if seed and seed not in seeds:
            seeds.append(seed)
    if not seeds:
        raise SecretStorageError("AI config secret key is not configured.")
    return tuple(seeds)


def _fernet_for_seed(seed: str) -> Fernet:
    ensure_secret_storage_dependencies()
    derived_key = base64.urlsafe_b64encode(hashlib.sha256(seed.encode("utf-8")).digest())
    return Fernet(derived_key)


def _fernet() -> Fernet:
    return _fernet_for_seed(_primary_secret_seed())


def is_encrypted_secret(value: Optional[str]) -> bool:
    return bool(value and str(value).startswith(SECRET_PREFIX))


def encrypt_secret(value: Optional[str]) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    if is_encrypted_secret(text):
        return text
    token = _fernet().encrypt(text.encode("utf-8")).decode("utf-8")
    return f"{SECRET_PREFIX}{token}"


def decrypt_secret(value: Optional[str]) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    if not is_encrypted_secret(text):
        return text
    token = text[len(SECRET_PREFIX):]
    encrypted_value = token.encode("utf-8")
    for seed in _secret_seed_candidates():
        try:
            return _fernet_for_seed(seed).decrypt(encrypted_value).decode("utf-8")
        except InvalidToken:
            continue
    raise SecretStorageError("Stored secret could not be decrypted with the current secret key.")
