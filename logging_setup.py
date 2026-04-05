from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_MARKER_ATTR = "_bamboo_handler_marker"


def _resolve_log_level(level_name: str) -> int:
    return getattr(logging, str(level_name or "INFO").upper(), logging.INFO)


def _build_handler(
    log_file: Path,
    *,
    level: int,
    console: bool,
    max_bytes: int,
    backup_count: int,
) -> logging.Handler:
    handler: logging.Handler
    if console:
        handler = logging.StreamHandler()
    else:
        handler = RotatingFileHandler(
            log_file,
            maxBytes=max(1024, int(max_bytes)),
            backupCount=max(1, int(backup_count)),
            encoding="utf-8",
        )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    setattr(handler, _MARKER_ATTR, True)
    return handler


def _ensure_handlers(
    logger: logging.Logger,
    log_file: Path,
    *,
    level: int,
    include_console: bool,
    max_bytes: int,
    backup_count: int,
) -> None:
    logger.setLevel(level)
    if not any(getattr(handler, _MARKER_ATTR, False) for handler in logger.handlers):
        logger.addHandler(
            _build_handler(
                log_file,
                level=level,
                console=False,
                max_bytes=max_bytes,
                backup_count=backup_count,
            )
        )
        if include_console:
            logger.addHandler(
                _build_handler(
                    log_file,
                    level=level,
                    console=True,
                    max_bytes=max_bytes,
                    backup_count=backup_count,
                )
            )


def setup_logging(
    base_dir: Path,
    *,
    log_dir: str,
    log_level: str,
    max_bytes: int,
    backup_count: int,
) -> Path:
    resolved_base = Path(base_dir).resolve()
    resolved_log_dir = Path(log_dir)
    if not resolved_log_dir.is_absolute():
        resolved_log_dir = resolved_base / resolved_log_dir
    resolved_log_dir.mkdir(parents=True, exist_ok=True)
    log_file = resolved_log_dir / "bamboo-ai.log"

    level = _resolve_log_level(log_level)
    app_logger = logging.getLogger("bamboo_ai")
    app_logger.propagate = False
    _ensure_handlers(
        app_logger,
        log_file,
        level=level,
        include_console=True,
        max_bytes=max_bytes,
        backup_count=backup_count,
    )

    for logger_name in ("uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(logger_name)
        _ensure_handlers(
            logger,
            log_file,
            level=level,
            include_console=False,
            max_bytes=max_bytes,
            backup_count=backup_count,
        )

    return log_file
