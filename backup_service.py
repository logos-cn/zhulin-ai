from __future__ import annotations

import errno
import fcntl
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Optional


ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
BACKUP_DIR = DATA_DIR / "db_backups"
BACKUP_SETTINGS_PATH = DATA_DIR / "database_backup_settings.json"
BACKUP_SCHEDULER_LOCK_PATH = DATA_DIR / "database_backup_scheduler.lock"
SCHEDULER_POLL_SECONDS = 60
DEFAULT_INTERVAL_HOURS = 24
DEFAULT_RETENTION_DAYS = 7
MIN_INTERVAL_HOURS = 1
MAX_INTERVAL_HOURS = 24 * 30
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 365


_SETTINGS_LOCK = threading.Lock()
_SCHEDULER_LOCK_HANDLE: Optional[Any] = None
_BACKUP_OPERATION_LOCK = threading.RLock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_datetime(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _default_settings() -> dict[str, Any]:
    return {
        "enabled": False,
        "interval_hours": DEFAULT_INTERVAL_HOURS,
        "retention_days": DEFAULT_RETENTION_DAYS,
        "last_backup_at": None,
        "last_backup_path": None,
        "last_error": None,
        "updated_at": _now_iso(),
    }


def _coerce_settings(payload: Optional[dict[str, Any]]) -> dict[str, Any]:
    merged = {**_default_settings(), **(payload or {})}
    interval_hours = int(merged.get("interval_hours") or DEFAULT_INTERVAL_HOURS)
    retention_days = int(merged.get("retention_days") or DEFAULT_RETENTION_DAYS)
    merged["enabled"] = bool(merged.get("enabled"))
    merged["interval_hours"] = max(MIN_INTERVAL_HOURS, min(MAX_INTERVAL_HOURS, interval_hours))
    merged["retention_days"] = max(MIN_RETENTION_DAYS, min(MAX_RETENTION_DAYS, retention_days))
    merged["last_backup_at"] = str(merged.get("last_backup_at") or "") or None
    merged["last_backup_path"] = str(merged.get("last_backup_path") or "") or None
    merged["last_error"] = str(merged.get("last_error") or "") or None
    merged["updated_at"] = str(merged.get("updated_at") or _now_iso())
    return merged


def load_backup_settings() -> dict[str, Any]:
    with _SETTINGS_LOCK:
        if not BACKUP_SETTINGS_PATH.exists():
            settings = _default_settings()
            BACKUP_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            BACKUP_SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
            return settings
        try:
            payload = json.loads(BACKUP_SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            payload = _default_settings()
        settings = _coerce_settings(payload if isinstance(payload, dict) else None)
        BACKUP_SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        return settings


def save_backup_settings(payload: dict[str, Any]) -> dict[str, Any]:
    settings = _coerce_settings(payload)
    settings["updated_at"] = _now_iso()
    with _SETTINGS_LOCK:
        BACKUP_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        BACKUP_SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return settings


def update_backup_settings(*, enabled: bool, interval_hours: int, retention_days: int) -> dict[str, Any]:
    current = load_backup_settings()
    current["enabled"] = bool(enabled)
    current["interval_hours"] = max(MIN_INTERVAL_HOURS, min(MAX_INTERVAL_HOURS, int(interval_hours)))
    current["retention_days"] = max(MIN_RETENTION_DAYS, min(MAX_RETENTION_DAYS, int(retention_days)))
    return save_backup_settings(current)


def _resolve_sqlite_path(database_url: str) -> Optional[Path]:
    raw = str(database_url or "").strip()
    if not raw.startswith("sqlite:///"):
        return None
    path_text = raw.removeprefix("sqlite:///")
    if not path_text or path_text == ":memory:" or path_text.startswith("file:"):
        return None
    path = Path(path_text)
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path


def _backup_capabilities(database_url: str) -> dict[str, Any]:
    sqlite_path = _resolve_sqlite_path(database_url)
    if sqlite_path is None:
        return {
            "supported": False,
            "database_engine": "non_sqlite",
            "detail": "当前自动备份仅支持 SQLite 数据库。",
            "database_path": None,
        }
    return {
        "supported": True,
        "database_engine": "sqlite",
        "detail": "将使用 SQLite 原生备份能力生成一致性快照。",
        "database_path": str(sqlite_path),
    }


def _backup_file_record(path: Path) -> dict[str, Any]:
    stat = path.stat()
    created_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    return {
        "filename": path.name,
        "path": str(path),
        "size_bytes": stat.st_size,
        "created_at": created_at,
    }


def resolve_backup_file_path(filename: str) -> Path:
    name = Path(str(filename or "").strip()).name
    if not name or name in {".", ".."} or name != str(filename or "").strip():
        raise ValueError("备份文件名不合法。")
    if not name.endswith(".sqlite3"):
        raise ValueError("仅支持 .sqlite3 备份文件。")
    path = BACKUP_DIR / name
    if not path.exists() or not path.is_file():
        raise FileNotFoundError("备份文件不存在。")
    return path


def list_backup_files(*, limit: int = 20) -> list[dict[str, Any]]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    items = sorted(BACKUP_DIR.glob("*.sqlite3"), key=lambda item: item.stat().st_mtime, reverse=True)
    return [_backup_file_record(path) for path in items[: max(1, limit)]]


def get_backup_status(database_url: str) -> dict[str, Any]:
    settings = load_backup_settings()
    capability = _backup_capabilities(database_url)
    recent_backups = list_backup_files(limit=12)
    last_backup_at = _parse_datetime(settings.get("last_backup_at"))
    next_backup_at = None
    if settings["enabled"] and capability["supported"]:
        if last_backup_at is None:
            next_backup_at = _now().isoformat()
        else:
            next_backup_at = (last_backup_at + timedelta(hours=settings["interval_hours"])).isoformat()
    return {
        **settings,
        **capability,
        "backup_directory": str(BACKUP_DIR),
        "recent_backups": recent_backups,
        "latest_backup": recent_backups[0] if recent_backups else None,
        "next_backup_at": next_backup_at,
    }


def _prune_old_backups(*, retention_days: int) -> int:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = _now() - timedelta(days=retention_days)
    deleted = 0
    for path in BACKUP_DIR.glob("*.sqlite3"):
        modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if modified_at < cutoff:
            path.unlink(missing_ok=True)
            deleted += 1
    return deleted


def _perform_sqlite_backup(source_path: Path, destination_path: Path) -> None:
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination_path.with_suffix(destination_path.suffix + ".tmp")
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    destination = sqlite3.connect(str(temp_path))
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()
    temp_path.replace(destination_path)


def _perform_sqlite_restore(source_path: Path, destination_path: Path) -> None:
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    destination = sqlite3.connect(str(destination_path))
    try:
        integrity = source.execute("PRAGMA integrity_check").fetchone()
        if not integrity or str(integrity[0]).strip().lower() != "ok":
            raise RuntimeError("备份文件校验失败，无法恢复数据库。")
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()


def run_database_backup_now(database_url: str, *, reason: str = "manual") -> dict[str, Any]:
    capability = _backup_capabilities(database_url)
    if not capability["supported"]:
        raise RuntimeError(capability["detail"])

    source_path = Path(capability["database_path"])
    if not source_path.exists():
        raise RuntimeError("数据库文件不存在，无法执行备份。")

    with _BACKUP_OPERATION_LOCK:
        timestamp = _now().strftime("%Y%m%d_%H%M%S")
        destination_path = BACKUP_DIR / f"bamboo_ai_{timestamp}_{reason}.sqlite3"
        _perform_sqlite_backup(source_path, destination_path)

        current = load_backup_settings()
        current["last_backup_at"] = _now_iso()
        current["last_backup_path"] = str(destination_path)
        current["last_error"] = None
        save_backup_settings(current)
        deleted_count = _prune_old_backups(retention_days=current["retention_days"])
        return {
            "backup": _backup_file_record(destination_path),
            "deleted_expired_count": deleted_count,
        }


def restore_database_from_backup(
    database_url: str,
    *,
    filename: str,
    create_safety_backup: bool = True,
) -> dict[str, Any]:
    capability = _backup_capabilities(database_url)
    if not capability["supported"]:
        raise RuntimeError(capability["detail"])

    destination_path = Path(capability["database_path"])
    source_path = resolve_backup_file_path(filename)
    if destination_path.resolve() == source_path.resolve():
        raise RuntimeError("不能将数据库恢复到同一个备份文件。")
    if not destination_path.exists():
        raise RuntimeError("数据库文件不存在，无法执行恢复。")

    with _BACKUP_OPERATION_LOCK:
        safety_backup = None
        if create_safety_backup:
            safety_backup = run_database_backup_now(database_url, reason="pre_restore")["backup"]
        _perform_sqlite_restore(source_path, destination_path)
        current = load_backup_settings()
        current["last_error"] = None
        save_backup_settings(current)
        return {
            "restored_from": _backup_file_record(source_path),
            "safety_backup": safety_backup,
        }


def run_scheduled_backup_if_due(database_url: str) -> Optional[dict[str, Any]]:
    settings = load_backup_settings()
    capability = _backup_capabilities(database_url)
    if not settings["enabled"] or not capability["supported"]:
        return None
    last_backup_at = _parse_datetime(settings.get("last_backup_at"))
    if last_backup_at is not None and last_backup_at + timedelta(hours=settings["interval_hours"]) > _now():
        return None
    try:
        return run_database_backup_now(database_url, reason="scheduled")
    except Exception as exc:
        current = load_backup_settings()
        current["last_error"] = str(exc)
        save_backup_settings(current)
        raise


def acquire_backup_scheduler_lock() -> bool:
    global _SCHEDULER_LOCK_HANDLE
    if _SCHEDULER_LOCK_HANDLE is not None:
        return True

    BACKUP_SCHEDULER_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = BACKUP_SCHEDULER_LOCK_PATH.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise

    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    _SCHEDULER_LOCK_HANDLE = handle
    return True


def release_backup_scheduler_lock() -> None:
    global _SCHEDULER_LOCK_HANDLE
    handle = _SCHEDULER_LOCK_HANDLE
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
        _SCHEDULER_LOCK_HANDLE = None


def backup_scheduler_loop(*, database_url: str, stop_event: threading.Event, logger) -> None:
    while not stop_event.wait(SCHEDULER_POLL_SECONDS):
        try:
            result = run_scheduled_backup_if_due(database_url)
            if result:
                logger.info(
                    "database_backup_completed file=%s size_bytes=%s deleted_expired_count=%s",
                    result["backup"]["path"],
                    result["backup"]["size_bytes"],
                    result["deleted_expired_count"],
                )
        except Exception:
            logger.exception("database_backup_failed")
