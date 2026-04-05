from __future__ import annotations

from dataclasses import replace
import os
import unittest
from unittest.mock import patch

import config
import secret_storage
from security import create_access_token, decode_access_token


class SecurityTests(unittest.TestCase):
    def test_create_access_token_rejects_reserved_claim_overrides(self) -> None:
        with self.assertRaises(ValueError) as context:
            create_access_token(
                subject="123",
                extra_claims={"sub": "999", "exp": 9999999999},
            )

        self.assertIn("reserved claims", str(context.exception))

    def test_decrypt_secret_accepts_legacy_jwt_shared_key_ciphertext(self) -> None:
        legacy_settings = replace(
            config.settings,
            jwt_secret_key="legacy-jwt-secret",
            ai_config_secret_key="legacy-jwt-secret",
        )
        rotated_settings = replace(
            config.settings,
            jwt_secret_key="legacy-jwt-secret",
            ai_config_secret_key="new-ai-config-secret",
        )

        with patch.object(secret_storage, "settings", legacy_settings):
            encrypted = secret_storage.encrypt_secret("stored-key")

        with patch.object(secret_storage, "settings", rotated_settings):
            decrypted = secret_storage.decrypt_secret(encrypted)

        self.assertEqual(decrypted, "stored-key")

    def test_load_settings_uses_distinct_default_ai_secret(self) -> None:
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "development",
                "JWT_SECRET_KEY": "jwt-only-secret",
            },
            clear=True,
        ):
            loaded = config.load_settings()

        self.assertEqual(loaded.jwt_secret_key, "jwt-only-secret")
        self.assertEqual(loaded.ai_config_secret_key, "change-me-ai-config-secret")
        self.assertNotEqual(loaded.ai_config_secret_key, loaded.jwt_secret_key)

    def test_load_settings_rejects_shared_ai_and_jwt_secret_in_production(self) -> None:
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "JWT_SECRET_KEY": "same-secret",
                "AI_CONFIG_SECRET_KEY": "same-secret",
            },
            clear=True,
        ):
            with self.assertRaises(RuntimeError) as context:
                config.load_settings()

        self.assertIn("must be different", str(context.exception))

    def test_decode_access_token_preserves_subject(self) -> None:
        token = create_access_token(subject="321", extra_claims={"role": "author"})

        payload = decode_access_token(token)

        self.assertEqual(payload["sub"], "321")
        self.assertEqual(payload["role"], "author")


if __name__ == "__main__":
    unittest.main()
