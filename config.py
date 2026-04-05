from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env", override=False)


def _parse_csv(value: str) -> tuple[str, ...]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    return tuple(items or ["*"])


def _get_int_env(name: str, default: int, *, minimum: int | None = None) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or not str(raw_value).strip():
        return default

    try:
        value = int(str(raw_value).strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Environment variable {name} must be an integer, got {raw_value!r}."
        ) from exc

    if minimum is not None and value < minimum:
        raise RuntimeError(
            f"Environment variable {name} must be >= {minimum}, got {value!r}."
        )

    return value


@dataclass(frozen=True)
class Settings:
    app_name: str
    app_env: str
    database_url: str
    jwt_secret_key: str
    ai_config_secret_key: str
    jwt_algorithm: str
    access_token_expire_minutes: int
    cors_allow_origins: tuple[str, ...]
    log_level: str
    log_dir: str
    log_max_bytes: int
    log_backup_count: int
    max_world_import_bytes: int
    login_rate_window_seconds: int
    login_rate_max_attempts: int
    login_lockout_seconds: int
    max_running_world_jobs_per_user: int
    max_running_world_jobs_per_book: int
    max_running_world_jobs_global: int


def load_settings() -> Settings:
    jwt_secret_key = os.getenv("JWT_SECRET_KEY", "change-me-in-production")
    ai_config_secret_key = os.getenv("AI_CONFIG_SECRET_KEY", "change-me-ai-config-secret")
    app_env = os.getenv("APP_ENV", "development")
    if app_env.lower() == "production" and jwt_secret_key == "change-me-in-production":
        raise RuntimeError("JWT_SECRET_KEY must be set to a non-default value in production.")
    if app_env.lower() == "production" and ai_config_secret_key == "change-me-ai-config-secret":
        raise RuntimeError("AI_CONFIG_SECRET_KEY must be set to a non-default value in production.")
    if app_env.lower() == "production" and ai_config_secret_key == jwt_secret_key:
        raise RuntimeError("AI_CONFIG_SECRET_KEY must be different from JWT_SECRET_KEY in production.")

    return Settings(
        app_name=os.getenv("APP_NAME", "竹林 AI"),
        app_env=app_env,
        database_url=os.getenv("DATABASE_URL", "sqlite:///./data/bamboo_ai.db"),
        jwt_secret_key=jwt_secret_key,
        ai_config_secret_key=ai_config_secret_key,
        jwt_algorithm=os.getenv("JWT_ALGORITHM", "HS256"),
        access_token_expire_minutes=_get_int_env("ACCESS_TOKEN_EXPIRE_MINUTES", 1440, minimum=1),
        cors_allow_origins=_parse_csv(os.getenv("CORS_ALLOW_ORIGINS", "*")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        log_dir=os.getenv("LOG_DIR", "logs"),
        log_max_bytes=_get_int_env("LOG_MAX_BYTES", 10 * 1024 * 1024, minimum=1),
        log_backup_count=_get_int_env("LOG_BACKUP_COUNT", 5, minimum=0),
        max_world_import_bytes=_get_int_env("MAX_WORLD_IMPORT_BYTES", 64 * 1024 * 1024, minimum=1),
        login_rate_window_seconds=_get_int_env("LOGIN_RATE_WINDOW_SECONDS", 300, minimum=1),
        login_rate_max_attempts=_get_int_env("LOGIN_RATE_MAX_ATTEMPTS", 8, minimum=1),
        login_lockout_seconds=_get_int_env("LOGIN_LOCKOUT_SECONDS", 600, minimum=1),
        max_running_world_jobs_per_user=_get_int_env("MAX_RUNNING_WORLD_JOBS_PER_USER", 2, minimum=1),
        max_running_world_jobs_per_book=_get_int_env("MAX_RUNNING_WORLD_JOBS_PER_BOOK", 1, minimum=1),
        max_running_world_jobs_global=_get_int_env("MAX_RUNNING_WORLD_JOBS_GLOBAL", 4, minimum=1),
    )


settings = load_settings()
