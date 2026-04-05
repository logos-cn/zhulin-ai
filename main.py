from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
import errno
import fcntl
import io
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any, Literal, Optional
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4
import zipfile

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import and_, delete as sql_delete, func, inspect, or_, select, text
from sqlalchemy.orm import Session, selectinload
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ai_service import (
    AIConfigNotFoundError,
    AIInvocationError,
    AccessDeniedError,
    _extract_json_block,
    apply_generation_draft,
    call_openai_compatible_chat,
    ResourceNotFoundError,
    ensure_book_access,
    estimate_text_units,
    fetch_openai_compatible_models,
    refine_generation_draft,
    get_book_and_chapter,
    iter_openai_compatible_chat_stream,
    resolve_ai_config_with_fallback,
    run_world_extraction as legacy_run_world_extraction,
    run_generation,
    store_latest_ai_draft_text,
    stream_generation_draft_events,
)
from backup_service import (
    MAX_INTERVAL_HOURS,
    MAX_RETENTION_DAYS,
    MIN_INTERVAL_HOURS,
    MIN_RETENTION_DAYS,
    acquire_backup_scheduler_lock,
    backup_scheduler_loop,
    get_backup_status,
    release_backup_scheduler_lock,
    resolve_backup_file_path,
    restore_database_from_backup,
    run_database_backup_now,
    update_backup_settings,
)
from config import settings
from database import SessionLocal, engine, get_db, init_database
from logging_setup import setup_logging
from memory_service import get_derived_style_summary, schedule_chapter_memory_consolidation
from network_security import UnsafeOutboundURLError, validate_outbound_base_url
from secret_storage import SecretStorageError, decrypt_secret, encrypt_secret, is_encrypted_secret
from character_cards import (
    CHARACTER_CARD_TEXT_FIELDS,
    merge_character_card_json,
    normalize_character_life_statuses,
    normalize_character_timeline_entries,
)
from models import (
    AIConfig,
    AIModule,
    AIScope,
    Book,
    BookStatus,
    Character,
    Chapter,
    ChapterEpisodicMemory,
    ChapterNodeType,
    ChapterStatus,
    Relation,
    RelationEvent,
    SemanticKnowledgeBase,
    Snapshot,
    User,
    UserRole,
    UserStatus,
    Faction,
    FactionMembership,
    WorldConflictStrategy,
    WorldExtractionJob,
    WorldExtractionJobStatus,
    WorldExtractionSource,
)
from security import (
    TokenError,
    PASSWORD_TIMING_PADDING_HASH,
    create_access_token,
    decode_access_token,
    ensure_security_dependencies,
    hash_password,
    verify_password,
)
from world_extraction_service import (
    ExtractionSegment,
    build_upload_storage_path,
    job_cancel_requested,
    job_conflict_strategy,
    job_is_terminated,
    mark_job_terminated,
    normalize_segment_unit_limit,
    process_world_extraction_job,
    recover_interrupted_world_extraction_jobs,
    resolve_world_extraction_conflict,
    run_segment_world_extraction,
    estimate_import_document,
    validate_world_import_source,
)
from world_schema import (
    faction_status_label,
    normalize_faction_status,
    normalize_relation_importance,
    normalize_relation_label,
    normalize_relation_type,
    relation_importance_label,
    relation_type_label,
)
from world_relations import record_relation_event


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
LOG_FILE = setup_logging(
    BASE_DIR,
    log_dir=settings.log_dir,
    log_level=settings.log_level,
    max_bytes=settings.log_max_bytes,
    backup_count=settings.log_backup_count,
)
logger = logging.getLogger("bamboo_ai")
LOGIN_RATE_LIMIT_LOCK = threading.Lock()
LOGIN_ATTEMPTS: dict[str, list[float]] = {}
LOGIN_BLOCKED_UNTIL: dict[str, float] = {}
WORLD_EXTRACTION_RECOVERY_LOCK_PATH = BASE_DIR / "data" / "world_extraction_recovery.lock"
WORLD_EXTRACTION_RECOVERY_LOCK_HANDLE: Optional[Any] = None
DATABASE_BACKUP_SCHEDULER_STOP_EVENT = threading.Event()
DATABASE_BACKUP_SCHEDULER_THREAD: Optional[threading.Thread] = None


def launch_world_extraction_job(job_id: int) -> None:
    worker = threading.Thread(
        target=process_world_extraction_job,
        args=(job_id,),
        name=f"world-extract-job-{job_id}",
        daemon=True,
    )
    worker.start()
    logger.info("world_extraction_job_dispatched job_id=%s", job_id)


def acquire_world_extraction_recovery_lock(
    lock_path: Path = WORLD_EXTRACTION_RECOVERY_LOCK_PATH,
) -> bool:
    global WORLD_EXTRACTION_RECOVERY_LOCK_HANDLE

    if WORLD_EXTRACTION_RECOVERY_LOCK_HANDLE is not None:
        return True

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
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
    WORLD_EXTRACTION_RECOVERY_LOCK_HANDLE = handle
    return True


def release_world_extraction_recovery_lock() -> None:
    global WORLD_EXTRACTION_RECOVERY_LOCK_HANDLE

    handle = WORLD_EXTRACTION_RECOVERY_LOCK_HANDLE
    if handle is None:
        return

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
        WORLD_EXTRACTION_RECOVERY_LOCK_HANDLE = None


def redact_database_url(database_url: str) -> str:
    raw = str(database_url or "").strip()
    if not raw or raw.startswith("sqlite"):
        return raw
    parsed = urlsplit(raw)
    if not parsed.netloc:
        return raw
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    if parsed.username:
        host = f"{parsed.username}:***@{host}"
    redacted = parsed._replace(netloc=host)
    return urlunsplit(redacted)


def _login_rate_limit_keys(request: Request, username: str) -> list[str]:
    client_host = request.client.host if request.client else "unknown"
    normalized_username = username.strip().lower() or "<empty>"
    return [f"ip:{client_host}", f"user:{normalized_username}"]


def enforce_login_rate_limit(request: Request, username: str) -> None:
    now = time.monotonic()
    with LOGIN_RATE_LIMIT_LOCK:
        for key in _login_rate_limit_keys(request, username):
            blocked_until = LOGIN_BLOCKED_UNTIL.get(key, 0)
            if blocked_until > now:
                retry_after = max(1, int(blocked_until - now))
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"登录尝试过多，请在 {retry_after} 秒后重试。",
                )


def record_login_failure(request: Request, username: str) -> None:
    now = time.monotonic()
    window_start = now - settings.login_rate_window_seconds
    with LOGIN_RATE_LIMIT_LOCK:
        for key in _login_rate_limit_keys(request, username):
            recent_attempts = [stamp for stamp in LOGIN_ATTEMPTS.get(key, []) if stamp >= window_start]
            recent_attempts.append(now)
            if len(recent_attempts) >= settings.login_rate_max_attempts:
                LOGIN_BLOCKED_UNTIL[key] = now + settings.login_lockout_seconds
                recent_attempts = []
            LOGIN_ATTEMPTS[key] = recent_attempts


def clear_login_failures(request: Request, username: str) -> None:
    with LOGIN_RATE_LIMIT_LOCK:
        for key in _login_rate_limit_keys(request, username):
            LOGIN_ATTEMPTS.pop(key, None)
            LOGIN_BLOCKED_UNTIL.pop(key, None)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global DATABASE_BACKUP_SCHEDULER_THREAD
    init_database()
    ensure_security_dependencies()
    logger.info(
        "application_start env=%s database=%s log_file=%s",
        settings.app_env,
        redact_database_url(settings.database_url),
        LOG_FILE,
    )
    recovery_session = SessionLocal()
    try:
        try:
            module_schema_migrated = migrate_ai_config_module_schema(recovery_session)
            if module_schema_migrated:
                logger.info("ai_config_module_schema_migrated assistant_enabled=true")
        except Exception:
            recovery_session.rollback()
            logger.exception("ai_config_module_schema_migration_failed")
        try:
            legacy_reference_stats = repair_ai_config_legacy_sqlite_references(recovery_session)
            if any(legacy_reference_stats.values()):
                logger.info("ai_config_legacy_sqlite_references_repaired %s", legacy_reference_stats)
        except Exception:
            recovery_session.rollback()
            logger.exception("ai_config_legacy_sqlite_reference_repair_failed")
        try:
            migrated_api_keys = migrate_ai_config_api_keys(recovery_session)
            if migrated_api_keys:
                logger.info("ai_config_api_keys_migrated count=%s", migrated_api_keys)
        except Exception:
            recovery_session.rollback()
            logger.exception("ai_config_api_key_migration_failed")
        try:
            migration_stats = migrate_world_schema(recovery_session)
            if any(migration_stats.values()):
                logger.info("world_schema_migrated %s", migration_stats)
        except Exception:
            recovery_session.rollback()
            logger.exception("world_schema_migration_failed")
        if acquire_world_extraction_recovery_lock():
            try:
                recover_interrupted_world_extraction_jobs(recovery_session)
            except Exception:
                recovery_session.rollback()
                logger.exception("world_extraction_recovery_failed")
        else:
            logger.info("world_extraction_recovery_skipped_existing_owner")
    finally:
        recovery_session.close()
    if acquire_backup_scheduler_lock():
        DATABASE_BACKUP_SCHEDULER_STOP_EVENT.clear()
        DATABASE_BACKUP_SCHEDULER_THREAD = threading.Thread(
            target=backup_scheduler_loop,
            kwargs={
                "database_url": settings.database_url,
                "stop_event": DATABASE_BACKUP_SCHEDULER_STOP_EVENT,
                "logger": logger,
            },
            name="database-backup-scheduler",
            daemon=True,
        )
        DATABASE_BACKUP_SCHEDULER_THREAD.start()
        logger.info("database_backup_scheduler_started")
    else:
        logger.info("database_backup_scheduler_skipped_existing_owner")
    try:
        yield
    finally:
        DATABASE_BACKUP_SCHEDULER_STOP_EVENT.set()
        DATABASE_BACKUP_SCHEDULER_THREAD = None
        release_backup_scheduler_lock()
        release_world_extraction_recovery_lock()


app = FastAPI(
    title=f"{settings.app_name} API",
    version="0.2.0",
    lifespan=lifespan,
)


allow_origins = list(settings.cors_allow_origins)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _should_disable_cache(path: str) -> bool:
    return path.startswith("/static/") or path in {
        "/",
        "/login",
        "/library",
        "/writer",
        "/characters",
        "/world",
        "/settings",
        "/history",
        "/admin",
    }


class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid4().hex[:12]
        scope.setdefault("state", {})["request_id"] = request_id
        started_at = time.perf_counter()
        method = str(scope.get("method") or "-")
        path = str(scope.get("path") or "")
        client = scope.get("client")
        client_host = client[0] if isinstance(client, tuple) and client else "-"
        response_started = False
        logged_completion = False

        async def send_with_observability(message: Message) -> None:
            nonlocal response_started, logged_completion
            if message["type"] == "http.response.start":
                response_started = True
                headers = list(message.get("headers") or [])
                headers.append((b"x-request-id", request_id.encode("ascii")))
                if _should_disable_cache(path):
                    headers.extend(
                        [
                            (b"cache-control", b"no-store, no-cache, must-revalidate, max-age=0"),
                            (b"pragma", b"no-cache"),
                            (b"expires", b"0"),
                        ]
                    )
                message["headers"] = headers
                if not path.startswith("/static/"):
                    duration_ms = (time.perf_counter() - started_at) * 1000
                    logger.info(
                        "request_completed request_id=%s method=%s path=%s status=%s client=%s duration_ms=%.2f",
                        request_id,
                        method,
                        path,
                        message.get("status"),
                        client_host,
                        duration_ms,
                    )
                    logged_completion = True
            await send(message)

        try:
            await self.app(scope, receive, send_with_observability)
        except Exception:
            duration_ms = (time.perf_counter() - started_at) * 1000
            logger.exception(
                "request_failed request_id=%s method=%s path=%s client=%s duration_ms=%.2f",
                request_id,
                method,
                path,
                client_host,
                duration_ms,
            )
            raise

        if response_started and not logged_completion and not path.startswith("/static/"):
            duration_ms = (time.perf_counter() - started_at) * 1000
            logger.info(
                "request_completed request_id=%s method=%s path=%s status=%s client=%s duration_ms=%.2f",
                request_id,
                method,
                path,
                "-",
                client_host,
                duration_ms,
            )


app.add_middleware(RequestLoggingMiddleware)


bearer_scheme = HTTPBearer(auto_error=False)


class LoginRequest(BaseModel):
    username: str
    password: str


class UserResponse(BaseModel):
    id: int
    username: str
    display_name: Optional[str]
    email: Optional[str]
    role: str
    status: str
    is_active: bool
    last_login_at: Optional[datetime]


class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    expires_in: int
    user: UserResponse


class HealthResponse(BaseModel):
    status: str
    app_name: str
    environment: str


class AIContextRequest(BaseModel):
    module: AIModule = AIModule.CO_WRITING
    user_prompt: str = ""
    target_field: Literal["content", "outline", "summary"] = "content"
    apply_mode: Literal["append", "replace"] = "append"
    target_units: Optional[int] = None
    previous_chapters: int = 3
    character_limit: int = 8
    system_prompt_override: Optional[str] = None


def _env_name_for_ai_module(module: Optional[AIModule], suffix: str) -> Optional[str]:
    if module is None:
        return None
    return f"AI_{module.value.upper()}_{suffix}"


def _resolve_runtime_env(*names: Optional[str]) -> Optional[str]:
    for name in names:
        if not name:
            continue
        value = os.getenv(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


class AIGenerationRequest(AIContextRequest):
    chunk_size: int = 900
    use_reasoner_planning: bool = True
    dry_run: bool = False
    store_snapshot: bool = True


class AIDraftRefineRequest(AIContextRequest):
    draft_text: str
    adjustment_mode: Literal["expand", "trim"]
    planning_text: Optional[str] = None


class AIDraftApplyRequest(AIContextRequest):
    generated_text: str
    planning_text: Optional[str] = None
    store_snapshot: bool = True


class AIWorldExtractionRequest(BaseModel):
    dry_run: bool = False
    update_world_bible: bool = True


class AIBookWorldExtractionRequest(BaseModel):
    dry_run: bool = False
    update_world_bible: bool = True
    chapter_scope: Literal["with_content", "all"] = "with_content"


class WorldExtractionJobCreateRequest(BaseModel):
    update_world_bible: bool = True
    chapter_scope: Literal["with_content", "all"] = "with_content"
    conflict_strategy: WorldConflictStrategy = WorldConflictStrategy.MERGE
    segment_unit_limit: int = 36000
    skip_unchanged_chapters: bool = True


class WorldExtractionJobResumeRequest(BaseModel):
    delete_previous: bool = False
    failed_only: bool = False


class WorldConflictResolutionRequest(BaseModel):
    conflict_id: str
    decision: Literal["keep_existing", "prefer_imported"]


class BookProjectImportSummary(BaseModel):
    merge_strategy: Literal["smart_merge", "keep_existing", "replace_existing"] = "smart_merge"


class BookProjectImportApplyRequest(BaseModel):
    session_id: str
    merge_strategy: Literal["smart_merge", "keep_existing", "replace_existing"] = "smart_merge"
    decisions: dict[str, str] = {}


class AssistantHistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class AssistantChatRequest(BaseModel):
    book_id: int
    chapter_id: Optional[int] = None
    page: Optional[str] = None
    message: str
    history: list[AssistantHistoryMessage] = []
    selected_character_ids: list[int] = []
    selected_chapter_ids: list[int] = []
    custom_prompt: Optional[str] = None
    current_chapter_title: Optional[str] = None
    current_chapter_summary: Optional[str] = None
    current_chapter_outline: Optional[str] = None
    current_chapter_content: Optional[str] = None


class BookCreateRequest(BaseModel):
    title: str
    owner_id: Optional[int] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    genre: Optional[str] = None
    language: str = "zh-CN"
    tags: Optional[list[str]] = None
    global_style_prompt: str = ""
    long_term_summary: Optional[str] = None
    world_bible: Optional[str] = None
    outline: Optional[str] = None
    status: BookStatus = BookStatus.DRAFT
    extra_data: Optional[dict[str, Any]] = None


class BookUpdateRequest(BaseModel):
    title: Optional[str] = None
    owner_id: Optional[int] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    genre: Optional[str] = None
    language: Optional[str] = None
    tags: Optional[list[str]] = None
    global_style_prompt: Optional[str] = None
    long_term_summary: Optional[str] = None
    world_bible: Optional[str] = None
    outline: Optional[str] = None
    status: Optional[BookStatus] = None
    extra_data: Optional[dict[str, Any]] = None


class ChapterCreateRequest(BaseModel):
    title: str
    parent_id: Optional[int] = None
    node_type: ChapterNodeType = ChapterNodeType.CHAPTER
    status: ChapterStatus = ChapterStatus.DRAFT
    sequence_number: Optional[int] = None
    sort_order: Optional[int] = None
    summary: Optional[str] = None
    outline: str = ""
    content: str = ""
    context_summary: Optional[str] = None
    extra_data: Optional[dict[str, Any]] = None


class ChapterUpdateRequest(BaseModel):
    title: Optional[str] = None
    parent_id: Optional[int] = None
    node_type: Optional[ChapterNodeType] = None
    status: Optional[ChapterStatus] = None
    sequence_number: Optional[int] = None
    sort_order: Optional[int] = None
    summary: Optional[str] = None
    outline: Optional[str] = None
    content: Optional[str] = None
    context_summary: Optional[str] = None
    extra_data: Optional[dict[str, Any]] = None


class AIConfigCreateRequest(BaseModel):
    name: str
    scope: AIScope
    module: AIModule
    user_id: Optional[int] = None
    book_id: Optional[int] = None
    provider_name: Optional[str] = None
    api_format: str = "openai_v1"
    base_url: Optional[str] = None
    base_url_env_var: Optional[str] = None
    api_key: Optional[str] = None
    api_key_env_var: Optional[str] = None
    model_name: Optional[str] = None
    model_name_env_var: Optional[str] = None
    reasoning_effort: Optional[str] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    timeout_seconds: int = 120
    priority: int = 100
    is_enabled: bool = True
    is_default: bool = False
    system_prompt_template: Optional[str] = None
    extra_headers: Optional[dict[str, Any]] = None
    extra_body: Optional[dict[str, Any]] = None
    notes: Optional[str] = None


class AIConfigUpdateRequest(BaseModel):
    name: Optional[str] = None
    scope: Optional[AIScope] = None
    module: Optional[AIModule] = None
    user_id: Optional[int] = None
    book_id: Optional[int] = None
    provider_name: Optional[str] = None
    api_format: Optional[str] = None
    base_url: Optional[str] = None
    base_url_env_var: Optional[str] = None
    api_key: Optional[str] = None
    clear_api_key: bool = False
    api_key_env_var: Optional[str] = None
    model_name: Optional[str] = None
    model_name_env_var: Optional[str] = None
    reasoning_effort: Optional[str] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    timeout_seconds: Optional[int] = None
    priority: Optional[int] = None
    is_enabled: Optional[bool] = None
    is_default: Optional[bool] = None
    system_prompt_template: Optional[str] = None
    extra_headers: Optional[dict[str, Any]] = None
    extra_body: Optional[dict[str, Any]] = None
    notes: Optional[str] = None


class AIModelDiscoveryRequest(BaseModel):
    config_id: Optional[int] = None
    api_format: str = "openai_v1"
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    timeout_seconds: int = 30


class UserCreateRequest(BaseModel):
    username: str
    password: str
    display_name: Optional[str] = None
    email: Optional[str] = None
    role: UserRole = UserRole.AUTHOR
    status: UserStatus = UserStatus.ACTIVE
    is_active: bool = True
    notes: Optional[str] = None


class UserUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[UserRole] = None
    status: Optional[UserStatus] = None
    is_active: Optional[bool] = None
    notes: Optional[str] = None


class UserPasswordResetRequest(BaseModel):
    password: str


class ChangeOwnPasswordRequest(BaseModel):
    old_password: str
    new_password: str


class DatabaseBackupSettingsUpdateRequest(BaseModel):
    enabled: bool
    interval_hours: int
    retention_days: int


class DatabaseBackupRestoreRequest(BaseModel):
    filename: str
    create_safety_backup: bool = True


class CharacterCreateRequest(BaseModel):
    name: str
    biography: Optional[str] = None
    aliases: Optional[list[str]] = None
    role_label: Optional[str] = None
    description: Optional[str] = None
    traits: Optional[list[str]] = None
    background: Optional[str] = None
    goals: Optional[str] = None
    secrets: Optional[str] = None
    notes: Optional[str] = None
    first_appearance_chapter_id: Optional[int] = None
    last_appearance_chapter_id: Optional[int] = None
    is_active: bool = True
    life_statuses: Optional[list[str]] = None
    timeline_entries: Optional[list[dict[str, Any]]] = None
    card_json: Optional[dict[str, Any]] = None


class CharacterUpdateRequest(BaseModel):
    name: Optional[str] = None
    biography: Optional[str] = None
    aliases: Optional[list[str]] = None
    role_label: Optional[str] = None
    description: Optional[str] = None
    traits: Optional[list[str]] = None
    background: Optional[str] = None
    goals: Optional[str] = None
    secrets: Optional[str] = None
    notes: Optional[str] = None
    first_appearance_chapter_id: Optional[int] = None
    last_appearance_chapter_id: Optional[int] = None
    is_active: Optional[bool] = None
    life_statuses: Optional[list[str]] = None
    timeline_entries: Optional[list[dict[str, Any]]] = None
    card_json: Optional[dict[str, Any]] = None


class RelationCreateRequest(BaseModel):
    source_character_id: int
    target_character_id: int
    relation_type: str
    label: Optional[str] = None
    description: Optional[str] = None
    strength: Optional[float] = None
    importance_level: Optional[str] = None
    is_bidirectional: bool = False
    valid_from_chapter_id: Optional[int] = None
    valid_to_chapter_id: Optional[int] = None
    extra_data: Optional[dict[str, Any]] = None


class RelationUpdateRequest(BaseModel):
    source_character_id: Optional[int] = None
    target_character_id: Optional[int] = None
    relation_type: Optional[str] = None
    label: Optional[str] = None
    description: Optional[str] = None
    strength: Optional[float] = None
    importance_level: Optional[str] = None
    is_bidirectional: Optional[bool] = None
    valid_from_chapter_id: Optional[int] = None
    valid_to_chapter_id: Optional[int] = None
    extra_data: Optional[dict[str, Any]] = None


class FactionCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    color: Optional[str] = None
    extra_data: Optional[dict[str, Any]] = None


class FactionUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    color: Optional[str] = None
    extra_data: Optional[dict[str, Any]] = None


class FactionMembershipCreateRequest(BaseModel):
    faction_id: int
    character_id: int
    role_label: Optional[str] = None
    loyalty: Optional[float] = None
    status: Optional[str] = None
    start_chapter_id: Optional[int] = None
    end_chapter_id: Optional[int] = None
    notes: Optional[str] = None


class FactionMembershipUpdateRequest(BaseModel):
    faction_id: Optional[int] = None
    character_id: Optional[int] = None
    role_label: Optional[str] = None
    loyalty: Optional[float] = None
    status: Optional[str] = None
    start_chapter_id: Optional[int] = None
    end_chapter_id: Optional[int] = None
    notes: Optional[str] = None


def serialize_user(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        email=user.email,
        role=user.role.value if isinstance(user.role, UserRole) else str(user.role),
        status=user.status.value if isinstance(user.status, UserStatus) else str(user.status),
        is_active=user.is_active,
        last_login_at=user.last_login_at,
    )


def serialize_book(book: Book, *, include_detail: bool = False) -> dict[str, Any]:
    if book.owner_id is not None and book.owner is None:
        logger.warning(
            "book_owner_missing book_id=%s owner_id=%s",
            book.id,
            book.owner_id,
        )
    payload = {
        "id": book.id,
        "owner_id": book.owner_id,
        "owner_username": book.owner.username if book.owner else None,
        "owner_display_name": book.owner.display_name if book.owner else None,
        "title": book.title,
        "slug": book.slug,
        "description": book.description,
        "genre": book.genre,
        "language": book.language,
        "tags": book.tags or [],
        "word_count": book.word_count,
        "chapter_count": book.chapter_count,
        "status": book.status.value,
        "created_at": book.created_at,
        "updated_at": book.updated_at,
    }
    if include_detail:
        derived_style_summary, derived_style_summary_updated_at = get_derived_style_summary(book)
        payload.update(
            {
                "global_style_prompt": book.global_style_prompt,
                "long_term_summary": book.long_term_summary,
                "world_bible": book.world_bible,
                "outline": book.outline,
                "extra_data": book.extra_data or {},
                "derived_style_summary": derived_style_summary,
                "derived_style_summary_updated_at": derived_style_summary_updated_at or None,
            }
        )
    return payload


def serialize_chapter_tree_item(chapter: Chapter) -> dict[str, Any]:
    return {
        "id": chapter.id,
        "book_id": chapter.book_id,
        "parent_id": chapter.parent_id,
        "title": chapter.title,
        "node_type": chapter.node_type.value,
        "status": chapter.status.value,
        "sequence_number": chapter.sequence_number,
        "sort_order": chapter.sort_order,
        "depth": chapter.depth,
        "tree_path": chapter.tree_path,
        "word_count": chapter.word_count,
        "version": chapter.version,
        "created_at": chapter.created_at,
        "updated_at": chapter.updated_at,
    }


def serialize_chapter_detail(chapter: Chapter) -> dict[str, Any]:
    payload = serialize_chapter_tree_item(chapter)
    payload.update(
        {
            "summary": chapter.summary,
            "outline": chapter.outline,
            "content": chapter.content,
            "context_summary": chapter.context_summary,
            "extra_data": chapter.extra_data or {},
        }
    )
    return payload


def serialize_admin_episodic_memory(chapter: Chapter) -> Optional[dict[str, Any]]:
    memory = chapter.episodic_memory
    if memory is None:
        return None
    return {
        "chapter_id": chapter.id,
        "chapter_title": chapter.title,
        "sequence_number": chapter.sequence_number,
        "sort_order": chapter.sort_order,
        "status": chapter.status.value if isinstance(chapter.status, ChapterStatus) else str(chapter.status),
        "summary": memory.summary,
        "involved_characters": memory.involved_characters,
        "updated_at": memory.updated_at,
    }


def serialize_admin_semantic_memory(entry: SemanticKnowledgeBase) -> dict[str, Any]:
    return {
        "id": entry.id,
        "entity_name": entry.entity_name,
        "core_fact": entry.core_fact,
        "updated_at": entry.updated_at,
    }


def chapter_has_extractable_content(chapter: Chapter) -> bool:
    return isinstance(chapter.content, str) and bool(chapter.content.strip())


def build_chapter_extraction_segment(chapter: Chapter) -> Any:
    chapter_number = chapter.sequence_number or chapter.sort_order or chapter.id
    parts = [f"章节标题：{chapter.title or f'第{chapter_number}章'}"]
    if chapter.content and chapter.content.strip():
        parts.append(f"章节正文：\n{chapter.content.strip()}")
    segment_text = "\n\n".join(part for part in parts if part.strip()).strip()
    return {
        "label": chapter.title or f"第{chapter_number}章",
        "text": segment_text,
        "unit_count": estimate_text_units(segment_text),
        "chapter_id": chapter.id,
    }


def select_book_chapters_for_world_extraction(
    db: Session,
    book_id: int,
    *,
    chapter_scope: Literal["with_content", "all"],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    chapters = db.execute(select(Chapter).where(Chapter.book_id == book_id)).scalars().all()
    chapters.sort(
        key=lambda chapter: (
            chapter.sequence_number is None,
            chapter.sequence_number or 0,
            chapter.sort_order,
            chapter.depth,
            chapter.id,
        )
    )

    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for chapter in chapters:
        if chapter.node_type not in {ChapterNodeType.CHAPTER, ChapterNodeType.SCENE}:
            continue
        if chapter_scope == "with_content" and not chapter_has_extractable_content(chapter):
            skipped.append(
                {
                    "chapter_id": chapter.id,
                    "chapter_title": chapter.title,
                    "reason": "empty_content",
                }
            )
            continue
        selected.append(
            {
                "chapter_id": chapter.id,
                "chapter_title": chapter.title,
            }
        )

    return selected, skipped


def serialize_snapshot(snapshot: Snapshot) -> dict[str, Any]:
    return {
        "id": snapshot.id,
        "book_id": snapshot.book_id,
        "book_title": snapshot.book.title if snapshot.book else None,
        "chapter_id": snapshot.chapter_id,
        "chapter_title": snapshot.chapter_title,
        "created_by_id": snapshot.created_by_id,
        "created_by_username": snapshot.created_by.username if snapshot.created_by else None,
        "ai_config_id": snapshot.ai_config_id,
        "kind": snapshot.kind.value,
        "label": snapshot.label,
        "chapter_version": snapshot.chapter_version,
        "summary": snapshot.summary,
        "diff_summary": snapshot.diff_summary,
        "source_model_name": snapshot.source_model_name,
        "word_count": snapshot.word_count,
        "character_count": snapshot.character_count,
        "created_at": snapshot.created_at,
        "updated_at": snapshot.updated_at,
    }


def serialize_snapshot_detail(snapshot: Snapshot) -> dict[str, Any]:
    current_chapter = snapshot.chapter
    prompt_payload = snapshot.prompt_payload or {}
    target_field = prompt_payload.get("target_field")
    if target_field not in {"content", "outline", "summary"}:
        target_field = "content"

    before_value = prompt_payload.get("before_value")
    if not isinstance(before_value, str):
        before_value = getattr(snapshot, target_field, None)
    if not isinstance(before_value, str):
        before_value = ""

    after_value = getattr(current_chapter, target_field, "") if current_chapter else ""
    if not isinstance(after_value, str):
        after_value = ""

    before_units = estimate_text_units(before_value)
    after_units = estimate_text_units(after_value)
    payload = serialize_snapshot(snapshot)
    payload.update(
        {
            "target_field": target_field,
            "before_value": before_value,
            "after_value": after_value,
            "outline": snapshot.outline,
            "content": snapshot.content,
            "prompt_payload": prompt_payload,
            "current_chapter": serialize_chapter_detail(current_chapter) if current_chapter else None,
            "metrics": {
                "before_units": before_units,
                "after_units": after_units,
                "delta_units": after_units - before_units,
                "chapter_version_after": current_chapter.version if current_chapter else None,
            },
        }
    )
    return payload


def serialize_ai_config(
    config: AIConfig,
    *,
    allow_runtime_env: bool = True,
    include_sensitive_fields: bool = True,
) -> dict[str, Any]:
    module_api_key_env = _env_name_for_ai_module(config.module, "API_KEY")
    try:
        stored_api_key = decrypt_secret(config.api_key)
    except SecretStorageError:
        stored_api_key = None
    resolved_api_key = (
        _resolve_runtime_env(
            config.api_key_env_var,
            module_api_key_env,
            "OPENAI_COMPAT_API_KEY",
            "OPENAI_API_KEY",
        )
        if allow_runtime_env
        else None
    ) or stored_api_key
    payload = {
        "id": config.id,
        "name": config.name,
        "scope": config.scope.value,
        "module": config.module.value,
        "user_id": config.user_id,
        "user_label": config.user.username if config.user else None,
        "book_id": config.book_id,
        "book_title": config.book.title if config.book else None,
        "provider_name": config.provider_name,
        "api_format": config.api_format,
        "base_url": config.base_url,
        "base_url_env_var": config.base_url_env_var if allow_runtime_env else None,
        "has_api_key": bool(resolved_api_key),
        "api_key_env_var": config.api_key_env_var if allow_runtime_env else None,
        "model_name": config.model_name,
        "model_name_env_var": config.model_name_env_var if allow_runtime_env else None,
        "reasoning_effort": config.reasoning_effort,
        "temperature": config.temperature,
        "top_p": config.top_p,
        "max_tokens": config.max_tokens,
        "timeout_seconds": config.timeout_seconds,
        "priority": config.priority,
        "is_enabled": config.is_enabled,
        "is_default": config.is_default,
        "system_prompt_template": config.system_prompt_template,
        "extra_headers": config.extra_headers or {},
        "extra_body": config.extra_body or {},
        "notes": config.notes,
        "created_at": config.created_at,
        "updated_at": config.updated_at,
    }
    if not include_sensitive_fields:
        payload.update(
            {
                "base_url": None,
                "base_url_env_var": None,
                "api_key_env_var": None,
                "model_name_env_var": None,
                "system_prompt_template": None,
                "extra_headers": {},
                "extra_body": {},
                "notes": None,
            }
        )
    return payload


def serialize_user_admin(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "email": user.email,
        "role": user.role.value,
        "status": user.status.value,
        "is_active": user.is_active,
        "last_login_at": user.last_login_at,
        "notes": user.notes,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
        "created_by_id": user.created_by_id,
        "created_by_username": user.created_by.username if user.created_by else None,
        "book_count": len(user.books or []),
    }


def serialize_character(character: Character) -> dict[str, Any]:
    normalized_card_json = merge_character_card_json(character.card_json)
    timeline_entries = normalize_character_timeline_entries(normalized_card_json.get("timeline_entries"))
    if timeline_entries:
        normalized_card_json["timeline_entries"] = timeline_entries
    return {
        "id": character.id,
        "book_id": character.book_id,
        "name": character.name,
        "aliases": character.aliases or [],
        "role_label": character.role_label,
        "biography": character.description,
        "description": character.description,
        "traits": character.traits or [],
        "background": character.background,
        "goals": character.goals,
        "secrets": character.secrets,
        "notes": character.notes,
        "first_appearance_chapter_id": character.first_appearance_chapter_id,
        "last_appearance_chapter_id": character.last_appearance_chapter_id,
        "first_appearance_chapter_title": character.first_appearance_chapter.title if character.first_appearance_chapter else None,
        "last_appearance_chapter_title": character.last_appearance_chapter.title if character.last_appearance_chapter else None,
        "is_active": character.is_active,
        "life_statuses": normalized_card_json.get("life_statuses", []),
        "timeline_entries": timeline_entries,
        "age": normalized_card_json.get("age"),
        "short_term_goal": normalized_card_json.get("short_term_goal"),
        "long_term_goal": normalized_card_json.get("long_term_goal"),
        "motivation": normalized_card_json.get("motivation"),
        "personality": normalized_card_json.get("personality"),
        "appearance": normalized_card_json.get("appearance"),
        "weakness": normalized_card_json.get("weakness"),
        "card_json": normalized_card_json,
        "created_at": character.created_at,
        "updated_at": character.updated_at,
    }


def _contains_cjk_text(value: Any) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", str(value or "")))


def _canonical_relation_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


_RELATION_TEXT_TRANSLATIONS = {
    "friend": "朋友",
    "friends": "朋友",
    "ally": "盟友",
    "allies": "盟友",
    "companion": "同伴",
    "companions": "同伴",
    "teammate": "队友",
    "teammates": "队友",
    "classmate": "同学",
    "classmates": "同学",
    "colleague": "同事",
    "colleagues": "同事",
    "roommate": "室友",
    "roommates": "室友",
    "neighbor": "邻居",
    "neighbors": "邻居",
    "relative": "亲属",
    "relatives": "亲属",
    "family": "家人",
    "mentor": "导师",
    "student": "学生",
    "mentor student": "师徒",
    "master disciple": "师徒",
    "teacher student": "师生",
    "senior junior": "前后辈",
    "lover": "恋人",
    "lovers": "恋人",
    "couple": "情侣",
    "spouse": "配偶",
    "spouses": "配偶",
    "husband wife": "夫妻",
    "wife husband": "夫妻",
    "married": "夫妻",
    "romantic interest": "暧昧对象",
    "crush": "心动对象",
    "enemy": "敌人",
    "enemies": "敌对",
    "rival": "对手",
    "rivals": "对手",
    "opponent": "对手",
    "opponents": "对手",
    "boss subordinate": "上下级",
    "leader subordinate": "上下级",
    "superior subordinate": "上下级",
    "employer employee": "雇佣关系",
    "guardian ward": "监护关系",
    "benefactor beneficiary": "恩人与受助者",
    "parent child": "亲子",
    "mother son": "母子",
    "mother daughter": "母女",
    "father son": "父子",
    "father daughter": "父女",
    "brother": "兄弟",
    "brothers": "兄弟",
    "sister": "姐妹",
    "sisters": "姐妹",
    "siblings": "手足",
    "brother sister": "手足",
}

MAX_RELATION_DESCRIPTION_CHARS = 180
RELATION_DESCRIPTION_PREVIEW_CHARS = 72


def localize_relation_text(value: Any) -> Optional[str]:
    return normalize_relation_label(value)


def sanitize_relation_text(value: Any, *, fallback: Optional[str] = None) -> Optional[str]:
    return localize_relation_text(value) or fallback


def normalize_relation_description(value: Any, *, max_chars: int = MAX_RELATION_DESCRIPTION_CHARS) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    text = re.sub(r"\s+", " ", text)
    first_sentence = re.split(r"[。！？!?；;]\s*", text, maxsplit=1)[0].strip()
    if first_sentence:
        text = first_sentence
    if len(text) <= max_chars:
        return text

    return f"{text[: max_chars - 1].rstrip()}…"


def relation_description_preview(value: Any) -> Optional[str]:
    return normalize_relation_description(value, max_chars=RELATION_DESCRIPTION_PREVIEW_CHARS)


def serialize_relation_event(event: RelationEvent) -> dict[str, Any]:
    description = normalize_relation_description(event.description)
    return {
        "id": event.id,
        "relation_id": event.relation_id,
        "book_id": event.book_id,
        "source_character_id": event.source_character_id,
        "source_character_name": event.source_character.name if event.source_character else None,
        "target_character_id": event.target_character_id,
        "target_character_name": event.target_character.name if event.target_character else None,
        "chapter_id": event.chapter_id,
        "chapter_title": event.chapter.title if event.chapter else None,
        "segment_label": event.segment_label,
        "relation_type": normalize_relation_type(event.relation_type),
        "relation_type_label": relation_type_label(event.relation_type),
        "label": sanitize_relation_text(event.label),
        "description": description,
        "strength": event.strength,
        "importance_level": normalize_relation_importance(event.importance_level),
        "importance_label": relation_importance_label(event.importance_level),
        "is_bidirectional": event.is_bidirectional,
        "event_summary": normalize_relation_description(event.event_summary),
        "created_at": event.created_at,
        "updated_at": event.updated_at,
    }


def serialize_relation(relation: Relation, *, include_description: bool = True) -> dict[str, Any]:
    description = normalize_relation_description(relation.description)
    events = list(relation.events or [])
    latest_event = events[-1] if events else None
    return {
        "id": relation.id,
        "book_id": relation.book_id,
        "source_character_id": relation.source_character_id,
        "source_character_name": relation.source_character.name if relation.source_character else None,
        "target_character_id": relation.target_character_id,
        "target_character_name": relation.target_character.name if relation.target_character else None,
        "relation_type": normalize_relation_type(relation.relation_type),
        "relation_type_label": relation_type_label(relation.relation_type),
        "label": sanitize_relation_text(relation.label),
        "description": description if include_description else None,
        "description_preview": relation_description_preview(description),
        "description_length": len(description or ""),
        "strength": relation.strength,
        "importance_level": normalize_relation_importance(relation.importance_level),
        "importance_label": relation_importance_label(relation.importance_level),
        "is_bidirectional": relation.is_bidirectional,
        "valid_from_chapter_id": relation.valid_from_chapter_id,
        "valid_from_chapter_title": relation.valid_from_chapter.title if relation.valid_from_chapter else None,
        "valid_to_chapter_id": relation.valid_to_chapter_id,
        "valid_to_chapter_title": relation.valid_to_chapter.title if relation.valid_to_chapter else None,
        "extra_data": relation.extra_data or {},
        "latest_event_summary": normalize_relation_description(
            latest_event.event_summary if latest_event else (relation.extra_data or {}).get("latest_event_summary")
        ),
        "event_count": len(events),
        "created_at": relation.created_at,
        "updated_at": relation.updated_at,
    }


def serialize_faction_membership(membership: FactionMembership) -> dict[str, Any]:
    return {
        "id": membership.id,
        "book_id": membership.book_id,
        "faction_id": membership.faction_id,
        "faction_name": membership.faction.name if membership.faction else None,
        "character_id": membership.character_id,
        "character_name": membership.character.name if membership.character else None,
        "role_label": membership.role_label,
        "loyalty": membership.loyalty,
        "status": normalize_faction_status(membership.status),
        "status_label": faction_status_label(membership.status),
        "start_chapter_id": membership.start_chapter_id,
        "start_chapter_title": membership.start_chapter.title if membership.start_chapter else None,
        "end_chapter_id": membership.end_chapter_id,
        "end_chapter_title": membership.end_chapter.title if membership.end_chapter else None,
        "notes": membership.notes,
        "created_at": membership.created_at,
        "updated_at": membership.updated_at,
    }


def serialize_faction(faction: Faction, *, include_memberships: bool = False) -> dict[str, Any]:
    payload = {
        "id": faction.id,
        "book_id": faction.book_id,
        "name": faction.name,
        "description": faction.description,
        "color": faction.color,
        "extra_data": faction.extra_data or {},
        "member_count": len(faction.memberships or []),
        "created_at": faction.created_at,
        "updated_at": faction.updated_at,
    }
    if include_memberships:
        payload["memberships"] = [serialize_faction_membership(item) for item in faction.memberships or []]
    return payload


PROJECT_ARCHIVE_ALLOWED_EXTENSIONS = {".txt", ".docx"}
PROJECT_ARCHIVE_INFO_DIR = "00-项目信息"
PROJECT_ARCHIVE_PROMPTS_DIR = "01-提示词"
PROJECT_ARCHIVE_AI_CONFIGS_DIR = "02-AI配置"
PROJECT_ARCHIVE_CHAPTERS_DIR = "03-正文"
PROJECT_ARCHIVE_CHARACTERS_DIR = "04-人物卡"
PROJECT_ARCHIVE_RELATIONS_DIR = "05-人物关系"
PROJECT_IMPORT_ALLOWED_STRATEGIES = {"smart_merge", "keep_existing", "replace_existing"}
PROJECT_IMPORT_SESSION_DIR = BASE_DIR / "data" / "project_import_sessions"
PROJECT_IMPORT_SESSION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


def _project_archive_safe_name(value: Any, fallback: str) -> str:
    text = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", str(value or "").strip())
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:80] or fallback


def _project_archive_text(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").strip()


def _project_archive_doc_title(title: str) -> str:
    return f"# {title}\n\n"


def _project_archive_section(title: str, value: Any) -> str:
    text = _project_archive_text(value)
    return f"## {title}\n{text if text else '（空）'}\n\n"


def _project_archive_parse_sections(text: str) -> dict[str, str]:
    chunks = re.split(r"^##\s+(.+?)\s*$", str(text or ""), flags=re.MULTILINE)
    sections: dict[str, str] = {}
    if len(chunks) <= 1:
        return sections
    for index in range(1, len(chunks), 2):
        title = chunks[index].strip()
        body = chunks[index + 1] if index + 1 < len(chunks) else ""
        sections[title] = body.strip()
    return sections


def _project_archive_read_zip_text(data: bytes, suffix: str) -> str:
    extension = suffix.lower()
    if extension == ".txt":
        for encoding in ("utf-8", "utf-8-sig", "gb18030"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="ignore")
    if extension == ".docx":
        try:
            from docx import Document
        except Exception as exc:  # pragma: no cover - dependency issue path
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="DOCX 解析依赖缺失。") from exc
        document = Document(io.BytesIO(data))
        return "\n".join(paragraph.text for paragraph in document.paragraphs)
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="仅支持导入 TXT 或 DOCX 文档。")


def _project_archive_bytes_to_json(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="导入包中的 JSON 元数据格式不正确。") from exc


def _project_archive_nonempty_count(*values: Any) -> int:
    return sum(1 for value in values if _project_archive_text(value))


def _project_archive_text_score(value: Any) -> int:
    return len(_project_archive_text(value))


def _project_archive_pick_text(existing: Any, incoming: Any, strategy: str) -> Optional[str]:
    existing_text = _project_archive_text(existing)
    incoming_text = _project_archive_text(incoming)
    if strategy == "replace_existing":
        return incoming_text or None
    if strategy == "keep_existing":
        return existing_text or incoming_text or None
    if not existing_text:
        return incoming_text or None
    if not incoming_text:
        return existing_text or None
    return incoming_text if len(incoming_text) >= len(existing_text) else existing_text


def _project_archive_merge_string_list(existing: Any, incoming: Any) -> list[str]:
    values: list[str] = []
    for source in (existing or [], incoming or []):
        if isinstance(source, str):
            items = re.split(r"[\n,，、/|;；]+", source)
        else:
            items = source if isinstance(source, (list, tuple, set)) else [source]
        for item in items:
            text = str(item or "").strip()
            if text and text not in values:
                values.append(text)
    return values


def _project_archive_choose_bool(existing: Optional[bool], incoming: Optional[bool], strategy: str) -> bool:
    if strategy == "replace_existing":
        return bool(incoming)
    if strategy == "keep_existing":
        return bool(existing if existing is not None else incoming)
    return bool(existing or incoming)


def _project_archive_choose_number(existing: Any, incoming: Any, strategy: str) -> Optional[float]:
    if incoming in (None, "") and existing in (None, ""):
        return None
    if strategy == "replace_existing":
        return incoming if incoming not in (None, "") else existing
    if strategy == "keep_existing":
        return existing if existing not in (None, "") else incoming
    if existing in (None, ""):
        return incoming
    if incoming in (None, ""):
        return existing
    return incoming if float(incoming) >= float(existing) else existing


def _project_archive_character_card_json(character: dict[str, Any]) -> dict[str, Any]:
    raw_card_json = dict(character.get("card_json")) if isinstance(character.get("card_json"), dict) else {}
    for key in CHARACTER_CARD_TEXT_FIELDS:
        text_value = _project_archive_text(character.get(key))
        if text_value:
            raw_card_json[key] = text_value
    return merge_character_card_json(
        raw_card_json,
        life_statuses=character.get("life_statuses"),
        timeline_entries=character.get("timeline_entries"),
    )


def _project_archive_character_payload(character: dict[str, Any]) -> dict[str, Any]:
    card_json = _project_archive_character_card_json(character)
    importance_level = normalize_importance_level(card_json.get("importance_level"))
    if importance_level:
        card_json["importance_level"] = importance_level
    else:
        card_json.pop("importance_level", None)
    return {
        "name": _project_archive_text(character.get("name")),
        "aliases": _project_archive_merge_string_list([], character.get("aliases") or []),
        "role_label": _project_archive_text(character.get("role_label")) or None,
        "biography": _project_archive_text(character.get("biography") or character.get("description")) or None,
        "traits": _project_archive_merge_string_list([], character.get("traits") or []),
        "background": _project_archive_text(character.get("background")) or None,
        "goals": _project_archive_text(character.get("goals")) or None,
        "secrets": _project_archive_text(character.get("secrets")) or None,
        "notes": _project_archive_text(character.get("notes")) or None,
        "is_active": bool(character.get("is_active", True)),
        "first_appearance_chapter_title": _project_archive_text(character.get("first_appearance_chapter_title")) or None,
        "last_appearance_chapter_title": _project_archive_text(character.get("last_appearance_chapter_title")) or None,
        "life_statuses": card_json.get("life_statuses", []),
        "timeline_entries": card_json.get("timeline_entries", []),
        "age": card_json.get("age"),
        "short_term_goal": card_json.get("short_term_goal"),
        "long_term_goal": card_json.get("long_term_goal"),
        "motivation": card_json.get("motivation"),
        "personality": card_json.get("personality"),
        "appearance": card_json.get("appearance"),
        "weakness": card_json.get("weakness"),
        "card_json": card_json,
    }


def normalize_importance_level(value: Any) -> Optional[str]:
    raw = str(value or "").strip().lower()
    if raw in {"major", "main", "primary", "important", "core", "主要", "主要人物"}:
        return "major"
    if raw in {"minor", "background", "extra", "npc", "次要", "次要人物", "路人"}:
        return "minor"
    return None


def _project_archive_character_doc(character: dict[str, Any]) -> str:
    payload = _project_archive_character_payload(character)
    timeline_lines = [
        " | ".join(
            [
                str(item.get("chapter_label") or f"第{item.get('chapter_number') or '?'}章"),
                str(item.get("event") or item.get("notes") or "").strip(),
                str(item.get("location") or "").strip(),
                str(item.get("status") or "").strip(),
            ]
        ).strip(" |")
        for item in payload.get("timeline_entries", [])
    ]
    return "".join(
        [
            _project_archive_doc_title(payload["name"] or "人物卡"),
            _project_archive_section("人物名", payload["name"]),
            _project_archive_section("重要程度", payload["card_json"].get("importance_level") or ""),
            _project_archive_section("角色标签", payload.get("role_label")),
            _project_archive_section("别名", "、".join(payload.get("aliases", []))),
            _project_archive_section("年龄", payload.get("age")),
            _project_archive_section("短期目标", payload.get("short_term_goal")),
            _project_archive_section("长期目标", payload.get("long_term_goal")),
            _project_archive_section("动机", payload.get("motivation")),
            _project_archive_section("性格", payload.get("personality")),
            _project_archive_section("外貌", payload.get("appearance")),
            _project_archive_section("弱点", payload.get("weakness")),
            _project_archive_section("人物小传", payload.get("biography")),
            _project_archive_section("性格特征", "、".join(payload.get("traits", []))),
            _project_archive_section("背景经历", payload.get("background")),
            _project_archive_section("目标", payload.get("goals")),
            _project_archive_section("秘密", payload.get("secrets")),
            _project_archive_section("备注", payload.get("notes")),
            _project_archive_section("生命状态", "、".join(payload.get("life_statuses", []))),
            _project_archive_section("首次出场章节", payload.get("first_appearance_chapter_title")),
            _project_archive_section("最后出场章节", payload.get("last_appearance_chapter_title")),
            _project_archive_section("章节时间节点", "\n".join(line for line in timeline_lines if line)),
        ]
    )


def _project_archive_chapter_doc(chapter: dict[str, Any]) -> str:
    return "".join(
        [
            _project_archive_doc_title(chapter.get("title") or "章节"),
            _project_archive_section("标题", chapter.get("title")),
            _project_archive_section("节点类型", chapter.get("node_type")),
            _project_archive_section("状态", chapter.get("status")),
            _project_archive_section("序号", chapter.get("sequence_number")),
            _project_archive_section("排序", chapter.get("sort_order")),
            _project_archive_section("父章节ID", chapter.get("parent_id")),
            _project_archive_section("摘要", chapter.get("summary")),
            _project_archive_section("大纲", chapter.get("outline")),
            _project_archive_section("正文", chapter.get("content")),
            _project_archive_section("上下文摘要", chapter.get("context_summary")),
        ]
    )


def _project_archive_relation_doc(relation: dict[str, Any]) -> str:
    return "".join(
        [
            _project_archive_doc_title(
                f"{relation.get('source_character_name') or '?'} -> {relation.get('target_character_name') or '?'}"
            ),
            _project_archive_section("起点人物", relation.get("source_character_name")),
            _project_archive_section("终点人物", relation.get("target_character_name")),
            _project_archive_section("关系类型", relation.get("relation_type")),
            _project_archive_section("关系标题", relation.get("label")),
            _project_archive_section("关系描述", relation.get("description")),
            _project_archive_section("强度", relation.get("strength")),
            _project_archive_section("是否双向", "是" if relation.get("is_bidirectional") else "否"),
            _project_archive_section("生效起始章节", relation.get("valid_from_chapter_title")),
            _project_archive_section("生效结束章节", relation.get("valid_to_chapter_title")),
        ]
    )


def _project_archive_ai_config_doc(config: dict[str, Any]) -> str:
    return "".join(
        [
            _project_archive_doc_title(config.get("name") or "AI配置"),
            _project_archive_section("配置名称", config.get("name")),
            _project_archive_section("用途模块", config.get("module")),
            _project_archive_section("服务商备注", config.get("provider_name")),
            _project_archive_section("接口地址", config.get("base_url")),
            _project_archive_section("模型名称", config.get("model_name")),
            _project_archive_section("思考强度", config.get("reasoning_effort")),
            _project_archive_section("超时时间", config.get("timeout_seconds")),
            _project_archive_section("优先级", config.get("priority")),
            _project_archive_section("是否启用", "是" if config.get("is_enabled") else "否"),
            _project_archive_section("是否默认", "是" if config.get("is_default") else "否"),
            _project_archive_section("固定提示词模板", config.get("system_prompt_template")),
            _project_archive_section("备注", config.get("notes")),
        ]
    )


def _project_archive_write_text_file(archive: zipfile.ZipFile, path: str, text: str) -> None:
    archive.writestr(path, text.encode("utf-8"))


def _project_archive_write_json_file(archive: zipfile.ZipFile, path: str, payload: Any) -> None:
    archive.writestr(path, json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8"))


def build_book_project_archive_bytes(db: Session, book: Book, current_user: User) -> bytes:
    chapters = db.execute(
        select(Chapter).where(Chapter.book_id == book.id).order_by(Chapter.sort_order.asc(), Chapter.id.asc())
    ).scalars().all()
    characters = db.execute(
        select(Character).where(Character.book_id == book.id).order_by(Character.name.asc(), Character.id.asc())
    ).scalars().all()
    relations = db.execute(
        select(Relation).where(Relation.book_id == book.id).order_by(Relation.id.asc())
    ).scalars().all()
    ai_configs = db.execute(
        select(AIConfig)
        .where(AIConfig.scope == AIScope.BOOK, AIConfig.book_id == book.id)
        .order_by(AIConfig.module.asc(), AIConfig.priority.asc(), AIConfig.id.asc())
    ).scalars().all()

    book_payload = serialize_book(book, include_detail=True)
    chapters_payload = [serialize_chapter_detail(chapter) for chapter in chapters]
    characters_payload = [serialize_character(character) for character in characters]
    relations_payload = [serialize_relation(relation) for relation in relations]
    ai_configs_payload = [
        serialize_ai_config(config, allow_runtime_env=is_admin(current_user), include_sensitive_fields=True)
        for config in ai_configs
    ]
    for item in ai_configs_payload:
        item.pop("has_api_key", None)
        item["api_key_env_var"] = None
        item["base_url_env_var"] = None
        item["model_name_env_var"] = None

    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _project_archive_write_json_file(
            archive,
            "manifest.json",
            {
                "type": "bamboo-book-project",
                "version": "0.2.0",
                "book_id": book.id,
                "book_title": book.title,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "modules": ["book", "prompts", "ai_configs", "chapters", "characters", "relations"],
            },
        )

        _project_archive_write_json_file(archive, f"{PROJECT_ARCHIVE_INFO_DIR}/book.json", book_payload)
        _project_archive_write_text_file(
            archive,
            f"{PROJECT_ARCHIVE_INFO_DIR}/书籍信息.txt",
            "".join(
                [
                    _project_archive_doc_title(book.title or "书籍信息"),
                    _project_archive_section("书名", book.title),
                    _project_archive_section("题材", book.genre),
                    _project_archive_section("语言", book.language),
                    _project_archive_section("标签", "、".join(book.tags or [])),
                    _project_archive_section("简介", book.description),
                    _project_archive_section("状态", book.status.value if isinstance(book.status, BookStatus) else book.status),
                ]
            ),
        )

        _project_archive_write_json_file(
            archive,
            f"{PROJECT_ARCHIVE_PROMPTS_DIR}/prompts.json",
            {
                "global_style_prompt": book.global_style_prompt,
                "long_term_summary": book.long_term_summary,
                "world_bible": book.world_bible,
                "outline": book.outline,
                "extra_data": book.extra_data or {},
            },
        )
        prompt_docs = {
            "写作要求.txt": book.global_style_prompt,
            "长期摘要.txt": book.long_term_summary,
            "世界观手册.txt": book.world_bible,
            "总大纲.txt": book.outline,
        }
        for filename, content in prompt_docs.items():
            _project_archive_write_text_file(
                archive,
                f"{PROJECT_ARCHIVE_PROMPTS_DIR}/{filename}",
                f"{_project_archive_doc_title(filename.replace('.txt', ''))}{_project_archive_text(content)}\n",
            )

        _project_archive_write_json_file(archive, f"{PROJECT_ARCHIVE_AI_CONFIGS_DIR}/ai-configs.json", ai_configs_payload)
        for index, config in enumerate(ai_configs_payload, start=1):
            filename = f"{index:03d}-{_project_archive_safe_name(config.get('name'), 'AI配置')}.txt"
            _project_archive_write_text_file(
                archive,
                f"{PROJECT_ARCHIVE_AI_CONFIGS_DIR}/{filename}",
                _project_archive_ai_config_doc(config),
            )

        _project_archive_write_json_file(archive, f"{PROJECT_ARCHIVE_CHAPTERS_DIR}/chapters.json", chapters_payload)
        for index, chapter in enumerate(chapters_payload, start=1):
            filename = f"{index:03d}-{_project_archive_safe_name(chapter.get('title'), '章节')}.txt"
            _project_archive_write_text_file(
                archive,
                f"{PROJECT_ARCHIVE_CHAPTERS_DIR}/{filename}",
                _project_archive_chapter_doc(chapter),
            )

        _project_archive_write_json_file(archive, f"{PROJECT_ARCHIVE_CHARACTERS_DIR}/characters.json", characters_payload)
        for index, character in enumerate(characters_payload, start=1):
            filename = f"{index:03d}-{_project_archive_safe_name(character.get('name'), '人物')}.txt"
            _project_archive_write_text_file(
                archive,
                f"{PROJECT_ARCHIVE_CHARACTERS_DIR}/{filename}",
                _project_archive_character_doc(character),
            )

        _project_archive_write_json_file(archive, f"{PROJECT_ARCHIVE_RELATIONS_DIR}/relations.json", relations_payload)
        for index, relation in enumerate(relations_payload, start=1):
            filename = (
                f"{index:03d}-{_project_archive_safe_name(relation.get('source_character_name'), '人物')}"
                f"__{_project_archive_safe_name(relation.get('relation_type'), '关系')}"
                f"__{_project_archive_safe_name(relation.get('target_character_name'), '人物')}.txt"
            )
            _project_archive_write_text_file(
                archive,
                f"{PROJECT_ARCHIVE_RELATIONS_DIR}/{filename}",
                _project_archive_relation_doc(relation),
            )

    return bundle.getvalue()


def _project_archive_load_folder_json(zip_file: zipfile.ZipFile, folder: str, filename: str) -> Any:
    path = f"{folder}/{filename}"
    if path not in zip_file.namelist():
        return None
    return _project_archive_bytes_to_json(zip_file.read(path))


def _project_archive_iter_folder_docs(zip_file: zipfile.ZipFile, folder: str) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    prefix = f"{folder}/"
    for name in zip_file.namelist():
        if not name.startswith(prefix):
            continue
        suffix = Path(name).suffix.lower()
        if suffix not in PROJECT_ARCHIVE_ALLOWED_EXTENSIONS:
            continue
        try:
            text = _project_archive_read_zip_text(zip_file.read(name), suffix)
        except HTTPException:
            raise
        items.append((name, text))
    return items


def _project_archive_load_prompts(zip_file: zipfile.ZipFile) -> Optional[dict[str, Any]]:
    payload = _project_archive_load_folder_json(zip_file, PROJECT_ARCHIVE_PROMPTS_DIR, "prompts.json")
    if isinstance(payload, dict):
        return payload

    docs = _project_archive_iter_folder_docs(zip_file, PROJECT_ARCHIVE_PROMPTS_DIR)
    if not docs:
        return None

    result: dict[str, Any] = {}
    for name, text in docs:
        filename = Path(name).name
        sections = _project_archive_parse_sections(text)
        body = "\n\n".join(value for value in sections.values() if value and value != "（空）").strip() or text.strip()
        if "写作要求" in filename:
            result["global_style_prompt"] = body
        elif "长期摘要" in filename:
            result["long_term_summary"] = body
        elif "世界观手册" in filename:
            result["world_bible"] = body
        elif "总大纲" in filename:
            result["outline"] = body
    return result or None


def _project_archive_load_chapters(zip_file: zipfile.ZipFile) -> list[dict[str, Any]]:
    payload = _project_archive_load_folder_json(zip_file, PROJECT_ARCHIVE_CHAPTERS_DIR, "chapters.json")
    if isinstance(payload, list):
        return payload

    chapters: list[dict[str, Any]] = []
    for name, text in _project_archive_iter_folder_docs(zip_file, PROJECT_ARCHIVE_CHAPTERS_DIR):
        sections = _project_archive_parse_sections(text)
        title = sections.get("标题") or Path(name).stem
        chapters.append(
            {
                "title": title.strip(),
                "node_type": sections.get("节点类型") or "chapter",
                "status": sections.get("状态") or "draft",
                "sequence_number": int(sections["序号"]) if str(sections.get("序号") or "").isdigit() else None,
                "sort_order": int(sections["排序"]) if str(sections.get("排序") or "").isdigit() else None,
                "summary": None if sections.get("摘要") in {None, "（空）"} else sections.get("摘要"),
                "outline": None if sections.get("大纲") in {None, "（空）"} else sections.get("大纲"),
                "content": None if sections.get("正文") in {None, "（空）"} else sections.get("正文"),
                "context_summary": None if sections.get("上下文摘要") in {None, "（空）"} else sections.get("上下文摘要"),
                "extra_data": {},
            }
        )
    return chapters


def _project_archive_load_characters(zip_file: zipfile.ZipFile) -> list[dict[str, Any]]:
    payload = _project_archive_load_folder_json(zip_file, PROJECT_ARCHIVE_CHARACTERS_DIR, "characters.json")
    if isinstance(payload, list):
        return payload

    characters: list[dict[str, Any]] = []
    for name, text in _project_archive_iter_folder_docs(zip_file, PROJECT_ARCHIVE_CHARACTERS_DIR):
        sections = _project_archive_parse_sections(text)
        character_name = (sections.get("人物名") or Path(name).stem).strip()
        timeline_entries = []
        for line in (sections.get("章节时间节点") or "").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [part.strip() for part in line.split("|")]
            chapter_label = parts[0] if parts else ""
            chapter_number_match = re.search(r"(\d+)", chapter_label)
            timeline_entries.append(
                {
                    "chapter_label": chapter_label or None,
                    "chapter_number": int(chapter_number_match.group(1)) if chapter_number_match else None,
                    "event": parts[1] if len(parts) > 1 else None,
                    "location": parts[2] if len(parts) > 2 else None,
                    "status": parts[3] if len(parts) > 3 else None,
                }
            )
        characters.append(
            {
                "name": character_name,
                "aliases": _project_archive_merge_string_list([], sections.get("别名")),
                "role_label": None if sections.get("角色标签") in {None, "（空）"} else sections.get("角色标签"),
                "age": None if sections.get("年龄") in {None, "（空）"} else sections.get("年龄"),
                "short_term_goal": None if sections.get("短期目标") in {None, "（空）"} else sections.get("短期目标"),
                "long_term_goal": None if sections.get("长期目标") in {None, "（空）"} else sections.get("长期目标"),
                "motivation": None if sections.get("动机") in {None, "（空）"} else sections.get("动机"),
                "personality": None if sections.get("性格") in {None, "（空）"} else sections.get("性格"),
                "appearance": None if sections.get("外貌") in {None, "（空）"} else sections.get("外貌"),
                "weakness": None if sections.get("弱点") in {None, "（空）"} else sections.get("弱点"),
                "biography": None if sections.get("人物小传") in {None, "（空）"} else sections.get("人物小传"),
                "traits": _project_archive_merge_string_list([], sections.get("性格特征")),
                "background": None if sections.get("背景经历") in {None, "（空）"} else sections.get("背景经历"),
                "goals": None if sections.get("目标") in {None, "（空）"} else sections.get("目标"),
                "secrets": None if sections.get("秘密") in {None, "（空）"} else sections.get("秘密"),
                "notes": None if sections.get("备注") in {None, "（空）"} else sections.get("备注"),
                "is_active": True,
                "life_statuses": _project_archive_merge_string_list([], sections.get("生命状态")),
                "timeline_entries": [item for item in timeline_entries if item.get("chapter_number") and item.get("event")],
                "card_json": {"importance_level": normalize_importance_level(sections.get("重要程度"))},
                "first_appearance_chapter_title": None if sections.get("首次出场章节") in {None, "（空）"} else sections.get("首次出场章节"),
                "last_appearance_chapter_title": None if sections.get("最后出场章节") in {None, "（空）"} else sections.get("最后出场章节"),
            }
        )
    return characters


def _project_archive_load_relations(zip_file: zipfile.ZipFile) -> list[dict[str, Any]]:
    payload = _project_archive_load_folder_json(zip_file, PROJECT_ARCHIVE_RELATIONS_DIR, "relations.json")
    if isinstance(payload, list):
        return payload

    relations: list[dict[str, Any]] = []
    for name, text in _project_archive_iter_folder_docs(zip_file, PROJECT_ARCHIVE_RELATIONS_DIR):
        sections = _project_archive_parse_sections(text)
        relation_name = Path(name).stem.split("__")
        relations.append(
            {
                "source_character_name": sections.get("起点人物") or (relation_name[0] if relation_name else None),
                "target_character_name": sections.get("终点人物") or (relation_name[-1] if len(relation_name) >= 3 else None),
                "relation_type": sections.get("关系类型") or (relation_name[1] if len(relation_name) >= 3 else "关系"),
                "label": None if sections.get("关系标题") in {None, "（空）"} else sections.get("关系标题"),
                "description": None if sections.get("关系描述") in {None, "（空）"} else sections.get("关系描述"),
                "strength": float(sections["强度"]) if str(sections.get("强度") or "").replace(".", "", 1).isdigit() else None,
                "is_bidirectional": str(sections.get("是否双向") or "").strip() in {"是", "true", "True", "1"},
                "valid_from_chapter_title": None if sections.get("生效起始章节") in {None, "（空）"} else sections.get("生效起始章节"),
                "valid_to_chapter_title": None if sections.get("生效结束章节") in {None, "（空）"} else sections.get("生效结束章节"),
                "extra_data": {},
            }
        )
    return relations


def _project_archive_load_ai_configs(zip_file: zipfile.ZipFile) -> list[dict[str, Any]]:
    payload = _project_archive_load_folder_json(zip_file, PROJECT_ARCHIVE_AI_CONFIGS_DIR, "ai-configs.json")
    return payload if isinstance(payload, list) else []


def _project_archive_validate_strategy(strategy: str) -> str:
    if strategy not in PROJECT_IMPORT_ALLOWED_STRATEGIES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不支持的导入策略。")
    return strategy


def _project_archive_read_payload(file_bytes: bytes) -> dict[str, Any]:
    try:
        zip_file = zipfile.ZipFile(io.BytesIO(file_bytes))
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="导入文件不是有效的 ZIP 压缩包。") from exc

    manifest = None
    if "manifest.json" in zip_file.namelist():
        manifest = _project_archive_bytes_to_json(zip_file.read("manifest.json"))

    book_payload = _project_archive_load_folder_json(zip_file, PROJECT_ARCHIVE_INFO_DIR, "book.json")
    prompts_payload = _project_archive_load_prompts(zip_file)
    ai_configs_payload = _project_archive_load_ai_configs(zip_file)
    chapters_payload = _project_archive_load_chapters(zip_file)
    characters_payload = _project_archive_load_characters(zip_file)
    relations_payload = _project_archive_load_relations(zip_file)

    modules_detected: list[str] = []
    if isinstance(book_payload, dict):
        modules_detected.append("book")
    if prompts_payload:
        modules_detected.append("prompts")
    if ai_configs_payload:
        modules_detected.append("ai_configs")
    if chapters_payload:
        modules_detected.append("chapters")
    if characters_payload:
        modules_detected.append("characters")
    if relations_payload:
        modules_detected.append("relations")

    return {
        "manifest": manifest if isinstance(manifest, dict) else {},
        "modules_detected": modules_detected,
        "book": book_payload if isinstance(book_payload, dict) else None,
        "prompts": prompts_payload if isinstance(prompts_payload, dict) else None,
        "ai_configs": ai_configs_payload,
        "chapters": chapters_payload,
        "characters": characters_payload,
        "relations": relations_payload,
    }


def _project_archive_metadata_conflict_id() -> str:
    return "book:metadata"


def _project_archive_prompts_conflict_id() -> str:
    return "prompts:global"


def _project_archive_ai_config_conflict_id(name: str, module: str) -> str:
    return f"ai_config:{name}:{module}"


def _project_archive_chapter_conflict_id(title: str) -> str:
    return f"chapter:{title}"


def _project_archive_character_conflict_id(name: str) -> str:
    return f"character:{name}"


def _project_archive_relation_conflict_id(source: str, relation_type: str, target: str) -> str:
    return f"relation:{source}->{relation_type}->{target}"


def _project_archive_has_meaningful_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(_project_archive_text(value))
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _project_archive_normalize_compare_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key in sorted(value):
            item = _project_archive_normalize_compare_value(value[key])
            if item in (None, "", [], {}):
                continue
            normalized[str(key)] = item
        return normalized
    if isinstance(value, (list, tuple, set)):
        normalized_items = [_project_archive_normalize_compare_value(item) for item in value]
        filtered_items = [item for item in normalized_items if item not in (None, "", [], {})]
        if all(isinstance(item, str) for item in filtered_items):
            return sorted(filtered_items)
        return filtered_items
    return str(value).strip()


def _project_archive_preview_value(value: Any, *, max_chars: int = 120) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    elif isinstance(value, (list, tuple, set)):
        parts = [_project_archive_text(item) for item in value if _project_archive_text(item)]
        text = "、".join(parts)
    else:
        text = _project_archive_text(value)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "（空）"
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 1].rstrip()}…"


def _project_archive_build_changed_fields(
    existing_payload: dict[str, Any],
    incoming_payload: dict[str, Any],
    field_specs: list[tuple[str, str]],
) -> tuple[list[dict[str, str]], bool]:
    changed_fields: list[dict[str, str]] = []
    has_existing = any(_project_archive_has_meaningful_value(existing_payload.get(key)) for key, _label in field_specs)
    for key, label in field_specs:
        current_value = existing_payload.get(key)
        incoming_value = incoming_payload.get(key)
        if _project_archive_normalize_compare_value(current_value) == _project_archive_normalize_compare_value(incoming_value):
            continue
        changed_fields.append(
            {
                "label": label,
                "current": _project_archive_preview_value(current_value),
                "incoming": _project_archive_preview_value(incoming_value),
            }
        )
    return changed_fields, has_existing


def _project_archive_preview_status(has_existing: bool, changed_fields: list[dict[str, str]]) -> str:
    if not changed_fields:
        return "same"
    return "conflict" if has_existing else "new"


def _project_archive_recommended_decision(status_value: str) -> str:
    if status_value == "new":
        return "replace_existing"
    if status_value == "same":
        return "keep_existing"
    return "smart_merge"


def _project_archive_build_preview_item(
    *,
    conflict_id: str,
    module: str,
    title: str,
    description: str,
    existing_payload: dict[str, Any],
    incoming_payload: dict[str, Any],
    field_specs: list[tuple[str, str]],
) -> dict[str, Any]:
    changed_fields, has_existing = _project_archive_build_changed_fields(existing_payload, incoming_payload, field_specs)
    status_value = _project_archive_preview_status(has_existing, changed_fields)
    return {
        "conflict_id": conflict_id,
        "module": module,
        "title": title,
        "description": description,
        "status": status_value,
        "recommended_decision": _project_archive_recommended_decision(status_value),
        "existing_summary": _project_archive_preview_value(existing_payload),
        "incoming_summary": _project_archive_preview_value(incoming_payload),
        "changed_fields": changed_fields[:8],
    }


def _project_archive_existing_book_payload(book: Book) -> dict[str, Any]:
    return {
        "title": book.title,
        "description": book.description,
        "genre": book.genre,
        "language": book.language,
        "tags": book.tags or [],
    }


def _project_archive_existing_prompts_payload(book: Book) -> dict[str, Any]:
    return {
        "global_style_prompt": book.global_style_prompt,
        "long_term_summary": book.long_term_summary,
        "world_bible": book.world_bible,
        "outline": book.outline,
    }


def _project_archive_existing_ai_config_payload(config: AIConfig) -> dict[str, Any]:
    return {
        "name": config.name,
        "module": config.module.value if isinstance(config.module, AIModule) else str(config.module),
        "provider_name": config.provider_name,
        "base_url": config.base_url,
        "model_name": config.model_name,
        "reasoning_effort": config.reasoning_effort,
        "timeout_seconds": config.timeout_seconds,
        "priority": config.priority,
        "is_enabled": config.is_enabled,
        "is_default": config.is_default,
        "system_prompt_template": config.system_prompt_template,
        "notes": config.notes,
    }


def _project_archive_incoming_ai_config_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": _project_archive_text(payload.get("name")),
        "module": _project_archive_text(payload.get("module")),
        "provider_name": payload.get("provider_name"),
        "base_url": payload.get("base_url"),
        "model_name": payload.get("model_name"),
        "reasoning_effort": payload.get("reasoning_effort"),
        "timeout_seconds": payload.get("timeout_seconds"),
        "priority": payload.get("priority"),
        "is_enabled": payload.get("is_enabled"),
        "is_default": payload.get("is_default"),
        "system_prompt_template": payload.get("system_prompt_template"),
        "notes": payload.get("notes"),
    }


def _project_archive_existing_chapter_payload(chapter: Chapter) -> dict[str, Any]:
    return {
        "title": chapter.title,
        "node_type": chapter.node_type.value if isinstance(chapter.node_type, ChapterNodeType) else str(chapter.node_type),
        "status": chapter.status.value if isinstance(chapter.status, ChapterStatus) else str(chapter.status),
        "sequence_number": chapter.sequence_number,
        "sort_order": chapter.sort_order,
        "summary": chapter.summary,
        "outline": chapter.outline,
        "content": chapter.content,
        "context_summary": chapter.context_summary,
    }


def _project_archive_incoming_chapter_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": _project_archive_text(payload.get("title")),
        "node_type": _project_archive_text(payload.get("node_type")),
        "status": _project_archive_text(payload.get("status")),
        "sequence_number": payload.get("sequence_number"),
        "sort_order": payload.get("sort_order"),
        "summary": payload.get("summary"),
        "outline": payload.get("outline"),
        "content": payload.get("content"),
        "context_summary": payload.get("context_summary"),
    }


def _project_archive_existing_character_payload(character: Character) -> dict[str, Any]:
    payload = serialize_character(character)
    card_json = payload.get("card_json") or {}
    return {
        "name": payload.get("name"),
        "importance_level": card_json.get("importance_level"),
        "role_label": payload.get("role_label"),
        "aliases": payload.get("aliases") or [],
        "age": payload.get("age"),
        "short_term_goal": payload.get("short_term_goal"),
        "long_term_goal": payload.get("long_term_goal"),
        "motivation": payload.get("motivation"),
        "personality": payload.get("personality"),
        "appearance": payload.get("appearance"),
        "weakness": payload.get("weakness"),
        "biography": payload.get("biography"),
        "traits": payload.get("traits") or [],
        "background": payload.get("background"),
        "goals": payload.get("goals"),
        "secrets": payload.get("secrets"),
        "notes": payload.get("notes"),
        "life_statuses": payload.get("life_statuses") or [],
        "timeline_entries": payload.get("timeline_entries") or [],
        "first_appearance_chapter_title": payload.get("first_appearance_chapter_title"),
        "last_appearance_chapter_title": payload.get("last_appearance_chapter_title"),
    }


def _project_archive_preview_character_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = _project_archive_character_payload(payload)
    return {
        "name": normalized.get("name"),
        "importance_level": (normalized.get("card_json") or {}).get("importance_level"),
        "role_label": normalized.get("role_label"),
        "aliases": normalized.get("aliases") or [],
        "age": normalized.get("age"),
        "short_term_goal": normalized.get("short_term_goal"),
        "long_term_goal": normalized.get("long_term_goal"),
        "motivation": normalized.get("motivation"),
        "personality": normalized.get("personality"),
        "appearance": normalized.get("appearance"),
        "weakness": normalized.get("weakness"),
        "biography": normalized.get("biography"),
        "traits": normalized.get("traits") or [],
        "background": normalized.get("background"),
        "goals": normalized.get("goals"),
        "secrets": normalized.get("secrets"),
        "notes": normalized.get("notes"),
        "life_statuses": normalized.get("life_statuses") or [],
        "timeline_entries": normalized.get("timeline_entries") or [],
        "first_appearance_chapter_title": normalized.get("first_appearance_chapter_title"),
        "last_appearance_chapter_title": normalized.get("last_appearance_chapter_title"),
    }


def _project_archive_existing_relation_payload(relation: Relation) -> dict[str, Any]:
    payload = serialize_relation(relation)
    return {
        "source_character_name": payload.get("source_character_name"),
        "target_character_name": payload.get("target_character_name"),
        "relation_type": payload.get("relation_type"),
        "label": payload.get("label"),
        "description": payload.get("description"),
        "strength": payload.get("strength"),
        "is_bidirectional": payload.get("is_bidirectional"),
        "valid_from_chapter_title": payload.get("valid_from_chapter_title"),
        "valid_to_chapter_title": payload.get("valid_to_chapter_title"),
    }


def _project_archive_preview_relation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_character_name": _project_archive_text(payload.get("source_character_name")),
        "target_character_name": _project_archive_text(payload.get("target_character_name")),
        "relation_type": sanitize_relation_text(payload.get("relation_type"), fallback="关系") or "关系",
        "label": sanitize_relation_text(payload.get("label")),
        "description": normalize_relation_description(payload.get("description")),
        "strength": payload.get("strength"),
        "is_bidirectional": bool(payload.get("is_bidirectional")),
        "valid_from_chapter_title": _project_archive_text(payload.get("valid_from_chapter_title")) or None,
        "valid_to_chapter_title": _project_archive_text(payload.get("valid_to_chapter_title")) or None,
    }


def _project_import_cleanup_sessions() -> None:
    if not PROJECT_IMPORT_SESSION_DIR.exists():
        return
    threshold = time.time() - PROJECT_IMPORT_SESSION_MAX_AGE_SECONDS
    for path in PROJECT_IMPORT_SESSION_DIR.glob("*.json"):
        try:
            if path.stat().st_mtime < threshold:
                path.unlink()
        except OSError:
            continue


def _project_import_session_path(session_id: str) -> Path:
    normalized = str(session_id or "").strip()
    if not re.fullmatch(r"[a-f0-9]{32}", normalized):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="导入会话不存在或已失效。")
    return PROJECT_IMPORT_SESSION_DIR / f"{normalized}.json"


def save_book_project_import_session(*, book: Book, current_user: User, archive_payload: dict[str, Any], preview: dict[str, Any]) -> str:
    _project_import_cleanup_sessions()
    PROJECT_IMPORT_SESSION_DIR.mkdir(parents=True, exist_ok=True)
    session_id = uuid4().hex
    payload = {
        "session_id": session_id,
        "book_id": book.id,
        "user_id": current_user.id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "archive_payload": archive_payload,
        "preview": preview,
    }
    _project_import_session_path(session_id).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return session_id


def load_book_project_import_session(session_id: str) -> dict[str, Any]:
    path = _project_import_session_path(session_id)
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="导入会话不存在或已失效。")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="导入会话数据已损坏。") from exc


def delete_book_project_import_session(session_id: str) -> None:
    path = _project_import_session_path(session_id)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("project_import_session_cleanup_failed session_id=%s", session_id)


def build_book_project_import_preview(db: Session, book: Book, archive_payload: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    warnings: list[str] = []

    book_payload = archive_payload.get("book")
    if isinstance(book_payload, dict):
        items.append(
            _project_archive_build_preview_item(
                conflict_id=_project_archive_metadata_conflict_id(),
                module="book",
                title="书籍信息",
                description="书名、简介、题材、语言、标签",
                existing_payload=_project_archive_existing_book_payload(book),
                incoming_payload={
                    "title": book_payload.get("title"),
                    "description": book_payload.get("description"),
                    "genre": book_payload.get("genre"),
                    "language": book_payload.get("language"),
                    "tags": book_payload.get("tags") or [],
                },
                field_specs=[
                    ("title", "书名"),
                    ("description", "简介"),
                    ("genre", "题材"),
                    ("language", "语言"),
                    ("tags", "标签"),
                ],
            )
        )

    prompts_payload = archive_payload.get("prompts")
    if isinstance(prompts_payload, dict):
        items.append(
            _project_archive_build_preview_item(
                conflict_id=_project_archive_prompts_conflict_id(),
                module="prompts",
                title="提示词与书籍记忆",
                description="写作要求、长期摘要、世界观手册、总大纲",
                existing_payload=_project_archive_existing_prompts_payload(book),
                incoming_payload={
                    "global_style_prompt": prompts_payload.get("global_style_prompt"),
                    "long_term_summary": prompts_payload.get("long_term_summary"),
                    "world_bible": prompts_payload.get("world_bible"),
                    "outline": prompts_payload.get("outline"),
                },
                field_specs=[
                    ("global_style_prompt", "写作要求"),
                    ("long_term_summary", "长期摘要"),
                    ("world_bible", "世界观手册"),
                    ("outline", "总大纲"),
                ],
            )
        )

    existing_configs = db.execute(
        select(AIConfig).where(AIConfig.scope == AIScope.BOOK, AIConfig.book_id == book.id)
    ).scalars().all()
    existing_config_map = {
        (item.name, item.module.value if isinstance(item.module, AIModule) else str(item.module)): item
        for item in existing_configs
    }
    for incoming in archive_payload.get("ai_configs") or []:
        incoming_payload = _project_archive_incoming_ai_config_payload(incoming)
        name = incoming_payload.get("name")
        module_value = incoming_payload.get("module")
        if not name or not module_value:
            warnings.append("跳过一条缺少名称或模块的 AI 配置。")
            continue
        existing = existing_config_map.get((name, module_value))
        items.append(
            _project_archive_build_preview_item(
                conflict_id=_project_archive_ai_config_conflict_id(name, module_value),
                module="ai_configs",
                title=f"AI 配置：{name}",
                description=f"模块：{module_value}",
                existing_payload=_project_archive_existing_ai_config_payload(existing) if existing else {},
                incoming_payload=incoming_payload,
                field_specs=[
                    ("provider_name", "服务商备注"),
                    ("base_url", "接口地址"),
                    ("model_name", "模型名称"),
                    ("reasoning_effort", "思考强度"),
                    ("timeout_seconds", "超时时间"),
                    ("priority", "优先级"),
                    ("is_enabled", "是否启用"),
                    ("is_default", "是否默认"),
                    ("system_prompt_template", "固定提示词模板"),
                    ("notes", "备注"),
                ],
            )
        )

    existing_chapter_map = {
        chapter.title: chapter for chapter in db.execute(select(Chapter).where(Chapter.book_id == book.id)).scalars().all()
    }
    for incoming in archive_payload.get("chapters") or []:
        incoming_payload = _project_archive_incoming_chapter_payload(incoming)
        title = incoming_payload.get("title")
        if not title:
            warnings.append("跳过一条缺少标题的章节。")
            continue
        existing = existing_chapter_map.get(title)
        items.append(
            _project_archive_build_preview_item(
                conflict_id=_project_archive_chapter_conflict_id(title),
                module="chapters",
                title=f"章节：{title}",
                description="正文、摘要、上下文摘要、排序等",
                existing_payload=_project_archive_existing_chapter_payload(existing) if existing else {},
                incoming_payload=incoming_payload,
                field_specs=[
                    ("node_type", "节点类型"),
                    ("status", "状态"),
                    ("sequence_number", "序号"),
                    ("sort_order", "排序"),
                    ("summary", "摘要"),
                    ("outline", "大纲"),
                    ("content", "正文"),
                    ("context_summary", "上下文摘要"),
                ],
            )
        )

    existing_character_map = {
        character.name: character for character in db.execute(select(Character).where(Character.book_id == book.id)).scalars().all()
    }
    for incoming in archive_payload.get("characters") or []:
        incoming_payload = _project_archive_preview_character_payload(incoming)
        name = incoming_payload.get("name")
        if not name:
            warnings.append("跳过一条缺少人物名的人物卡。")
            continue
        existing = existing_character_map.get(name)
        items.append(
            _project_archive_build_preview_item(
                conflict_id=_project_archive_character_conflict_id(name),
                module="characters",
                title=f"人物卡：{name}",
                description="人物设定、主次、生命状态、章节时间节点",
                existing_payload=_project_archive_existing_character_payload(existing) if existing else {},
                incoming_payload=incoming_payload,
                field_specs=[
                    ("importance_level", "重要程度"),
                    ("role_label", "角色标签"),
                    ("aliases", "别名"),
                    ("age", "年龄"),
                    ("short_term_goal", "短期目标"),
                    ("long_term_goal", "长期目标"),
                    ("motivation", "动机"),
                    ("personality", "性格"),
                    ("appearance", "外貌"),
                    ("weakness", "弱点"),
                    ("biography", "人物小传"),
                    ("traits", "性格特征"),
                    ("background", "背景经历"),
                    ("goals", "目标"),
                    ("secrets", "秘密"),
                    ("notes", "备注"),
                    ("life_statuses", "生命状态"),
                    ("timeline_entries", "章节时间节点"),
                    ("first_appearance_chapter_title", "首次出场章节"),
                    ("last_appearance_chapter_title", "最后出场章节"),
                ],
            )
        )

    existing_relations = db.execute(select(Relation).where(Relation.book_id == book.id)).scalars().all()
    relation_map = {
        (
            relation.source_character.name if relation.source_character else "",
            relation.target_character.name if relation.target_character else "",
            sanitize_relation_text(relation.relation_type, fallback="关系") or "关系",
        ): relation
        for relation in existing_relations
    }
    for incoming in archive_payload.get("relations") or []:
        incoming_payload = _project_archive_preview_relation_payload(incoming)
        source_name = incoming_payload.get("source_character_name")
        target_name = incoming_payload.get("target_character_name")
        relation_type = incoming_payload.get("relation_type")
        if not source_name or not target_name:
            warnings.append("跳过一条缺少起点或终点人物的人物关系。")
            continue
        existing = relation_map.get((source_name, target_name, relation_type))
        items.append(
            _project_archive_build_preview_item(
                conflict_id=_project_archive_relation_conflict_id(source_name, relation_type, target_name),
                module="relations",
                title=f"人物关系：{source_name} -> {target_name}",
                description=f"关系词：{relation_type}",
                existing_payload=_project_archive_existing_relation_payload(existing) if existing else {},
                incoming_payload=incoming_payload,
                field_specs=[
                    ("label", "关系标题"),
                    ("description", "关系描述"),
                    ("strength", "强度"),
                    ("is_bidirectional", "是否双向"),
                    ("valid_from_chapter_title", "生效起始章节"),
                    ("valid_to_chapter_title", "生效结束章节"),
                ],
            )
        )

    counts = {
        "new": sum(1 for item in items if item["status"] == "new"),
        "conflict": sum(1 for item in items if item["status"] == "conflict"),
        "same": sum(1 for item in items if item["status"] == "same"),
        "total": len(items),
    }
    return {
        "modules_detected": archive_payload.get("modules_detected") or [],
        "counts": counts,
        "items": items,
        "warnings": warnings,
    }


def _project_archive_book_update_payload(book: Book, incoming: dict[str, Any], strategy: str) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    fields = ("title", "description", "genre", "language", "global_style_prompt", "long_term_summary", "world_bible", "outline")
    current_map = serialize_book(book, include_detail=True)
    for field in fields:
        if field not in incoming:
            continue
        value = _project_archive_pick_text(current_map.get(field), incoming.get(field), strategy)
        if value or field == "title":
            updates[field] = value
    if "tags" in incoming:
        updates["tags"] = (
            incoming.get("tags") or []
            if strategy == "replace_existing"
            else _project_archive_merge_string_list(book.tags or [], incoming.get("tags") or [])
        )
    extra_data = dict(book.extra_data or {})
    if isinstance(incoming.get("extra_data"), dict):
        extra_data.update(incoming.get("extra_data") or {})
        updates["extra_data"] = extra_data
    return updates


def _project_archive_apply_book_prompts(book: Book, prompts: dict[str, Any], strategy: str) -> None:
    if not isinstance(prompts, dict):
        return
    for field in ("global_style_prompt", "long_term_summary", "world_bible", "outline"):
        if field in prompts:
            setattr(book, field, _project_archive_pick_text(getattr(book, field), prompts.get(field), strategy))
    if isinstance(prompts.get("extra_data"), dict):
        merged_extra_data = dict(book.extra_data or {})
        merged_extra_data.update(prompts.get("extra_data") or {})
        book.extra_data = merged_extra_data


def apply_book_project_archive_payload(
    *,
    db: Session,
    book: Book,
    current_user: User,
    archive_payload: dict[str, Any],
    merge_strategy: str,
    decisions: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    merge_strategy = _project_archive_validate_strategy(merge_strategy)
    raw_decisions = decisions or {}
    normalized_decisions = {key: _project_archive_validate_strategy(value) for key, value in raw_decisions.items()}
    report = {
        "merge_strategy": merge_strategy,
        "modules_detected": list(archive_payload.get("modules_detected") or []),
        "book": {"updated": False},
        "ai_configs": {"created": 0, "updated": 0, "skipped": 0},
        "chapters": {"created": 0, "updated": 0, "skipped": 0},
        "characters": {"created": 0, "updated": 0, "skipped": 0},
        "relations": {"created": 0, "updated": 0, "skipped": 0},
        "warnings": [],
    }

    book_payload = archive_payload.get("book")
    prompts_payload = archive_payload.get("prompts")
    ai_configs_payload = archive_payload.get("ai_configs") or []
    chapters_payload = archive_payload.get("chapters") or []
    characters_payload = archive_payload.get("characters") or []
    relations_payload = archive_payload.get("relations") or []

    book_metadata_strategy = normalized_decisions.get(_project_archive_metadata_conflict_id(), merge_strategy)
    prompts_strategy = normalized_decisions.get(_project_archive_prompts_conflict_id(), merge_strategy)

    if isinstance(book_payload, dict) or prompts_payload:
        updates = (
            _project_archive_book_update_payload(book, book_payload or {}, book_metadata_strategy)
            if isinstance(book_payload, dict) and normalized_decisions.get(_project_archive_metadata_conflict_id()) != "keep_existing"
            else {}
        )
        for key, value in updates.items():
            setattr(book, key, value)
        if prompts_payload and normalized_decisions.get(_project_archive_prompts_conflict_id()) != "keep_existing":
            _project_archive_apply_book_prompts(book, prompts_payload, prompts_strategy)
        db.add(book)
        report["book"]["updated"] = bool(
            updates
            or (prompts_payload and normalized_decisions.get(_project_archive_prompts_conflict_id()) != "keep_existing")
        )

    if ai_configs_payload:
        existing_configs = db.execute(
            select(AIConfig).where(AIConfig.scope == AIScope.BOOK, AIConfig.book_id == book.id)
        ).scalars().all()
        existing_map = {(item.name, item.module.value if isinstance(item.module, AIModule) else str(item.module)): item for item in existing_configs}
        for incoming in ai_configs_payload:
            module_value = str(incoming.get("module") or "").strip()
            if not module_value:
                report["ai_configs"]["skipped"] += 1
                continue
            try:
                module_enum = AIModule(module_value)
            except Exception:
                report["warnings"].append(f"跳过未知 AI 配置模块：{module_value}")
                report["ai_configs"]["skipped"] += 1
                continue

            key = (str(incoming.get("name") or "").strip(), module_enum.value)
            if not key[0]:
                report["ai_configs"]["skipped"] += 1
                continue

            existing = existing_map.get(key)
            item_strategy = normalized_decisions.get(_project_archive_ai_config_conflict_id(key[0], module_enum.value))
            if item_strategy == "keep_existing":
                report["ai_configs"]["skipped"] += 1
                continue
            effective_strategy = item_strategy or merge_strategy
            payload = {
                "name": key[0],
                "scope": AIScope.BOOK,
                "module": module_enum,
                "book_id": book.id,
                "provider_name": _project_archive_pick_text(existing.provider_name if existing else None, incoming.get("provider_name"), effective_strategy),
                "api_format": _project_archive_pick_text(existing.api_format if existing else "openai_v1", incoming.get("api_format"), effective_strategy) or "openai_v1",
                "base_url": _project_archive_pick_text(existing.base_url if existing else None, incoming.get("base_url"), effective_strategy),
                "model_name": _project_archive_pick_text(existing.model_name if existing else None, incoming.get("model_name"), effective_strategy),
                "reasoning_effort": _project_archive_pick_text(existing.reasoning_effort if existing else None, incoming.get("reasoning_effort"), effective_strategy),
                "timeout_seconds": int(_project_archive_choose_number(existing.timeout_seconds if existing else 120, incoming.get("timeout_seconds"), effective_strategy) or 120),
                "priority": int(_project_archive_choose_number(existing.priority if existing else 100, incoming.get("priority"), effective_strategy) or 100),
                "is_enabled": _project_archive_choose_bool(existing.is_enabled if existing else True, incoming.get("is_enabled"), effective_strategy),
                "is_default": _project_archive_choose_bool(existing.is_default if existing else False, incoming.get("is_default"), effective_strategy),
                "system_prompt_template": _project_archive_pick_text(existing.system_prompt_template if existing else None, incoming.get("system_prompt_template"), effective_strategy),
                "notes": _project_archive_pick_text(existing.notes if existing else None, incoming.get("notes"), effective_strategy),
                "extra_headers": dict(existing.extra_headers or {}) if existing and effective_strategy != "replace_existing" else {},
                "extra_body": dict(existing.extra_body or {}) if existing and effective_strategy != "replace_existing" else {},
            }
            if isinstance(incoming.get("extra_headers"), dict):
                payload["extra_headers"].update(incoming.get("extra_headers") or {})
            if isinstance(incoming.get("extra_body"), dict):
                payload["extra_body"].update(incoming.get("extra_body") or {})

            if existing is None:
                config = AIConfig(**payload, api_key=None)
                db.add(config)
                existing_map[key] = config
                report["ai_configs"]["created"] += 1
            else:
                for field, value in payload.items():
                    setattr(existing, field, value)
                db.add(existing)
                report["ai_configs"]["updated"] += 1

    chapter_title_map = {chapter.title: chapter for chapter in db.execute(select(Chapter).where(Chapter.book_id == book.id)).scalars().all()}
    imported_chapter_map: dict[str, Chapter] = dict(chapter_title_map)
    if chapters_payload:
        sorted_payload = sorted(chapters_payload, key=lambda item: (item.get("depth") or 0, item.get("sort_order") or 0, item.get("id") or 0))
        for incoming in sorted_payload:
            title = str(incoming.get("title") or "").strip()
            if not title:
                report["chapters"]["skipped"] += 1
                continue
            existing = chapter_title_map.get(title)
            item_strategy = normalized_decisions.get(_project_archive_chapter_conflict_id(title))
            if item_strategy == "keep_existing" and existing is not None:
                report["chapters"]["skipped"] += 1
                imported_chapter_map[title] = existing
                continue
            if item_strategy == "keep_existing" and existing is None:
                report["chapters"]["skipped"] += 1
                continue
            effective_strategy = item_strategy or merge_strategy
            parent = None
            parent_title = None
            if isinstance(incoming.get("parent_id"), int):
                parent_title = next((item.get("title") for item in sorted_payload if item.get("id") == incoming.get("parent_id")), None)
            if parent_title:
                parent = imported_chapter_map.get(parent_title)
            node_type = incoming.get("node_type") or ChapterNodeType.CHAPTER.value
            status_value = incoming.get("status") or ChapterStatus.DRAFT.value
            try:
                node_type_enum = ChapterNodeType(node_type)
            except Exception:
                node_type_enum = ChapterNodeType.CHAPTER
            try:
                status_enum = ChapterStatus(status_value)
            except Exception:
                status_enum = ChapterStatus.DRAFT

            merged = {
                "summary": _project_archive_pick_text(existing.summary if existing else None, incoming.get("summary"), effective_strategy),
                "outline": _project_archive_pick_text(existing.outline if existing else None, incoming.get("outline"), effective_strategy),
                "content": _project_archive_pick_text(existing.content if existing else None, incoming.get("content"), effective_strategy),
                "context_summary": _project_archive_pick_text(existing.context_summary if existing else None, incoming.get("context_summary"), effective_strategy),
            }

            if existing is None:
                chapter = Chapter(
                    book_id=book.id,
                    parent_id=parent.id if parent else None,
                    title=title,
                    node_type=node_type_enum,
                    status=status_enum,
                    sequence_number=incoming.get("sequence_number"),
                    sort_order=incoming.get("sort_order") if incoming.get("sort_order") is not None else next_chapter_sort_order(db, book.id, parent.id if parent else None),
                    depth=0,
                    tree_path="",
                    summary=merged["summary"],
                    outline=merged["outline"] or "",
                    content=merged["content"] or "",
                    context_summary=merged["context_summary"],
                    word_count=estimate_text_units(merged["content"] or ""),
                    version=1,
                    extra_data=incoming.get("extra_data") if isinstance(incoming.get("extra_data"), dict) else {},
                )
                db.add(chapter)
                db.flush()
                rebuild_chapter_tree(db, chapter)
                chapter_title_map[title] = chapter
                imported_chapter_map[title] = chapter
                report["chapters"]["created"] += 1
            else:
                existing.parent_id = parent.id if parent else existing.parent_id
                existing.node_type = node_type_enum if effective_strategy == "replace_existing" or not existing.node_type else existing.node_type
                existing.status = status_enum if effective_strategy == "replace_existing" or not existing.status else existing.status
                if effective_strategy != "keep_existing" or existing.sequence_number is None:
                    existing.sequence_number = incoming.get("sequence_number") if incoming.get("sequence_number") is not None else existing.sequence_number
                if effective_strategy != "keep_existing" or existing.sort_order is None:
                    existing.sort_order = incoming.get("sort_order") if incoming.get("sort_order") is not None else existing.sort_order
                existing.summary = merged["summary"]
                existing.outline = merged["outline"] or ""
                existing.content = merged["content"] or ""
                existing.context_summary = merged["context_summary"]
                existing.word_count = estimate_text_units(existing.content or "")
                if isinstance(incoming.get("extra_data"), dict):
                    extra_data = dict(existing.extra_data or {})
                    extra_data.update(incoming.get("extra_data") or {})
                    existing.extra_data = extra_data
                db.add(existing)
                db.flush()
                rebuild_chapter_tree(db, existing)
                imported_chapter_map[title] = existing
                report["chapters"]["updated"] += 1
        refresh_book_aggregates(db, book)

    chapter_title_lookup = {chapter.title: chapter.id for chapter in db.execute(select(Chapter).where(Chapter.book_id == book.id)).scalars().all()}

    character_name_map = {character.name: character for character in db.execute(select(Character).where(Character.book_id == book.id)).scalars().all()}
    if characters_payload:
        for incoming in characters_payload:
            payload = _project_archive_character_payload(incoming)
            if not payload["name"]:
                report["characters"]["skipped"] += 1
                continue
            existing = character_name_map.get(payload["name"])
            item_strategy = normalized_decisions.get(_project_archive_character_conflict_id(payload["name"]))
            if item_strategy == "keep_existing" and existing is not None:
                report["characters"]["skipped"] += 1
                continue
            if item_strategy == "keep_existing" and existing is None:
                report["characters"]["skipped"] += 1
                continue
            effective_strategy = item_strategy or merge_strategy
            existing_card_json = merge_character_card_json(existing.card_json) if existing else {}
            incoming_card_json = payload["card_json"]
            card_json_base = (
                incoming_card_json
                if existing is None or effective_strategy == "replace_existing"
                else existing_card_json
            )
            merged_card_json = merge_character_card_json(
                card_json_base,
                life_statuses=(
                    payload["life_statuses"]
                    if effective_strategy == "replace_existing"
                    else normalize_character_life_statuses((existing_card_json.get("life_statuses") or []) + (payload["life_statuses"] or []))
                ),
                timeline_entries=(
                    payload["timeline_entries"]
                    if effective_strategy == "replace_existing"
                    else normalize_character_timeline_entries((existing_card_json.get("timeline_entries") or []) + (payload["timeline_entries"] or []))
                ),
            )
            importance_level = normalize_importance_level(
                _project_archive_pick_text(existing_card_json.get("importance_level") if existing else None, incoming_card_json.get("importance_level"), effective_strategy)
            )
            if importance_level:
                merged_card_json["importance_level"] = importance_level

            merged_payload = {
                "aliases": _project_archive_merge_string_list(existing.aliases if existing else [], payload.get("aliases") or []),
                "role_label": _project_archive_pick_text(existing.role_label if existing else None, payload.get("role_label"), effective_strategy),
                "description": _project_archive_pick_text(existing.description if existing else None, payload.get("biography"), effective_strategy),
                "traits": _project_archive_merge_string_list(existing.traits if existing else [], payload.get("traits") or []),
                "background": _project_archive_pick_text(existing.background if existing else None, payload.get("background"), effective_strategy),
                "goals": _project_archive_pick_text(existing.goals if existing else None, payload.get("goals"), effective_strategy),
                "secrets": _project_archive_pick_text(existing.secrets if existing else None, payload.get("secrets"), effective_strategy),
                "notes": _project_archive_pick_text(existing.notes if existing else None, payload.get("notes"), effective_strategy),
                "is_active": _project_archive_choose_bool(existing.is_active if existing else True, payload.get("is_active"), effective_strategy),
                "card_json": merged_card_json,
                "first_appearance_chapter_id": chapter_title_lookup.get(payload.get("first_appearance_chapter_title") or "", existing.first_appearance_chapter_id if existing else None),
                "last_appearance_chapter_id": chapter_title_lookup.get(payload.get("last_appearance_chapter_title") or "", existing.last_appearance_chapter_id if existing else None),
            }

            if existing is None:
                character = Character(book_id=book.id, name=payload["name"], **merged_payload)
                db.add(character)
                character_name_map[payload["name"]] = character
                report["characters"]["created"] += 1
            else:
                for field, value in merged_payload.items():
                    setattr(existing, field, value)
                db.add(existing)
                report["characters"]["updated"] += 1
        db.flush()

    character_name_to_id = {
        character.name: character.id
        for character in db.execute(select(Character).where(Character.book_id == book.id)).scalars().all()
        if character.id
    }

    if relations_payload:
        existing_relations = db.execute(select(Relation).where(Relation.book_id == book.id)).scalars().all()
        relation_map = {
            (
                relation.source_character.name if relation.source_character else "",
                relation.target_character.name if relation.target_character else "",
                sanitize_relation_text(relation.relation_type, fallback="关系") or "关系",
            ): relation
            for relation in existing_relations
        }
        for incoming in relations_payload:
            source_name = _project_archive_text(incoming.get("source_character_name"))
            target_name = _project_archive_text(incoming.get("target_character_name"))
            relation_type = sanitize_relation_text(incoming.get("relation_type"), fallback="关系") or "关系"
            source_id = character_name_to_id.get(source_name)
            target_id = character_name_to_id.get(target_name)
            if not source_id or not target_id or source_id == target_id:
                report["warnings"].append(f"跳过关系：{source_name} -> {target_name}，因为人物不存在或不合法。")
                report["relations"]["skipped"] += 1
                continue

            existing = relation_map.get((source_name, target_name, relation_type))
            item_strategy = normalized_decisions.get(_project_archive_relation_conflict_id(source_name, relation_type, target_name))
            if item_strategy == "keep_existing" and existing is not None:
                report["relations"]["skipped"] += 1
                continue
            if item_strategy == "keep_existing" and existing is None:
                report["relations"]["skipped"] += 1
                continue
            effective_strategy = item_strategy or merge_strategy
            merged_payload = {
                "label": sanitize_relation_text(
                    _project_archive_pick_text(existing.label if existing else None, incoming.get("label"), effective_strategy)
                ),
                "description": normalize_relation_description(
                    _project_archive_pick_text(existing.description if existing else None, incoming.get("description"), effective_strategy)
                ),
                "strength": _project_archive_choose_number(existing.strength if existing else None, incoming.get("strength"), effective_strategy),
                "is_bidirectional": _project_archive_choose_bool(existing.is_bidirectional if existing else False, incoming.get("is_bidirectional"), effective_strategy),
                "valid_from_chapter_id": chapter_title_lookup.get(
                    _project_archive_text(incoming.get("valid_from_chapter_title")) or "",
                    existing.valid_from_chapter_id if existing else None,
                ),
                "valid_to_chapter_id": chapter_title_lookup.get(
                    _project_archive_text(incoming.get("valid_to_chapter_title")) or "",
                    existing.valid_to_chapter_id if existing else None,
                ),
            }
            if existing is None:
                relation = Relation(
                    book_id=book.id,
                    source_character_id=source_id,
                    target_character_id=target_id,
                    relation_type=relation_type,
                    extra_data=incoming.get("extra_data") if isinstance(incoming.get("extra_data"), dict) else {},
                    **merged_payload,
                )
                db.add(relation)
                relation_map[(source_name, target_name, relation_type)] = relation
                report["relations"]["created"] += 1
            else:
                existing.source_character_id = source_id
                existing.target_character_id = target_id
                existing.relation_type = relation_type
                for field, value in merged_payload.items():
                    setattr(existing, field, value)
                if isinstance(incoming.get("extra_data"), dict):
                    extra_data = dict(existing.extra_data or {})
                    extra_data.update(incoming.get("extra_data") or {})
                    existing.extra_data = extra_data
                db.add(existing)
                report["relations"]["updated"] += 1

    db.add(book)
    db.commit()
    db.refresh(book)
    return report


def import_book_project_archive(
    *,
    db: Session,
    book: Book,
    current_user: User,
    file_bytes: bytes,
    merge_strategy: str,
) -> dict[str, Any]:
    archive_payload = _project_archive_read_payload(file_bytes)
    return apply_book_project_archive_payload(
        db=db,
        book=book,
        current_user=current_user,
        archive_payload=archive_payload,
        merge_strategy=merge_strategy,
    )


def world_extraction_job_can_resume(job: WorldExtractionJob) -> bool:
    if job.status not in {WorldExtractionJobStatus.FAILED}:
        return False
    if job.source_type == WorldExtractionSource.INTERNAL_BOOK:
        return True
    stored_path = (job.options_json or {}).get("stored_path")
    return bool(stored_path and Path(str(stored_path)).exists())


def world_extraction_job_failed_segment_count(job: WorldExtractionJob) -> int:
    result_payload = job.result_payload or {}
    totals = result_payload.get("totals") or {}
    raw_count = totals.get("failed_segment_count")
    if isinstance(raw_count, int) and raw_count >= 0:
        return raw_count
    return len(result_payload.get("errors") or [])


def world_extraction_job_can_retry_failed(job: WorldExtractionJob) -> bool:
    if job.status not in {WorldExtractionJobStatus.FAILED, WorldExtractionJobStatus.COMPLETED}:
        return False
    if world_extraction_job_failed_segment_count(job) <= 0:
        return False
    if job.source_type == WorldExtractionSource.INTERNAL_BOOK:
        return True
    stored_path = (job.options_json or {}).get("stored_path")
    return bool(stored_path and Path(str(stored_path)).exists())


def cleanup_world_extraction_job_artifacts(db: Session, job: WorldExtractionJob) -> None:
    stored_path = (job.options_json or {}).get("stored_path")
    if not stored_path:
        return
    other_jobs = db.execute(
        select(WorldExtractionJob).where(WorldExtractionJob.id != job.id)
    ).scalars().all()
    if any((item.options_json or {}).get("stored_path") == stored_path for item in other_jobs):
        return
    source_path = Path(str(stored_path))
    if not source_path.exists():
        return
    try:
        source_path.unlink()
        if source_path.parent.exists():
            source_path.parent.rmdir()
    except OSError:
        logger.warning("world_extraction_cleanup_failed path=%s", source_path)


def localize_world_extraction_text(message: Optional[str]) -> Optional[str]:
    if message is None:
        return None

    text = str(message).strip()
    if not text:
        return text

    direct_translations = {
        "Cancelled by user.": "已由用户终止。",
        "Extraction terminated before start.": "提取任务在开始前已终止。",
        "Extraction terminated after service restart.": "服务进程中断或重新启动后，提取任务已标记为终止。",
        "Extraction was interrupted by service restart. You can continue it from the job list.": "提取任务因服务进程中断或重新启动而中断，可在任务列表中继续提取。",
        "Extraction queue was interrupted before start.": "提取队列在开始前因服务进程中断或重新启动而中断。",
        "Extraction stopped unexpectedly during service restart.": "提取任务在服务进程中断或重新启动时意外停止。",
    "Preparing extraction plan.": "正在准备提取计划。",
        "No new or matching content was found for extraction.": "没有找到可提取的新内容或匹配内容。",
        "All written chapters are already up to date.": "已写章节都已完成提取，无需重复扫描。",
        "No matching content was found for extraction.": "没有找到可用于提取的内容。",
        "Extraction terminated by user.": "提取任务已按你的要求终止。",
        "Extraction completed successfully.": "提取任务已完成。",
        "Extraction failed.": "提取任务失败。",
        "Termination requested. Waiting for the current segment to stop.": "已收到终止请求，系统会在当前片段处理完后停止。",
        "Queued to continue the previous extraction.": "已排队继续之前的提取任务。",
        "Queued to retry failed extraction segments.": "已排队重试上次失败片段。",
        "Queued for extraction.": "已进入提取队列。",
        "Uploaded and waiting to start.": "文件已上传，等待开始提取。",
        "Only pending or running extraction jobs can be terminated.": "只有排队中或进行中的提取任务才能终止。",
        "Only failed extraction jobs with available source data can be continued.": "只有失败且仍保留源数据的提取任务才能继续。",
        "Only completed or failed jobs with failed segments can retry those segments.": "只有存在失败片段的已完成或失败任务，才能重试失败片段。",
        "No failed segments are available to retry for this task.": "当前任务没有可重试的失败片段。",
        "The original imported document is no longer available, so this task cannot be continued.": "原始导入文档已不存在，无法继续该提取任务。",
        "Running extraction jobs must be terminated before they can be deleted.": "请先终止正在运行的提取任务，再删除记录。",
    }
    if text in direct_translations:
        return direct_translations[text]

    match = re.match(
        r"^Planned (\d+) extraction segments with ~(\d+) units per segment and (\d+) worker\(s\)\.$",
        text,
    )
    if match:
        return f"已规划 {match.group(1)} 个提取片段，单段约 {match.group(2)} 字，使用 {match.group(3)} 个线程。"

    match = re.match(r"^Skipped failed segment (\d+)/(\d+):\s*(.+)$", text)
    if match:
        return f"已跳过失败片段 {match.group(1)}/{match.group(2)}：{match.group(3)}"

    match = re.match(r"^Processed segment (\d+)/(\d+):\s*(.+)$", text)
    if match:
        return f"已处理片段 {match.group(1)}/{match.group(2)}：{match.group(3)}"

    match = re.match(r"^Completed with (\d+) failed segments\.$", text)
    if match:
        return f"提取已完成，其中 {match.group(1)} 个片段失败。"

    match = re.match(r"^Resolved conflict (.+)\.$", text)
    if match:
        return f"已处理冲突 {match.group(1)}。"

    return text


def serialize_world_extraction_job(job: WorldExtractionJob) -> dict[str, Any]:
    progress_ratio = 0.0
    if job.total_segments:
        progress_ratio = min(job.processed_segments / job.total_segments, 1.0)
    result_payload = job.result_payload or {}
    conflicts = result_payload.get("conflicts") or []
    pending_conflicts_count = sum(1 for item in conflicts if item.get("status") != "resolved")
    resolved_conflicts_count = sum(1 for item in conflicts if item.get("status") == "resolved")
    failed_segment_count = world_extraction_job_failed_segment_count(job)
    effective_strategy = job_conflict_strategy(job)
    options = job.options_json or {}

    return {
        "id": job.id,
        "book_id": job.book_id,
        "created_by_id": job.created_by_id,
        "created_by_username": job.created_by.username if job.created_by else None,
        "source_type": job.source_type.value,
        "source_name": job.source_name,
        "status": job.status.value,
        "conflict_strategy": effective_strategy.value,
        "update_world_bible": job.update_world_bible,
        "chapter_scope": job.chapter_scope,
        "segment_unit_limit": job.segment_unit_limit,
        "total_units": job.total_units,
        "processed_units": job.processed_units,
        "total_segments": job.total_segments,
        "processed_segments": job.processed_segments,
        "progress_ratio": progress_ratio,
        "pending_conflicts_count": pending_conflicts_count,
        "resolved_conflicts_count": resolved_conflicts_count,
        "failed_segment_count": failed_segment_count,
        "cancel_requested": job_cancel_requested(job),
        "is_terminated": job_is_terminated(job),
        "message": localize_world_extraction_text(job.message),
        "error_message": localize_world_extraction_text(job.error_message),
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "options_json": options,
        "termination_requested_at": options.get("termination_requested_at"),
        "terminated_at": options.get("terminated_at"),
        "worker_count": options.get("worker_count"),
        "detected_context_window": options.get("detected_context_window"),
        "skip_unchanged_chapters": bool(options.get("skip_unchanged_chapters", job.source_type == WorldExtractionSource.INTERNAL_BOOK)),
        "planned_chapter_count": options.get("planned_chapter_count"),
        "planned_segment_count": options.get("planned_segment_count"),
        "skipped_empty_chapter_count": options.get("skipped_empty_chapter_count", 0),
        "skipped_unchanged_chapter_count": options.get("skipped_unchanged_chapter_count", 0),
        "resume_available": world_extraction_job_can_resume(job),
        "retry_failed_available": world_extraction_job_can_retry_failed(job),
        "result_payload": result_payload,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def unauthorized(detail: str = "Invalid authentication credentials.") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def is_admin(user: User) -> bool:
    return user.role in {UserRole.SUPER_ADMIN, UserRole.ADMIN}


def ensure_admin(current_user: User) -> None:
    if not is_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges are required.",
        )


def get_model_updates(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(exclude_unset=True)


def get_book_or_404(db: Session, book_id: int) -> Book:
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Book not found.")
    return book


def get_chapter_or_404(db: Session, book_id: int, chapter_id: int) -> Chapter:
    chapter = db.get(Chapter, chapter_id)
    if chapter is None or chapter.book_id != book_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chapter not found.")
    return chapter


def get_snapshot_or_404(db: Session, book_id: int, snapshot_id: int) -> Snapshot:
    snapshot = db.get(Snapshot, snapshot_id)
    if snapshot is None or snapshot.book_id != book_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Snapshot not found.")
    return snapshot


def get_ai_config_or_404(db: Session, config_id: int) -> AIConfig:
    config = db.get(AIConfig, config_id)
    if config is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI config not found.")
    return config


def get_user_or_404(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    return user


def get_character_or_404(db: Session, book_id: int, character_id: int) -> Character:
    character = db.get(Character, character_id)
    if character is None or character.book_id != book_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Character not found.")
    return character


def get_relation_or_404(db: Session, book_id: int, relation_id: int) -> Relation:
    relation = db.get(Relation, relation_id)
    if relation is None or relation.book_id != book_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Relation not found.")
    return relation


def get_faction_or_404(db: Session, book_id: int, faction_id: int) -> Faction:
    faction = db.get(Faction, faction_id)
    if faction is None or faction.book_id != book_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Faction not found.")
    return faction


def get_faction_membership_or_404(db: Session, book_id: int, membership_id: int) -> FactionMembership:
    membership = db.get(FactionMembership, membership_id)
    if membership is None or membership.book_id != book_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Faction membership not found.")
    return membership


def get_world_extraction_job_or_404(db: Session, book_id: int, job_id: int) -> WorldExtractionJob:
    job = db.get(WorldExtractionJob, job_id)
    if job is None or job.book_id != book_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="World extraction job not found.")
    return job


def save_world_import_upload(job: WorldExtractionJob, upload: UploadFile) -> None:
    filename = upload.filename or "source"
    try:
        validate_world_import_source(filename)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    destination = build_upload_storage_path(job.id, filename)
    total_bytes = 0
    try:
        with destination.open("wb") as handle:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > settings.max_world_import_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"导入文件过大，当前上限为 {settings.max_world_import_bytes // (1024 * 1024)} MB。",
                    )
                handle.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise

    options = dict(job.options_json or {})
    options.update(
        {
            "stored_path": str(destination),
            "original_filename": filename,
            "content_type": upload.content_type,
            "file_size_bytes": total_bytes,
        }
    )
    job.options_json = options
    job.source_name = filename


def save_temporary_world_import_upload(upload: UploadFile) -> tuple[Path, int, str]:
    filename = upload.filename or "source"
    try:
        extension = validate_world_import_source(filename)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    total_bytes = 0
    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="world-import-estimate-",
            suffix=extension,
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > settings.max_world_import_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"导入文件过大，当前上限为 {settings.max_world_import_bytes // (1024 * 1024)} MB。",
                    )
                handle.write(chunk)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise

    return temp_path, total_bytes, filename


def ensure_book_http_access(book: Book, current_user: User) -> None:
    try:
        ensure_book_access(book, current_user)
    except AccessDeniedError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))


def resolve_book_owner_id(db: Session, requested_owner_id: Optional[int], current_user: User) -> int:
    if requested_owner_id is None:
        return current_user.id

    if not is_admin(current_user) and requested_owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only administrators can assign books to other users.",
        )

    owner = db.get(User, requested_owner_id)
    if owner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owner user not found.")
    return owner.id


def ensure_ai_env_var_permission(current_user: User, values: dict[str, Any]) -> None:
    if is_admin(current_user):
        return
    env_fields = ("base_url_env_var", "api_key_env_var", "model_name_env_var")
    if any(field in values for field in env_fields):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only administrators can configure runtime environment variable bindings for AI settings.",
        )


def validate_ai_base_url_for_user(
    current_user: User,
    base_url: Optional[str],
    *,
    resolve_dns: bool,
) -> Optional[str]:
    value = str(base_url or "").strip()
    if not value:
        return None
    try:
        return validate_outbound_base_url(
            value,
            allow_private_network=is_admin(current_user),
            resolve_dns=resolve_dns,
        )
    except UnsafeOutboundURLError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc


def ensure_world_extraction_capacity(db: Session, current_user: User, book_id: int) -> None:
    running_statuses = (WorldExtractionJobStatus.PENDING, WorldExtractionJobStatus.RUNNING)
    global_running = db.execute(
        select(func.count(WorldExtractionJob.id)).where(WorldExtractionJob.status.in_(running_statuses))
    ).scalar_one()
    if global_running >= settings.max_running_world_jobs_global:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="当前后台提取任务已达全局上限，请稍后再试。",
        )

    user_running = db.execute(
        select(func.count(WorldExtractionJob.id)).where(
            WorldExtractionJob.created_by_id == current_user.id,
            WorldExtractionJob.status.in_(running_statuses),
        )
    ).scalar_one()
    if user_running >= settings.max_running_world_jobs_per_user:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="你当前已有过多后台提取任务正在运行，请等待现有任务完成后再试。",
        )

    book_running = db.execute(
        select(func.count(WorldExtractionJob.id)).where(
            WorldExtractionJob.book_id == book_id,
            WorldExtractionJob.status.in_(running_statuses),
        )
    ).scalar_one()
    if book_running >= settings.max_running_world_jobs_per_book:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="当前书籍已有后台提取任务在运行，请先等待其完成。",
        )


@contextmanager
def sqlite_foreign_keys_disabled(connection: Any):
    if connection.dialect.name != "sqlite":
        yield
        return

    raw_connection = getattr(connection, "connection", None)
    driver_connection = getattr(raw_connection, "driver_connection", raw_connection)

    if driver_connection is None:
        yield
        return

    def _set_foreign_keys(enabled: bool) -> None:
        previous_isolation_level = getattr(driver_connection, "isolation_level", None)
        cursor = driver_connection.cursor()
        try:
            if hasattr(driver_connection, "isolation_level"):
                driver_connection.isolation_level = None
            cursor.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")
        finally:
            cursor.close()
            if hasattr(driver_connection, "isolation_level"):
                driver_connection.isolation_level = previous_isolation_level

    cursor = driver_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys")
        row = cursor.fetchone()
    finally:
        cursor.close()

    original_state = bool(row[0]) if row else False
    _set_foreign_keys(False)
    try:
        yield
    finally:
        _set_foreign_keys(original_state)


def migrate_ai_config_api_keys(db: Session) -> int:
    configs = db.execute(select(AIConfig).where(AIConfig.api_key.is_not(None))).scalars().all()
    migrated = 0
    for config in configs:
        raw_value = str(config.api_key or "").strip()
        if not raw_value or is_encrypted_secret(raw_value):
            continue
        config.api_key = encrypt_secret(raw_value)
        db.add(config)
        migrated += 1
    if migrated:
        db.commit()
    return migrated


def migrate_ai_config_module_schema(db: Session) -> bool:
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "sqlite":
        return False

    table_sql = db.execute(
        text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'ai_configs'")
    ).scalar_one_or_none()
    if not table_sql:
        return False

    table_sql = str(table_sql)
    if "'assistant'" in table_sql:
        return False

    old_module_constraint = (
        "('co_writing', 'outline_expansion', 'summary', 'setting_extraction', "
        "'character_extraction', 'relation_extraction', 'reasoner')"
    )
    new_module_constraint = (
        "('co_writing', 'outline_expansion', 'summary', 'setting_extraction', "
        "'character_extraction', 'relation_extraction', 'reasoner', 'assistant')"
    )
    if old_module_constraint not in table_sql:
        raise RuntimeError("Current ai_configs schema does not match expected AIModule constraint.")

    rebuilt_table_sql = table_sql.replace(old_module_constraint, new_module_constraint)
    index_sql_list = [
        str(value)
        for value in db.execute(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = 'ai_configs' AND sql IS NOT NULL "
                "ORDER BY name"
            )
        ).scalars().all()
        if value
    ]
    column_names = [str(column.get("name")) for column in inspect(bind).get_columns("ai_configs") if column.get("name")]
    if not column_names:
        raise RuntimeError("Unable to determine ai_configs columns for schema migration.")

    quoted_columns = ", ".join(f'"{name}"' for name in column_names)
    connection = db.connection()
    with sqlite_foreign_keys_disabled(connection):
        try:
            connection.exec_driver_sql("ALTER TABLE ai_configs RENAME TO ai_configs__old")
            connection.exec_driver_sql(rebuilt_table_sql)
            connection.exec_driver_sql(
                f'INSERT INTO ai_configs ({quoted_columns}) SELECT {quoted_columns} FROM ai_configs__old'
            )
            for index_sql in index_sql_list:
                connection.exec_driver_sql(index_sql)
            connection.exec_driver_sql("DROP TABLE ai_configs__old")
            db.commit()
            return True
        except Exception:
            db.rollback()
            raise


def _sqlite_rebuild_table_with_sql(db: Session, *, table_name: str, rebuilt_table_sql: str) -> None:
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "sqlite":
        raise RuntimeError("SQLite table rebuild helper can only run on SQLite.")

    index_sql_list = [
        str(value)
        for value in db.execute(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = :table_name AND sql IS NOT NULL "
                "ORDER BY name"
            ),
            {"table_name": table_name},
        ).scalars().all()
        if value
    ]
    column_names = [str(column.get("name")) for column in inspect(bind).get_columns(table_name) if column.get("name")]
    if not column_names:
        raise RuntimeError(f"Unable to determine {table_name} columns for SQLite rebuild.")

    quoted_columns = ", ".join(f'"{name}"' for name in column_names)
    temp_table_name = f"{table_name}__rebuild_old"
    connection = db.connection()
    with sqlite_foreign_keys_disabled(connection):
        try:
            connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{temp_table_name}"')
            connection.exec_driver_sql(f'ALTER TABLE "{table_name}" RENAME TO "{temp_table_name}"')
            connection.exec_driver_sql(rebuilt_table_sql)
            connection.exec_driver_sql(
                f'INSERT INTO "{table_name}" ({quoted_columns}) SELECT {quoted_columns} FROM "{temp_table_name}"'
            )
            connection.exec_driver_sql(f'DROP TABLE "{temp_table_name}"')
            for index_sql in index_sql_list:
                connection.exec_driver_sql(index_sql)
            db.commit()
        except Exception:
            db.rollback()
            raise


def repair_ai_config_legacy_sqlite_references(db: Session) -> dict[str, int]:
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "sqlite":
        return {"rewired_tables": 0, "dropped_legacy_tables": 0}

    legacy_ai_config_sql = db.execute(
        text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'ai_configs__old'")
    ).scalar_one_or_none()
    legacy_reference_rows = db.execute(
        text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'table' AND name != 'ai_configs__old' AND sql LIKE '%ai_configs__old%' "
            "ORDER BY name"
        )
    ).all()

    rewired_tables = 0
    for table_name, table_sql in legacy_reference_rows:
        rebuilt_table_sql = str(table_sql).replace('"ai_configs__old"', '"ai_configs"').replace(" ai_configs__old", " ai_configs")
        if rebuilt_table_sql == str(table_sql):
            continue
        _sqlite_rebuild_table_with_sql(db, table_name=str(table_name), rebuilt_table_sql=rebuilt_table_sql)
        rewired_tables += 1

    dropped_legacy_tables = 0
    if legacy_ai_config_sql:
        remaining_legacy_refs = db.execute(
            text(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type = 'table' AND name != 'ai_configs__old' AND sql LIKE '%ai_configs__old%'"
            )
        ).scalar_one()
        if not remaining_legacy_refs:
            connection = db.connection()
            with sqlite_foreign_keys_disabled(connection):
                try:
                    connection.exec_driver_sql('DROP TABLE IF EXISTS "ai_configs__old"')
                    db.commit()
                    dropped_legacy_tables = 1
                except Exception:
                    db.rollback()
                    raise

    return {
        "rewired_tables": rewired_tables,
        "dropped_legacy_tables": dropped_legacy_tables,
    }


def _table_columns(db: Session, table_name: str) -> set[str]:
    bind = db.get_bind()
    if bind is None:
        return set()
    inspector = inspect(bind)
    return {
        str(column.get("name"))
        for column in inspector.get_columns(table_name)
        if column.get("name")
    }


def _index_names(db: Session, table_name: str) -> set[str]:
    bind = db.get_bind()
    if bind is None:
        return set()
    inspector = inspect(bind)
    names = {
        str(item.get("name"))
        for item in inspector.get_indexes(table_name)
        if item.get("name")
    }
    names.update(
        str(item.get("name"))
        for item in inspector.get_unique_constraints(table_name)
        if item.get("name")
    )
    return names


def _add_column_if_missing(db: Session, table_name: str, column_name: str, column_sql: str) -> bool:
    if column_name in _table_columns(db, table_name):
        return False
    db.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}"))
    return True


def _relation_importance_rank(value: Any) -> int:
    normalized = normalize_relation_importance(value)
    return {
        "background": 0,
        "minor": 1,
        "major": 2,
        "core": 3,
    }.get(normalized, 2)


def _merge_relation_extra(existing: Optional[dict[str, Any]], incoming: Optional[dict[str, Any]]) -> dict[str, Any]:
    merged = dict(existing or {})
    if isinstance(incoming, dict):
        merged.update(incoming)
    return merged


def migrate_world_schema(db: Session) -> dict[str, int]:
    migrated_columns = 0
    normalized_relations = 0
    merged_relations = 0
    created_relation_events = 0

    migrated_columns += int(
        _add_column_if_missing(
            db,
            "relations",
            "importance_level",
            "VARCHAR(32) DEFAULT 'major'",
        )
    )
    db.execute(text("UPDATE relations SET importance_level = 'major' WHERE importance_level IS NULL"))
    db.commit()

    relations = db.execute(select(Relation).order_by(Relation.id.asc())).scalars().all()
    normalized_relation_map: dict[tuple[int, int, int, str], Relation] = {}
    for relation in relations:
        legacy_type = str(relation.relation_type or "").strip()
        normalized_type = normalize_relation_type(legacy_type, fallback="other")
        normalized_label = sanitize_relation_text(relation.label) or sanitize_relation_text(legacy_type) or relation_type_label(normalized_type)
        normalized_importance = normalize_relation_importance(getattr(relation, "importance_level", None))
        extra_data = _merge_relation_extra(relation.extra_data, None)
        if legacy_type and legacy_type != normalized_type:
            extra_data.setdefault("legacy_relation_type", legacy_type)

        key = (
            relation.book_id,
            relation.source_character_id,
            relation.target_character_id,
            normalized_type,
        )
        existing = normalized_relation_map.get(key)
        if existing is not None and existing.id != relation.id:
            merged_extra = _merge_relation_extra(existing.extra_data, extra_data)
            merged_legacy = list(merged_extra.get("merged_legacy_relation_types") or [])
            for candidate in filter(None, {legacy_type, merged_extra.get("legacy_relation_type")}):
                if candidate != normalized_type and candidate not in merged_legacy:
                    merged_legacy.append(candidate)
            if merged_legacy:
                merged_extra["merged_legacy_relation_types"] = merged_legacy
            existing.extra_data = merged_extra
            if not existing.label and normalized_label:
                existing.label = normalized_label
            if not existing.description and relation.description:
                existing.description = relation.description
            if relation.strength is not None and (existing.strength is None or relation.strength > existing.strength):
                existing.strength = relation.strength
            if relation.valid_from_chapter_id is not None:
                if existing.valid_from_chapter_id is None or relation.valid_from_chapter_id < existing.valid_from_chapter_id:
                    existing.valid_from_chapter_id = relation.valid_from_chapter_id
            if relation.valid_to_chapter_id is not None:
                if existing.valid_to_chapter_id is None or relation.valid_to_chapter_id > existing.valid_to_chapter_id:
                    existing.valid_to_chapter_id = relation.valid_to_chapter_id
            if _relation_importance_rank(normalized_importance) > _relation_importance_rank(existing.importance_level):
                existing.importance_level = normalized_importance
            existing.is_bidirectional = existing.is_bidirectional or relation.is_bidirectional
            db.add(existing)
            db.delete(relation)
            merged_relations += 1
            continue

        relation.relation_type = normalized_type
        relation.label = normalized_label
        relation.importance_level = normalized_importance
        relation.extra_data = extra_data
        normalized_relation_map[key] = relation
        db.add(relation)
        normalized_relations += 1

    db.commit()

    if "ix_relations_book_source_target_type_unique" not in _index_names(db, "relations"):
        db.execute(
            text(
                "CREATE UNIQUE INDEX ix_relations_book_source_target_type_unique "
                "ON relations (book_id, source_character_id, target_character_id, relation_type)"
            )
        )
        db.commit()

    relations = db.execute(select(Relation).order_by(Relation.id.asc())).scalars().all()
    for relation in relations:
        has_events = db.execute(
            select(func.count(RelationEvent.id)).where(RelationEvent.relation_id == relation.id)
        ).scalar_one()
        if has_events:
            continue
        event = RelationEvent(
            relation_id=relation.id,
            book_id=relation.book_id,
            source_character_id=relation.source_character_id,
            target_character_id=relation.target_character_id,
            chapter_id=relation.valid_from_chapter_id,
            segment_label=None,
            relation_type=normalize_relation_type(relation.relation_type),
            label=sanitize_relation_text(relation.label),
            description=normalize_relation_description(relation.description),
            strength=relation.strength,
            importance_level=normalize_relation_importance(relation.importance_level),
            is_bidirectional=relation.is_bidirectional,
            event_summary=normalize_relation_description(
                (relation.extra_data or {}).get("latest_event_summary") or relation.description
            ),
        )
        db.add(event)
        created_relation_events += 1
    if created_relation_events:
        db.commit()

    return {
        "migrated_columns": migrated_columns,
        "normalized_relations": normalized_relations,
        "merged_relations": merged_relations,
        "created_relation_events": created_relation_events,
    }


def next_chapter_sort_order(db: Session, book_id: int, parent_id: Optional[int]) -> int:
    max_order = db.execute(
        select(func.max(Chapter.sort_order)).where(
            Chapter.book_id == book_id,
            Chapter.parent_id == parent_id,
        )
    ).scalar_one()
    return int(max_order or 0) + 1


def validate_parent_assignment(
    db: Session,
    *,
    book_id: int,
    parent_id: Optional[int],
    chapter_id: Optional[int] = None,
) -> Optional[Chapter]:
    if parent_id is None:
        return None

    parent = db.get(Chapter, parent_id)
    if parent is None or parent.book_id != book_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Parent chapter is invalid.")

    if chapter_id is None:
        return parent

    if parent_id == chapter_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Chapter cannot be its own parent.")

    cursor = parent
    while cursor.parent_id is not None:
        if cursor.parent_id == chapter_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot move a chapter under its descendant.",
            )
        cursor = db.get(Chapter, cursor.parent_id)
        if cursor is None:
            break

    return parent


def validate_book_chapter_reference(
    db: Session,
    book_id: int,
    chapter_id: Optional[int],
) -> Optional[int]:
    if chapter_id is None:
        return None
    chapter = get_chapter_or_404(db, book_id, chapter_id)
    return chapter.id


def validate_relation_endpoints(
    db: Session,
    book_id: int,
    source_character_id: int,
    target_character_id: int,
) -> tuple[int, int]:
    source = get_character_or_404(db, book_id, source_character_id)
    target = get_character_or_404(db, book_id, target_character_id)
    if source.id == target.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Source and target characters must be different.",
        )
    return source.id, target.id


def find_relation_by_signature(
    db: Session,
    *,
    book_id: int,
    source_character_id: int,
    target_character_id: int,
    relation_type: str,
    exclude_relation_id: Optional[int] = None,
) -> Optional[Relation]:
    query = select(Relation).where(
        Relation.book_id == book_id,
        Relation.source_character_id == source_character_id,
        Relation.target_character_id == target_character_id,
        Relation.relation_type == normalize_relation_type(relation_type),
    )
    if exclude_relation_id is not None:
        query = query.where(Relation.id != exclude_relation_id)
    return db.execute(query.order_by(Relation.id.asc())).scalars().first()


def validate_faction_membership_refs(
    db: Session,
    *,
    book_id: int,
    faction_id: int,
    character_id: int,
) -> tuple[int, int]:
    faction = get_faction_or_404(db, book_id, faction_id)
    character = get_character_or_404(db, book_id, character_id)
    return faction.id, character.id


def refresh_book_aggregates(db: Session, book: Book) -> None:
    """Recalculate aggregates inside the caller's transaction and flush staged values."""
    chapters = db.execute(
        select(Chapter).where(Chapter.book_id == book.id)
    ).scalars().all()
    book.chapter_count = sum(
        1 for chapter in chapters if chapter.node_type in {ChapterNodeType.CHAPTER, ChapterNodeType.SCENE}
    )
    book.word_count = sum(estimate_text_units(chapter.content or "") for chapter in chapters)
    db.add(book)
    db.flush()


def rebuild_chapter_tree(db: Session, chapter: Chapter) -> None:
    parent = db.get(Chapter, chapter.parent_id) if chapter.parent_id is not None else None
    chapter.depth = (parent.depth + 1) if parent else 0
    db.add(chapter)
    db.flush()

    prefix = parent.tree_path if parent else ""
    chapter.tree_path = f"{prefix}/{chapter.id:06d}" if prefix else f"{chapter.id:06d}"
    db.add(chapter)
    db.flush()

    children = db.execute(
        select(Chapter)
        .where(Chapter.parent_id == chapter.id)
        .order_by(Chapter.sort_order.asc(), Chapter.id.asc())
    ).scalars().all()
    for child in children:
        rebuild_chapter_tree(db, child)


def coerce_ai_scope_targets(
    db: Session,
    *,
    scope: AIScope,
    user_id: Optional[int],
    book_id: Optional[int],
    current_user: User,
) -> tuple[Optional[int], Optional[int]]:
    if scope == AIScope.SYSTEM:
        if not is_admin(current_user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only administrators can manage system AI configs.",
            )
        return None, None

    if scope == AIScope.USER:
        resolved_user_id = user_id or current_user.id
        if not is_admin(current_user) and resolved_user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only manage your own user-scoped AI configs.",
            )

        target_user = db.get(User, resolved_user_id)
        if target_user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target user not found.")
        return target_user.id, None

    if book_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Book-scoped AI configs require a book_id.",
        )

    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    return None, book.id


def unset_competing_defaults(db: Session, config: AIConfig) -> None:
    if not config.is_default:
        return

    stmt = select(AIConfig).where(
        AIConfig.id != config.id,
        AIConfig.scope == config.scope,
        AIConfig.module == config.module,
    )
    if config.scope == AIScope.USER:
        stmt = stmt.where(AIConfig.user_id == config.user_id)
    elif config.scope == AIScope.BOOK:
        stmt = stmt.where(AIConfig.book_id == config.book_id)

    for other in db.execute(stmt).scalars().all():
        other.is_default = False
        db.add(other)


def ensure_ai_config_access(config: AIConfig, current_user: User) -> None:
    if is_admin(current_user):
        return
    if config.scope == AIScope.SYSTEM:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only administrators can manage system AI configs.",
        )
    if config.scope == AIScope.USER and config.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied.")
    if config.scope == AIScope.BOOK:
        if config.book is None or config.book.owner_id != current_user.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied.")


def resolve_ai_connection_inputs(
    db: Session,
    payload: AIModelDiscoveryRequest,
    current_user: User,
) -> tuple[Optional[AIConfig], str, str, Optional[str], int]:
    config: Optional[AIConfig] = None
    if payload.config_id is not None:
        config = get_ai_config_or_404(db, payload.config_id)
        ensure_ai_config_access(config, current_user)

    api_format = (payload.api_format or (config.api_format if config else "openai_v1")).strip()
    if api_format != "openai_v1":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only OpenAI-compatible model discovery is currently supported.",
        )

    module = config.module if config else None
    allow_runtime_env = is_admin(current_user)
    resolved_base_url = (
        _resolve_runtime_env(
            config.base_url_env_var if config else None,
            _env_name_for_ai_module(module, "BASE_URL"),
            "OPENAI_COMPAT_BASE_URL",
            "OPENAI_BASE_URL",
        )
        if allow_runtime_env
        else None
    ) or (config.base_url if config else "")
    try:
        decrypted_api_key = decrypt_secret(config.api_key) if config else ""
    except SecretStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="已保存的 AI API Key 无法解密，请联系管理员重新配置。",
        ) from exc
    resolved_api_key = (
        _resolve_runtime_env(
            config.api_key_env_var if config else None,
            _env_name_for_ai_module(module, "API_KEY"),
            "OPENAI_COMPAT_API_KEY",
            "OPENAI_API_KEY",
        )
        if allow_runtime_env
        else None
    ) or decrypted_api_key

    base_url = validate_ai_base_url_for_user(
        current_user,
        payload.base_url or resolved_base_url,
        resolve_dns=not is_admin(current_user),
    ) or ""
    api_key = (payload.api_key or resolved_api_key or "").strip() or None
    timeout_seconds = max(5, min(int(payload.timeout_seconds or 30), 180))

    if not base_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please provide a base URL before loading models.",
        )

    return config, api_format, base_url, api_key, timeout_seconds


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthorized()

    try:
        payload = decode_access_token(credentials.credentials)
        subject = payload.get("sub")
        user_id = int(subject)
    except (TokenError, TypeError, ValueError):
        raise unauthorized()

    user = db.get(User, user_id)
    if user is None:
        raise unauthorized()

    if user.status != UserStatus.ACTIVE or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is disabled or locked.",
        )

    return user


def _raise_ai_http_error(exc: Exception) -> None:
    if isinstance(exc, ResourceNotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, AccessDeniedError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, AIConfigNotFoundError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if isinstance(exc, AIInvocationError):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    raise exc


def _assistant_normalize_message(text: str) -> str:
    return "".join(str(text or "").strip().lower().split())


def _assistant_recent_chapters(
    db: Session,
    *,
    book_id: int,
    limit: int = 5,
) -> list[Chapter]:
    chapters = db.execute(
        select(Chapter).where(Chapter.book_id == book_id)
    ).scalars().all()
    usable = [
        chapter
        for chapter in chapters
        if chapter.node_type in {ChapterNodeType.CHAPTER, ChapterNodeType.SCENE}
        and any(
            isinstance(value, str) and value.strip()
            for value in (chapter.content, chapter.summary, chapter.outline)
        )
    ]
    usable.sort(
        key=lambda item: (
            item.sequence_number if item.sequence_number is not None else (item.sort_order or 0),
            item.sort_order or 0,
            item.id,
        )
    )
    return usable[-limit:]


def _assistant_trim_text(value: Optional[str], limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}..."


def _assistant_find_latest_pending_conflict_job(
    db: Session,
    *,
    book_id: int,
) -> tuple[Optional[WorldExtractionJob], list[dict[str, Any]]]:
    jobs = db.execute(
        select(WorldExtractionJob)
        .where(WorldExtractionJob.book_id == book_id)
        .order_by(WorldExtractionJob.created_at.desc(), WorldExtractionJob.id.desc())
    ).scalars().all()
    for job in jobs:
        conflicts = [
            item
            for item in list((job.result_payload or {}).get("conflicts") or [])
            if item.get("status") != "resolved"
        ]
        if conflicts:
            return job, conflicts
    return None, []


def _assistant_match_conflict_resolution(
    message: str,
) -> Optional[tuple[WorldConflictStrategy, str]]:
    normalized = _assistant_normalize_message(message)
    if not normalized:
        return None

    keep_existing_markers = [
        "保留软件内",
        "保留现有",
        "保留当前人物卡",
        "保留软件里",
        "以软件内为准",
        "以现有为准",
    ]
    prefer_imported_markers = [
        "保留原著",
        "保留导入",
        "采用原著",
        "采用导入",
        "以原著为准",
        "以导入为准",
    ]

    if any(marker in normalized for marker in keep_existing_markers):
        return WorldConflictStrategy.KEEP_EXISTING, "保留软件内人物卡和关系"
    if any(marker in normalized for marker in prefer_imported_markers):
        return WorldConflictStrategy.PREFER_IMPORTED, "采用导入原著的人物卡和关系"
    return None


def _assistant_is_trend_request(message: str) -> bool:
    normalized = _assistant_normalize_message(message)
    markers = [
        "剧情趋势",
        "最近几章",
        "写得怎么样",
        "节奏",
        "点评",
        "分析",
        "走向",
        "伏笔",
    ]
    return any(marker in normalized for marker in markers)


def _assistant_is_edit_request(message: str) -> bool:
    normalized = _assistant_normalize_message(message)
    markers = [
        "改写",
        "重写",
        "润色",
        "修改",
        "重构",
        "优化这段",
        "优化正文",
        "改正文",
        "改大纲",
        "改摘要",
        "直接写",
        "帮我写",
    ]
    return any(marker in normalized for marker in markers)


def _assistant_extract_edit_proposal(text: str) -> tuple[str, Optional[dict[str, Any]]]:
    raw = str(text or "").strip()
    if not raw:
        return "", None

    match = re.search(r"<assistant_edit>\s*(\{.*?\})\s*</assistant_edit>\s*$", raw, flags=re.DOTALL)
    if not match:
        return raw, None

    proposal = _extract_json_block(match.group(1))
    if not isinstance(proposal, dict):
        return re.sub(r"<assistant_edit>.*?</assistant_edit>\s*$", "", raw, flags=re.DOTALL).strip(), None

    target_field = str(proposal.get("target_field") or "").strip()
    content = str(proposal.get("content") or "").strip()
    if target_field not in {"content", "outline", "summary"} or not content:
        return re.sub(r"<assistant_edit>.*?</assistant_edit>\s*$", "", raw, flags=re.DOTALL).strip(), None

    cleaned_text = re.sub(r"<assistant_edit>.*?</assistant_edit>\s*$", "", raw, flags=re.DOTALL).strip()
    return cleaned_text, {
        "target_field": target_field,
        "content": content,
        "title": str(proposal.get("title") or "").strip() or "待确认改写",
    }


def _assistant_finalize_reply_text(reply_text: str, edit_proposal: Optional[dict[str, Any]]) -> str:
    cleaned = str(reply_text or "").strip()
    if cleaned:
        return cleaned
    if not isinstance(edit_proposal, dict):
        return ""

    target_field = str(edit_proposal.get("target_field") or "").strip()
    target_label = "正文" if target_field == "content" else "章节大纲" if target_field == "outline" else "章节摘要" if target_field == "summary" else "内容"
    content = str(edit_proposal.get("content") or "").strip()
    if not content:
        return ""
    return f"已生成{target_label}修改建议，请确认是否接受修改。"


def _assistant_selected_characters(
    db: Session,
    *,
    book_id: int,
    selected_ids: list[int],
) -> list[Character]:
    normalized_ids = [int(item) for item in selected_ids if int(item) > 0]
    if not normalized_ids:
        return []
    characters = db.execute(
        select(Character)
        .where(Character.book_id == book_id, Character.id.in_(normalized_ids))
        .order_by(Character.name.asc(), Character.id.asc())
    ).scalars().all()
    return characters


def _assistant_selected_chapters(
    db: Session,
    *,
    book_id: int,
    selected_ids: list[int],
) -> list[Chapter]:
    normalized_ids = [int(item) for item in selected_ids if int(item) > 0]
    if not normalized_ids:
        return []
    chapters = db.execute(
        select(Chapter)
        .where(Chapter.book_id == book_id, Chapter.id.in_(normalized_ids))
        .order_by(Chapter.sort_order.asc(), Chapter.id.asc())
    ).scalars().all()
    return chapters


def _assistant_character_section(character: Character) -> str:
    card_json = merge_character_card_json(character.card_json)
    parts = [
        f"人物：{character.name or '未命名'}",
        f"别名：{'、'.join(character.aliases or []) or '暂无'}",
        f"简介：{_assistant_trim_text(character.biography or character.description, 600) or '暂无'}",
        f"目标：{_assistant_trim_text(character.goals or card_json.get('long_term_goal') or card_json.get('short_term_goal'), 320) or '暂无'}",
        f"秘密：{_assistant_trim_text(character.secrets, 240) or '暂无'}",
        f"备注：{_assistant_trim_text(character.notes, 220) or '暂无'}",
    ]
    return "\n".join(parts)


def _assistant_chapter_section(chapter: Chapter) -> str:
    excerpt_source = chapter.summary or chapter.outline or chapter.content or ""
    return "\n".join(
        [
            f"章节：{chapter.title}",
            f"摘要：{_assistant_trim_text(chapter.summary, 500) or '暂无'}",
            f"大纲：{_assistant_trim_text(chapter.outline, 700) or '暂无'}",
            f"正文摘录：{_assistant_trim_text(excerpt_source, 1800) or '暂无'}",
        ]
    )


def _assistant_prepare_chat_request(
    db: Session,
    *,
    current_user: User,
    book: Book,
    chapter: Optional[Chapter],
    page: Optional[str],
    message: str,
    history: list[AssistantHistoryMessage],
    mode: Literal["general", "trend"],
    selected_character_ids: list[int],
    selected_chapter_ids: list[int],
    custom_prompt: Optional[str],
    current_chapter_title: Optional[str],
    current_chapter_summary: Optional[str],
    current_chapter_outline: Optional[str],
    current_chapter_content: Optional[str],
) -> tuple[Any, list[dict[str, str]], int]:
    config = resolve_ai_config_with_fallback(
        db,
        [AIModule.ASSISTANT, AIModule.SUMMARY, AIModule.CO_WRITING, AIModule.REASONER],
        current_user,
        book,
    )
    recent_chapters = _assistant_recent_chapters(db, book_id=book.id, limit=5)
    characters = db.execute(
        select(Character)
        .where(Character.book_id == book.id, Character.is_active.is_(True))
        .order_by(Character.updated_at.desc(), Character.id.desc())
    ).scalars().all()[:8]
    latest_job, pending_conflicts = _assistant_find_latest_pending_conflict_job(db, book_id=book.id)
    selected_characters = _assistant_selected_characters(
        db,
        book_id=book.id,
        selected_ids=selected_character_ids,
    )
    selected_chapters = _assistant_selected_chapters(
        db,
        book_id=book.id,
        selected_ids=selected_chapter_ids,
    )
    selected_character_names = [item.name for item in selected_characters if item.name]
    selected_chapter_titles = [item.title for item in selected_chapters if item.title]
    prompt_scope_lines = []
    if selected_character_names:
        prompt_scope_lines.append(f"指定人物卡：{'、'.join(selected_character_names)}")
    if selected_chapter_titles:
        prompt_scope_lines.append(f"指定章节：{'、'.join(selected_chapter_titles)}")
    custom_prompt_text = str(custom_prompt or "").strip()
    if custom_prompt_text:
        prompt_scope_lines.append(f"自定义提示词：{custom_prompt_text}")
    selected_character_sections = (
        "已选人物卡：\n" + "\n\n".join(_assistant_character_section(item) for item in selected_characters)
        if selected_characters
        else ""
    )
    selected_chapter_sections = (
        "已选章节：\n" + "\n\n".join(_assistant_chapter_section(item) for item in selected_chapters)
        if selected_chapters
        else ""
    )

    recent_sections = []
    for item in recent_chapters:
        excerpt_source = item.summary or item.outline or item.content or ""
        recent_sections.append(
            "\n".join(
                [
                    f"章节：{item.title}",
                    f"摘要：{_assistant_trim_text(item.summary, 500) or '暂无'}",
                    f"大纲：{_assistant_trim_text(item.outline, 500) or '暂无'}",
                    f"正文摘录：{_assistant_trim_text(excerpt_source, 1400) or '暂无'}",
                ]
            )
        )

    current_chapter_section = ""
    if chapter is not None:
        draft_title = str(current_chapter_title or "").strip() or chapter.title
        draft_summary = str(current_chapter_summary or "").strip() if current_chapter_summary is not None else (chapter.summary or "")
        draft_outline = str(current_chapter_outline or "").strip() if current_chapter_outline is not None else (chapter.outline or "")
        draft_content = str(current_chapter_content or "").strip() if current_chapter_content is not None else (chapter.content or "")
        current_chapter_section = "\n".join(
            [
                f"当前章节：{draft_title}",
                f"当前章节摘要：{_assistant_trim_text(draft_summary, 700) or '暂无'}",
                f"当前章节大纲：{_assistant_trim_text(draft_outline, 900) or '暂无'}",
                f"当前章节正文摘录：{_assistant_trim_text(draft_content, 2200) or '暂无'}",
            ]
        )

    book_context = "\n\n".join(
        section
        for section in [
            f"书名：{book.title}",
            f"类型：{book.genre or '未填写'}",
            f"页面：{page or 'unknown'}",
            f"全局文风：{_assistant_trim_text(book.global_style_prompt, 900) or '暂无'}",
            f"长期摘要：{_assistant_trim_text(book.long_term_summary, 1400) or '暂无'}",
            f"世界观手册：{_assistant_trim_text(book.world_bible, 1800) or '暂无'}",
            (
                "主要人物："
                + ("、".join(item.name for item in characters if item.name) if characters else "暂无")
            ),
            (
                f"待处理冲突：任务 #{latest_job.id} 还有 {len(pending_conflicts)} 条"
                if latest_job and pending_conflicts
                else "待处理冲突：暂无"
            ),
            "本轮提示词范围：\n" + "\n".join(prompt_scope_lines) if prompt_scope_lines else "",
            selected_character_sections,
            selected_chapter_sections,
            current_chapter_section,
            "最近章节：\n" + "\n\n".join(recent_sections) if recent_sections else "最近章节：暂无",
        ]
        if section
    )

    if mode == "trend":
        task_instruction = (
            "用户想看最近几章剧情趋势。请用简洁中文回答，并严格分成四段："
            "1. 剧情趋势 2. 人物推进 3. 当前风险 4. 下一步建议。"
            "只基于当前上下文回答，不要脑补未写出的剧情，不要空泛抒情。"
            "不要机械复述摘要原文，要提炼趋势、证据和变化。"
        )
    else:
        task_instruction = (
            "你是竹林 AI 的全局小说助手。你只能协助处理小说内容、章节、世界观、人物和关系相关事务。"
            "不要声称自己能改源代码、账号权限或系统配置。"
            "如果用户要求这些范围外的内容，直接说明你没有权限。"
            "回答用中文，务必具体，避免空话。"
            "只根据当前书籍上下文行动，不要脑补未写出的设定。"
            "给建议时优先落到可执行的修改点，不要用旁白口吻替用户做选择。"
            "不要把世界观、人物卡或摘要原文整段复读给用户，应整理后再回答。"
        )
    if prompt_scope_lines:
        task_instruction += (
            "本轮用户还指定了提示词范围。你的回答必须优先围绕已选人物卡、已选章节和自定义提示词展开。"
            "不要扩展到未选中的人物、章节或无关设定；如果上下文不足，直接说信息不足。"
            "不要输出与用户问题无关的铺垫、寒暄或泛泛总结。"
        )
    else:
        task_instruction += "不要输出与用户问题无关的铺垫、寒暄或泛泛总结。"

    if chapter is not None and _assistant_is_edit_request(message):
        task_instruction += (
            "如果用户明确要求你直接修改当前章节，请你在正常中文回复后，额外追加一个机器可读标签。"
            "标签格式必须是 <assistant_edit>{...}</assistant_edit>，其中 JSON 结构固定为 "
            '{"target_field":"content|outline|summary","title":"简短标题","content":"改写后的完整文本"}。'
            "只有在你确实给出了可直接写入当前章节的完整改写稿时，才能输出这个标签。"
            "如果用户意图不明确，或你无法判断应该改正文/大纲/摘要哪一项，就不要输出标签，直接先追问。"
            "标签中的 content 必须是可直接覆盖写入的完整结果，不要只给片段。"
        )

    messages = [
        {
            "role": "system",
            "content": f"{task_instruction}\n\n以下是本次可用的书籍上下文：\n{book_context}",
        }
    ]
    for item in history[-6:]:
        content = str(item.content or "").strip()
        if not content:
            continue
        messages.append({"role": item.role, "content": content[:2000]})
    user_prompt_parts = []
    if selected_character_names:
        user_prompt_parts.append(f"本次指定人物卡：{'、'.join(selected_character_names)}。")
    if selected_chapter_titles:
        user_prompt_parts.append(f"本次指定章节：{'、'.join(selected_chapter_titles)}。")
    if custom_prompt_text:
        user_prompt_parts.append(f"本次自定义提示词：{custom_prompt_text}")
    user_prompt_parts.append(f"用户问题：{str(message or '').strip()[:4000]}")
    messages.append({"role": "user", "content": "\n".join(user_prompt_parts)})

    model_name = str(config.model_name or "").strip().lower()
    is_reasoning_model = "reasoner" in model_name or "reasoning" in model_name
    if is_reasoning_model:
        return config, messages, (3200 if mode == "trend" else 3600)
    return config, messages, (1400 if mode == "trend" else 1600)


def _assistant_run_book_chat(
    db: Session,
    *,
    current_user: User,
    book: Book,
    chapter: Optional[Chapter],
    page: Optional[str],
    message: str,
    history: list[AssistantHistoryMessage],
    mode: Literal["general", "trend"],
    selected_character_ids: list[int],
    selected_chapter_ids: list[int],
    custom_prompt: Optional[str],
    current_chapter_title: Optional[str],
    current_chapter_summary: Optional[str],
    current_chapter_outline: Optional[str],
    current_chapter_content: Optional[str],
) -> tuple[str, dict[str, Any], Optional[dict[str, Any]]]:
    config, messages, max_tokens_override = _assistant_prepare_chat_request(
        db,
        current_user=current_user,
        book=book,
        chapter=chapter,
        page=page,
        message=message,
        history=history,
        mode=mode,
        selected_character_ids=selected_character_ids,
        selected_chapter_ids=selected_chapter_ids,
        custom_prompt=custom_prompt,
        current_chapter_title=current_chapter_title,
        current_chapter_summary=current_chapter_summary,
        current_chapter_outline=current_chapter_outline,
        current_chapter_content=current_chapter_content,
    )

    payload = call_openai_compatible_chat(
        config,
        messages=messages,
        max_tokens_override=max_tokens_override,
    )
    reply_text, edit_proposal = _assistant_extract_edit_proposal(payload["text"].strip())
    reply_text = _assistant_finalize_reply_text(reply_text, edit_proposal)
    return reply_text, config.public_dict(), edit_proposal


def _assistant_stream_book_chat_events(
    db: Session,
    *,
    current_user: User,
    book: Book,
    chapter: Optional[Chapter],
    page: Optional[str],
    message: str,
    history: list[AssistantHistoryMessage],
    mode: Literal["general", "trend"],
    selected_character_ids: list[int],
    selected_chapter_ids: list[int],
    custom_prompt: Optional[str],
    current_chapter_title: Optional[str],
    current_chapter_summary: Optional[str],
    current_chapter_outline: Optional[str],
    current_chapter_content: Optional[str],
):
    config, messages, max_tokens_override = _assistant_prepare_chat_request(
        db,
        current_user=current_user,
        book=book,
        chapter=chapter,
        page=page,
        message=message,
        history=history,
        mode=mode,
        selected_character_ids=selected_character_ids,
        selected_chapter_ids=selected_chapter_ids,
        custom_prompt=custom_prompt,
        current_chapter_title=current_chapter_title,
        current_chapter_summary=current_chapter_summary,
        current_chapter_outline=current_chapter_outline,
        current_chapter_content=current_chapter_content,
    )

    suppressed_tail = ""
    raw_text = ""
    visible_text = ""
    holdback = max(1, len("<assistant_edit>") - 1)
    suppress_edit_block = False

    for stream_event in iter_openai_compatible_chat_stream(
        config,
        messages=messages,
        max_tokens_override=max_tokens_override,
    ):
        event_type = str(stream_event.get("type") or "")
        if event_type == "delta":
            delta = str(stream_event.get("delta") or "")
            if not delta:
                continue
            raw_text += delta
            if suppress_edit_block:
                suppressed_tail += delta
                continue

            suppressed_tail += delta
            start_index = suppressed_tail.find("<assistant_edit>")
            if start_index != -1:
                safe_text = suppressed_tail[:start_index]
                if safe_text:
                    visible_text += safe_text
                    yield {"type": "delta", "delta": safe_text, "text": visible_text}
                suppressed_tail = suppressed_tail[start_index:]
                suppress_edit_block = True
                continue

            if len(suppressed_tail) > holdback:
                safe_text = suppressed_tail[:-holdback]
                suppressed_tail = suppressed_tail[-holdback:]
                if safe_text:
                    visible_text += safe_text
                    yield {"type": "delta", "delta": safe_text, "text": visible_text}
            continue

        if event_type == "done":
            if not suppress_edit_block and suppressed_tail:
                visible_text += suppressed_tail
                yield {"type": "delta", "delta": suppressed_tail, "text": visible_text}
                suppressed_tail = ""
            reply_text, edit_proposal = _assistant_extract_edit_proposal(raw_text.strip())
            if not raw_text.strip():
                logger.warning(
                    "assistant_stream_empty_visible_text book_id=%s chapter_id=%s model=%s mode=%s; falling back to non-stream completion",
                    book.id,
                    chapter.id if chapter is not None else None,
                    config.model_name,
                    mode,
                )
                fallback_payload = call_openai_compatible_chat(
                    config,
                    messages=messages,
                    max_tokens_override=max_tokens_override,
                )
                reply_text, edit_proposal = _assistant_extract_edit_proposal(fallback_payload["text"].strip())

            reply_text = _assistant_finalize_reply_text(reply_text, edit_proposal)
            if reply_text and visible_text != reply_text:
                visible_text = reply_text
                yield {"type": "delta", "delta": reply_text, "text": visible_text}
            yield {
                "type": "final",
                "response": {
                    "reply": reply_text,
                    "action": {
                        "type": "chat",
                        "mode": mode,
                        "should_reload": False,
                    },
                    "ai_config": config.public_dict(),
                    "edit_proposal": edit_proposal,
                },
            }
            return


def serve_page(page_name: str) -> FileResponse:
    return FileResponse(
        STATIC_DIR / page_name,
        media_type="text/html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@app.get("/login", include_in_schema=False)
def login_page() -> FileResponse:
    return serve_page("login.html")


@app.get("/library", include_in_schema=False)
def library_page() -> FileResponse:
    return serve_page("library.html")


@app.get("/writer", include_in_schema=False)
def writer_page() -> FileResponse:
    return serve_page("writer.html")


@app.get("/characters", include_in_schema=False)
def characters_page() -> FileResponse:
    return serve_page("characters.html")


@app.get("/settings", include_in_schema=False)
def settings_page() -> FileResponse:
    return serve_page("settings.html")


@app.get("/history", include_in_schema=False)
def history_page() -> FileResponse:
    return serve_page("history.html")


@app.get("/world", include_in_schema=False)
def world_page() -> FileResponse:
    return serve_page("world.html")


@app.get("/admin", include_in_schema=False)
def admin_page() -> FileResponse:
    return serve_page("admin.html")


@app.get("/healthz", response_model=HealthResponse, tags=["system"])
def healthz() -> HealthResponse:
    return HealthResponse(
        status="ok",
        app_name=settings.app_name,
        environment=settings.app_env,
    )


@app.post("/api/v1/auth/login", response_model=TokenResponse, tags=["auth"])
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> TokenResponse:
    username = payload.username.strip()
    enforce_login_rate_limit(request, username)
    user = db.execute(
        select(User).where(User.username == username)
    ).scalar_one_or_none()
    password_hash = user.password_hash if user is not None else PASSWORD_TIMING_PADDING_HASH
    password_valid = verify_password(payload.password, password_hash)

    if user is None or not password_valid:
        record_login_failure(request, username)
        logger.warning("login_failed username=%s reason=invalid_credentials", username)
        raise unauthorized("Incorrect username or password.")

    if user.status != UserStatus.ACTIVE or not user.is_active:
        record_login_failure(request, username)
        logger.warning("login_failed username=%s reason=user_inactive", username)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is disabled or locked.",
        )

    clear_login_failures(request, username)
    user.last_login_at = datetime.now(timezone.utc)
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("login_success user_id=%s username=%s role=%s", user.id, user.username, user.role.value)

    expires_delta = timedelta(minutes=settings.access_token_expire_minutes)
    access_token = create_access_token(
        subject=str(user.id),
        extra_claims={
            "username": user.username,
            "role": user.role.value,
        },
        expires_delta=expires_delta,
    )

    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=int(expires_delta.total_seconds()),
        user=serialize_user(user),
    )


@app.get("/api/v1/auth/me", response_model=UserResponse, tags=["auth"])
def me(current_user: User = Depends(get_current_user)) -> UserResponse:
    return serialize_user(current_user)


@app.get("/api/v1/users", tags=["users"])
def list_users(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ensure_admin(current_user)
    users = db.execute(select(User).order_by(User.created_at.desc(), User.id.desc())).scalars().all()
    return {"items": [serialize_user_admin(user) for user in users]}


@app.post("/api/v1/users", status_code=status.HTTP_201_CREATED, tags=["users"])
def create_user(
    payload: UserCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ensure_admin(current_user)

    existing = db.execute(
        select(User).where(User.username == payload.username.strip())
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username already exists.")
    if payload.email:
        existing_email = db.execute(
            select(User).where(User.email == payload.email)
        ).scalar_one_or_none()
        if existing_email is not None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email already exists.")

    user = User(
        username=payload.username.strip(),
        display_name=payload.display_name,
        email=payload.email,
        password_hash=hash_password(payload.password),
        role=payload.role,
        status=payload.status,
        is_active=payload.is_active,
        notes=payload.notes,
        created_by_id=current_user.id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return serialize_user_admin(user)


@app.put("/api/v1/users/{user_id}", tags=["users"])
def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ensure_admin(current_user)
    user = get_user_or_404(db, user_id)
    updates = get_model_updates(payload)

    if "email" in updates and updates["email"] is not None:
        existing = db.execute(
            select(User).where(User.email == updates["email"], User.id != user_id)
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email already exists.")

    for field in ("display_name", "email", "role", "status", "is_active", "notes"):
        if field in updates:
            setattr(user, field, updates[field])

    db.add(user)
    db.commit()
    db.refresh(user)
    return serialize_user_admin(user)


@app.post("/api/v1/users/{user_id}/reset-password", tags=["users"])
def reset_user_password(
    user_id: int,
    payload: UserPasswordResetRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ensure_admin(current_user)
    user = get_user_or_404(db, user_id)
    user.password_hash = hash_password(payload.password)
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"id": user.id, "username": user.username, "message": "Password reset successfully."}


@app.get("/api/v1/admin/books/{book_id}/memory", tags=["admin"])
def get_admin_book_memory(
    book_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ensure_admin(current_user)
    book = db.execute(
        select(Book)
        .options(selectinload(Book.owner))
        .where(Book.id == book_id)
    ).scalar_one_or_none()
    if book is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="书籍不存在。")

    chapters = db.execute(
        select(Chapter)
        .options(selectinload(Chapter.episodic_memory))
        .where(Chapter.book_id == book.id)
        .order_by(
            Chapter.sequence_number.is_(None),
            Chapter.sequence_number.asc(),
            Chapter.sort_order.asc(),
            Chapter.id.asc(),
        )
    ).scalars().all()
    semantic_entries = db.execute(
        select(SemanticKnowledgeBase)
        .where(SemanticKnowledgeBase.book_id == book.id)
        .order_by(SemanticKnowledgeBase.updated_at.desc(), SemanticKnowledgeBase.id.desc())
    ).scalars().all()

    episodic_memories = [
        item
        for item in (serialize_admin_episodic_memory(chapter) for chapter in chapters)
        if item is not None
    ]
    derived_style_summary, derived_style_summary_updated_at = get_derived_style_summary(book)
    return {
        "book": serialize_book(book, include_detail=True),
        "style_anchor": {
            "source": "derived_style_summary" if derived_style_summary else "none",
            "content": derived_style_summary,
            "updated_at": derived_style_summary_updated_at or None,
        },
        "episodic_memories": episodic_memories,
        "semantic_memories": [serialize_admin_semantic_memory(entry) for entry in semantic_entries],
        "memory_stats": {
            "episodic_count": len(episodic_memories),
            "semantic_count": len(semantic_entries),
            "chapter_count": len(chapters),
        },
    }


@app.get("/api/v1/admin/database-backup", tags=["admin"])
def get_database_backup_settings(
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    ensure_admin(current_user)
    return get_backup_status(settings.database_url)


@app.put("/api/v1/admin/database-backup", tags=["admin"])
def update_database_backup_configuration(
    payload: DatabaseBackupSettingsUpdateRequest,
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    ensure_admin(current_user)
    if payload.interval_hours < MIN_INTERVAL_HOURS or payload.interval_hours > MAX_INTERVAL_HOURS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"备份间隔必须在 {MIN_INTERVAL_HOURS} 到 {MAX_INTERVAL_HOURS} 小时之间。",
        )
    if payload.retention_days < MIN_RETENTION_DAYS or payload.retention_days > MAX_RETENTION_DAYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"备份保留天数必须在 {MIN_RETENTION_DAYS} 到 {MAX_RETENTION_DAYS} 天之间。",
        )
    update_backup_settings(
        enabled=payload.enabled,
        interval_hours=payload.interval_hours,
        retention_days=payload.retention_days,
    )
    return get_backup_status(settings.database_url)


@app.post("/api/v1/admin/database-backup/run", tags=["admin"])
def run_database_backup(
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    ensure_admin(current_user)
    try:
        result = run_database_backup_now(settings.database_url, reason="manual")
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    status_payload = get_backup_status(settings.database_url)
    return {
        "message": "数据库备份已完成。",
        "result": result,
        "settings": status_payload,
    }


@app.get("/api/v1/admin/database-backup/files/{filename}", tags=["admin"])
def download_database_backup(
    filename: str,
    current_user: User = Depends(get_current_user),
) -> FileResponse:
    ensure_admin(current_user)
    try:
        backup_path = resolve_backup_file_path(filename)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return FileResponse(
        path=backup_path,
        media_type="application/octet-stream",
        filename=backup_path.name,
    )


@app.post("/api/v1/admin/database-backup/restore", tags=["admin"])
def restore_database_backup(
    payload: DatabaseBackupRestoreRequest,
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    ensure_admin(current_user)
    try:
        engine.dispose()
        result = restore_database_from_backup(
            settings.database_url,
            filename=payload.filename,
            create_safety_backup=payload.create_safety_backup,
        )
        init_database()
        migration_session = SessionLocal()
        try:
            migrate_ai_config_module_schema(migration_session)
            repair_ai_config_legacy_sqlite_references(migration_session)
            migrate_ai_config_api_keys(migration_session)
            migrate_world_schema(migration_session)
        finally:
            migration_session.close()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    status_payload = get_backup_status(settings.database_url)
    return {
        "message": "数据库已从备份恢复。",
        "result": result,
        "settings": status_payload,
    }


@app.post("/api/v1/auth/change-password", tags=["auth"])
def change_own_password(
    payload: ChangeOwnPasswordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """用户修改自己的密码，需验证旧密码。"""
    if not payload.old_password or not payload.new_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="旧密码和新密码都不能为空。")
    if len(payload.new_password) < 6:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="新密码长度至少 6 位。")
    if not verify_password(payload.old_password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="旧密码不正确。")
    current_user.password_hash = hash_password(payload.new_password)
    db.add(current_user)
    db.commit()
    db.refresh(current_user)
    return {"id": current_user.id, "username": current_user.username, "message": "密码修改成功。"}


@app.get("/api/v1/books", tags=["books"])
def list_books(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    stmt = select(Book).order_by(Book.updated_at.desc(), Book.id.desc())
    if not is_admin(current_user):
        stmt = stmt.where(Book.owner_id == current_user.id)

    books = db.execute(stmt).scalars().all()
    return {"items": [serialize_book(book) for book in books]}


@app.post("/api/v1/books", status_code=status.HTTP_201_CREATED, tags=["books"])
def create_book(
    payload: BookCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    owner_id = resolve_book_owner_id(db, payload.owner_id, current_user)
    book = Book(
        owner_id=owner_id,
        title=payload.title.strip(),
        slug=payload.slug,
        description=payload.description,
        genre=payload.genre,
        language=payload.language,
        tags=payload.tags or [],
        global_style_prompt=payload.global_style_prompt,
        long_term_summary=payload.long_term_summary,
        world_bible=payload.world_bible,
        outline=payload.outline,
        status=payload.status,
        extra_data=payload.extra_data or {},
    )
    db.add(book)
    db.commit()
    db.refresh(book)
    return serialize_book(book, include_detail=True)


@app.get("/api/v1/books/{book_id}", tags=["books"])
def get_book(
    book_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    return serialize_book(book, include_detail=True)


@app.put("/api/v1/books/{book_id}", tags=["books"])
def update_book(
    book_id: int,
    payload: BookUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)

    updates = get_model_updates(payload)
    if "owner_id" in updates:
        book.owner_id = resolve_book_owner_id(db, updates["owner_id"], current_user)
    if "title" in updates and updates["title"] is not None:
        book.title = updates["title"].strip()
    for field in (
        "slug",
        "description",
        "genre",
        "language",
        "tags",
        "global_style_prompt",
        "long_term_summary",
        "world_bible",
        "outline",
        "status",
        "extra_data",
    ):
        if field in updates:
            setattr(book, field, updates[field])

    db.add(book)
    db.commit()
    db.refresh(book)
    return serialize_book(book, include_detail=True)


@app.get("/api/v1/books/{book_id}/project-archive", tags=["books"])
def export_book_project_archive(
    book_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    archive_bytes = build_book_project_archive_bytes(db, book, current_user)
    safe_title = _project_archive_safe_name(book.title, f"book-{book.id}")
    ascii_title = re.sub(r"[^A-Za-z0-9._-]+", "-", safe_title).strip("-") or f"book-{book.id}"
    filename = f"{ascii_title}-project-0.2.0.zip"
    utf8_filename = quote(f"{safe_title}-project-0.2.0.zip")
    return StreamingResponse(
        io.BytesIO(archive_bytes),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"; filename*=UTF-8\'\'{utf8_filename}'
        },
    )


@app.post("/api/v1/books/{book_id}/project-archive/import", tags=["books"])
def import_book_project_archive_endpoint(
    book_id: int,
    file: UploadFile = File(...),
    merge_strategy: str = Form(default="smart_merge"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    filename = str(file.filename or "").strip().lower()
    if not filename.endswith(".zip"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="请上传 ZIP 压缩包。")
    file_bytes = file.file.read()
    if not file_bytes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="导入压缩包为空。")
    report = import_book_project_archive(
        db=db,
        book=book,
        current_user=current_user,
        file_bytes=file_bytes,
        merge_strategy=merge_strategy,
    )
    return {
        "ok": True,
        "book_id": book.id,
        "book_title": book.title,
        "report": report,
    }


@app.post("/api/v1/books/{book_id}/project-archive/preview", tags=["books"])
def preview_book_project_archive_import(
    book_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    filename = str(file.filename or "").strip().lower()
    if not filename.endswith(".zip"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="请上传 ZIP 压缩包。")
    file_bytes = file.file.read()
    if not file_bytes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="导入压缩包为空。")

    archive_payload = _project_archive_read_payload(file_bytes)
    preview = build_book_project_import_preview(db, book, archive_payload)
    session_id = save_book_project_import_session(
        book=book,
        current_user=current_user,
        archive_payload=archive_payload,
        preview=preview,
    )
    return {
        "ok": True,
        "book_id": book.id,
        "book_title": book.title,
        "session_id": session_id,
        "preview": preview,
    }


@app.post("/api/v1/books/{book_id}/project-archive/apply", tags=["books"])
def apply_book_project_archive_import(
    book_id: int,
    request: BookProjectImportApplyRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)

    session_payload = load_book_project_import_session(request.session_id)
    if session_payload.get("book_id") != book.id or session_payload.get("user_id") != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="当前账号不能使用这个导入会话。")

    report = apply_book_project_archive_payload(
        db=db,
        book=book,
        current_user=current_user,
        archive_payload=session_payload.get("archive_payload") or {},
        merge_strategy=request.merge_strategy,
        decisions=request.decisions,
    )
    delete_book_project_import_session(request.session_id)
    return {
        "ok": True,
        "book_id": book.id,
        "book_title": book.title,
        "report": report,
    }


@app.delete("/api/v1/books/{book_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["books"])
def delete_book(
    book_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    db.delete(book)
    db.commit()


@app.get("/api/v1/books/{book_id}/chapters", tags=["chapters"])
def list_chapters(
    book_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    chapters = db.execute(
        select(Chapter)
        .where(Chapter.book_id == book_id)
        .order_by(Chapter.sort_order.asc(), Chapter.id.asc())
    ).scalars().all()
    return {"items": [serialize_chapter_tree_item(chapter) for chapter in chapters]}


@app.post("/api/v1/books/{book_id}/chapters", status_code=status.HTTP_201_CREATED, tags=["chapters"])
def create_chapter(
    book_id: int,
    payload: ChapterCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)

    parent = validate_parent_assignment(db, book_id=book_id, parent_id=payload.parent_id)
    chapter = Chapter(
        book_id=book_id,
        parent_id=parent.id if parent else None,
        title=payload.title.strip(),
        node_type=payload.node_type,
        status=payload.status,
        sequence_number=payload.sequence_number,
        sort_order=payload.sort_order if payload.sort_order is not None else next_chapter_sort_order(db, book_id, payload.parent_id),
        depth=0,
        tree_path="",
        summary=payload.summary,
        outline=payload.outline,
        content=payload.content,
        context_summary=payload.context_summary,
        word_count=estimate_text_units(payload.content),
        version=1,
        extra_data=payload.extra_data or {},
    )
    db.add(chapter)
    db.flush()
    rebuild_chapter_tree(db, chapter)
    refresh_book_aggregates(db, book)
    db.commit()
    db.refresh(chapter)
    logger.info(
        "chapter_created user_id=%s book_id=%s chapter_id=%s parent_id=%s title=%r",
        current_user.id,
        book_id,
        chapter.id,
        chapter.parent_id,
        chapter.title,
    )
    return serialize_chapter_detail(chapter)


@app.get("/api/v1/books/{book_id}/chapters/{chapter_id}", tags=["chapters"])
def get_chapter(
    book_id: int,
    chapter_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    chapter = get_chapter_or_404(db, book_id, chapter_id)
    return serialize_chapter_detail(chapter)


@app.put("/api/v1/books/{book_id}/chapters/{chapter_id}", tags=["chapters"])
def update_chapter(
    book_id: int,
    chapter_id: int,
    payload: ChapterUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    chapter = get_chapter_or_404(db, book_id, chapter_id)

    updates = get_model_updates(payload)
    content_fields_changed = False
    should_schedule_memory_consolidation = False

    if "parent_id" in updates:
        parent = validate_parent_assignment(
            db,
            book_id=book_id,
            parent_id=updates["parent_id"],
            chapter_id=chapter_id,
        )
        chapter.parent_id = parent.id if parent else None

    if "title" in updates and updates["title"] is not None:
        chapter.title = updates["title"].strip()
    for field in (
        "node_type",
        "status",
        "sequence_number",
        "sort_order",
        "summary",
        "outline",
        "content",
        "context_summary",
        "extra_data",
    ):
        if field in updates:
            setattr(chapter, field, updates[field])
            if field in {"summary", "outline", "content"}:
                content_fields_changed = True

    chapter.word_count = estimate_text_units(chapter.content or "")
    if content_fields_changed:
        chapter.version += 1

    db.add(chapter)
    db.flush()
    rebuild_chapter_tree(db, chapter)
    refresh_book_aggregates(db, book)
    db.commit()
    db.refresh(chapter)
    if (
        isinstance(chapter.content, str)
        and chapter.content.strip()
        and (
            content_fields_changed
            or ("status" in updates and chapter.status == ChapterStatus.FINAL)
        )
    ):
        should_schedule_memory_consolidation = True
    if should_schedule_memory_consolidation:
        schedule_chapter_memory_consolidation(chapter.id)
    return serialize_chapter_detail(chapter)


@app.delete("/api/v1/books/{book_id}/chapters/{chapter_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["chapters"])
def delete_chapter(
    book_id: int,
    chapter_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    chapter = get_chapter_or_404(db, book_id, chapter_id)
    db.delete(chapter)
    db.flush()
    refresh_book_aggregates(db, book)
    db.commit()


@app.get("/api/v1/books/{book_id}/snapshots", tags=["snapshots"])
def list_book_snapshots(
    book_id: int,
    chapter_id: Optional[int] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)

    stmt = (
        select(Snapshot)
        .where(Snapshot.book_id == book_id)
        .order_by(Snapshot.created_at.desc(), Snapshot.id.desc())
        .limit(limit)
    )
    if chapter_id is not None:
        get_chapter_or_404(db, book_id, chapter_id)
        stmt = (
            select(Snapshot)
            .where(Snapshot.book_id == book_id, Snapshot.chapter_id == chapter_id)
            .order_by(Snapshot.created_at.desc(), Snapshot.id.desc())
            .limit(limit)
        )

    snapshots = db.execute(stmt).scalars().all()
    return {"items": [serialize_snapshot(snapshot) for snapshot in snapshots]}


@app.get("/api/v1/books/{book_id}/snapshots/{snapshot_id}", tags=["snapshots"])
def get_snapshot_detail(
    book_id: int,
    snapshot_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    snapshot = get_snapshot_or_404(db, book_id, snapshot_id)
    return serialize_snapshot_detail(snapshot)


@app.get("/api/v1/snapshots/recent", tags=["snapshots"])
def list_recent_snapshots(
    limit: int = Query(default=10, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    stmt = (
        select(Snapshot)
        .join(Book, Snapshot.book_id == Book.id)
        .order_by(Snapshot.created_at.desc(), Snapshot.id.desc())
        .limit(limit)
    )
    if not is_admin(current_user):
        stmt = (
            select(Snapshot)
            .join(Book, Snapshot.book_id == Book.id)
            .where(Book.owner_id == current_user.id)
            .order_by(Snapshot.created_at.desc(), Snapshot.id.desc())
            .limit(limit)
        )

    snapshots = db.execute(stmt).scalars().all()
    return {"items": [serialize_snapshot(snapshot) for snapshot in snapshots]}


@app.get("/api/v1/books/{book_id}/characters", tags=["world"])
def list_characters(
    book_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    characters = db.execute(
        select(Character)
        .where(Character.book_id == book_id)
        .order_by(Character.name.asc(), Character.id.asc())
    ).scalars().all()
    return {"items": [serialize_character(character) for character in characters]}


@app.post("/api/v1/books/{book_id}/characters", status_code=status.HTTP_201_CREATED, tags=["world"])
def create_character(
    book_id: int,
    payload: CharacterCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)

    existing = db.execute(
        select(Character).where(Character.book_id == book_id, Character.name == payload.name.strip())
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="当前书籍中已存在同名人物，请修改名称后再保存。")

    character = Character(
        book_id=book_id,
        name=payload.name.strip(),
        aliases=payload.aliases or [],
        role_label=payload.role_label,
        description=payload.biography if payload.biography is not None else payload.description,
        traits=payload.traits or [],
        background=payload.background,
        goals=payload.goals,
        secrets=payload.secrets,
        notes=payload.notes,
        first_appearance_chapter_id=validate_book_chapter_reference(db, book_id, payload.first_appearance_chapter_id),
        last_appearance_chapter_id=validate_book_chapter_reference(db, book_id, payload.last_appearance_chapter_id),
        is_active=payload.is_active,
        card_json=merge_character_card_json(
            payload.card_json,
            life_statuses=payload.life_statuses,
            timeline_entries=payload.timeline_entries,
        ),
    )
    db.add(character)
    db.commit()
    db.refresh(character)
    return serialize_character(character)


@app.get("/api/v1/books/{book_id}/characters/{character_id}", tags=["world"])
def get_character(
    book_id: int,
    character_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    character = get_character_or_404(db, book_id, character_id)
    return serialize_character(character)


@app.put("/api/v1/books/{book_id}/characters/{character_id}", tags=["world"])
def update_character(
    book_id: int,
    character_id: int,
    payload: CharacterUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    character = get_character_or_404(db, book_id, character_id)
    updates = get_model_updates(payload)

    if "name" in updates and updates["name"] is not None:
        name = updates["name"].strip()
        existing = db.execute(
            select(Character).where(
                Character.book_id == book_id,
                Character.name == name,
                Character.id != character_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="当前书籍中已存在同名人物，请修改名称后再保存。")
        character.name = name

    if "biography" in updates:
        character.description = updates["biography"]
    elif "description" in updates:
        character.description = updates["description"]

    for field in ("aliases", "role_label", "traits", "background", "goals", "secrets", "notes", "is_active"):
        if field in updates:
            value = updates[field]
            if field in {"aliases", "traits"} and value is None:
                value = []
            setattr(character, field, value)

    if "card_json" in updates or "life_statuses" in updates or "timeline_entries" in updates:
        base_card_json = updates.get("card_json", character.card_json)
        if base_card_json is None:
            base_card_json = {}
        character.card_json = merge_character_card_json(
            base_card_json,
            life_statuses=updates["life_statuses"] if "life_statuses" in updates else None,
            timeline_entries=updates["timeline_entries"] if "timeline_entries" in updates else None,
        )

    if "first_appearance_chapter_id" in updates:
        character.first_appearance_chapter_id = validate_book_chapter_reference(db, book_id, updates["first_appearance_chapter_id"])
    if "last_appearance_chapter_id" in updates:
        character.last_appearance_chapter_id = validate_book_chapter_reference(db, book_id, updates["last_appearance_chapter_id"])

    db.add(character)
    db.commit()
    db.refresh(character)
    return serialize_character(character)


@app.delete("/api/v1/books/{book_id}/characters/{character_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["world"])
def delete_character(
    book_id: int,
    character_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    character = get_character_or_404(db, book_id, character_id)
    db.execute(
        sql_delete(Relation).where(
            Relation.book_id == book_id,
            or_(
                Relation.source_character_id == character_id,
                Relation.target_character_id == character_id,
            ),
        )
    )
    db.flush()
    db.delete(character)
    db.commit()


@app.delete("/api/v1/books/{book_id}/characters", tags=["world"])
def delete_all_characters(
    book_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    deleted_relation_count = db.execute(
        sql_delete(Relation).where(Relation.book_id == book_id)
    ).rowcount or 0
    deleted_character_count = db.execute(
        sql_delete(Character).where(Character.book_id == book_id)
    ).rowcount or 0
    db.commit()
    return {
        "deleted_character_count": deleted_character_count,
        "deleted_relation_count": deleted_relation_count,
    }


@app.get("/api/v1/books/{book_id}/relations", tags=["world"])
def list_relations(
    book_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    relations = db.execute(
        select(Relation)
        .where(Relation.book_id == book_id)
        .order_by(Relation.id.asc())
    ).scalars().all()
    return {"items": [serialize_relation(relation, include_description=False) for relation in relations]}


@app.get("/api/v1/books/{book_id}/relations/{relation_id}/events", tags=["world"])
def list_relation_events(
    book_id: int,
    relation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    relation = get_relation_or_404(db, book_id, relation_id)
    events = db.execute(
        select(RelationEvent)
        .where(RelationEvent.relation_id == relation.id)
        .order_by(RelationEvent.chapter_id.asc(), RelationEvent.id.asc())
    ).scalars().all()
    return {"items": [serialize_relation_event(event) for event in events]}


@app.post("/api/v1/books/{book_id}/relations", status_code=status.HTTP_201_CREATED, tags=["world"])
def create_relation(
    book_id: int,
    payload: RelationCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    source_character_id, target_character_id = validate_relation_endpoints(
        db,
        book_id,
        payload.source_character_id,
        payload.target_character_id,
    )
    relation_type = normalize_relation_type(payload.relation_type)
    relation_label = sanitize_relation_text(payload.label) or sanitize_relation_text(payload.relation_type) or relation_type_label(relation_type)
    valid_from_chapter_id = validate_book_chapter_reference(db, book_id, payload.valid_from_chapter_id)
    valid_to_chapter_id = validate_book_chapter_reference(db, book_id, payload.valid_to_chapter_id)
    relation = find_relation_by_signature(
        db,
        book_id=book_id,
        source_character_id=source_character_id,
        target_character_id=target_character_id,
        relation_type=relation_type,
    )
    if relation is None:
        relation = Relation(
            book_id=book_id,
            source_character_id=source_character_id,
            target_character_id=target_character_id,
            relation_type=relation_type,
        )
    relation.label = relation_label
    relation.description = normalize_relation_description(payload.description)
    relation.strength = payload.strength
    relation.importance_level = normalize_relation_importance(payload.importance_level)
    relation.is_bidirectional = payload.is_bidirectional
    relation.valid_from_chapter_id = valid_from_chapter_id
    relation.valid_to_chapter_id = valid_to_chapter_id
    relation.extra_data = _merge_relation_extra(relation.extra_data, payload.extra_data)
    db.add(relation)
    db.flush()
    record_relation_event(
        db,
        relation,
        chapter_id=relation.valid_from_chapter_id,
        segment_label="manual_edit",
        relation_type=relation.relation_type,
        label=relation.label,
        description=relation.description,
        strength=relation.strength,
        importance_level=relation.importance_level,
        is_bidirectional=relation.is_bidirectional,
        event_summary=relation.description,
    )
    db.commit()
    db.refresh(relation)
    return serialize_relation(relation)


@app.get("/api/v1/books/{book_id}/relations/{relation_id}", tags=["world"])
def get_relation(
    book_id: int,
    relation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    relation = get_relation_or_404(db, book_id, relation_id)
    payload = serialize_relation(relation)
    payload["events"] = [serialize_relation_event(item) for item in relation.events or []]
    return payload


@app.put("/api/v1/books/{book_id}/relations/{relation_id}", tags=["world"])
def update_relation(
    book_id: int,
    relation_id: int,
    payload: RelationUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    relation = get_relation_or_404(db, book_id, relation_id)
    updates = get_model_updates(payload)

    source_id = updates.get("source_character_id", relation.source_character_id)
    target_id = updates.get("target_character_id", relation.target_character_id)
    source_character_id, target_character_id = validate_relation_endpoints(db, book_id, source_id, target_id)
    relation_type = normalize_relation_type(updates.get("relation_type", relation.relation_type))
    duplicate = find_relation_by_signature(
        db,
        book_id=book_id,
        source_character_id=source_character_id,
        target_character_id=target_character_id,
        relation_type=relation_type,
        exclude_relation_id=relation.id,
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="相同人物对与关系类别的关系已存在，请直接编辑现有关系。",
        )

    relation.source_character_id = source_character_id
    relation.target_character_id = target_character_id

    if "relation_type" in updates:
        relation.relation_type = relation_type
    if "label" in updates:
        relation.label = sanitize_relation_text(updates.get("label")) or sanitize_relation_text(relation.label) or relation_type_label(relation.relation_type)
    elif not relation.label:
        relation.label = relation_type_label(relation.relation_type)
    if "description" in updates:
        relation.description = normalize_relation_description(updates.get("description"))
    if "strength" in updates:
        relation.strength = updates.get("strength")
    if "importance_level" in updates:
        relation.importance_level = normalize_relation_importance(updates.get("importance_level"))
    if "is_bidirectional" in updates:
        relation.is_bidirectional = bool(updates.get("is_bidirectional"))
    if "extra_data" in updates:
        relation.extra_data = _merge_relation_extra(relation.extra_data, updates.get("extra_data"))

    if "valid_from_chapter_id" in updates:
        relation.valid_from_chapter_id = validate_book_chapter_reference(db, book_id, updates["valid_from_chapter_id"])
    if "valid_to_chapter_id" in updates:
        relation.valid_to_chapter_id = validate_book_chapter_reference(db, book_id, updates["valid_to_chapter_id"])

    db.add(relation)
    db.flush()
    record_relation_event(
        db,
        relation,
        chapter_id=relation.valid_from_chapter_id,
        segment_label="manual_edit",
        relation_type=relation.relation_type,
        label=relation.label,
        description=relation.description,
        strength=relation.strength,
        importance_level=relation.importance_level,
        is_bidirectional=relation.is_bidirectional,
        event_summary=relation.description,
    )
    db.commit()
    db.refresh(relation)
    return serialize_relation(relation)


@app.delete("/api/v1/books/{book_id}/relations/{relation_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["world"])
def delete_relation(
    book_id: int,
    relation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    relation = get_relation_or_404(db, book_id, relation_id)
    db.delete(relation)
    db.commit()


@app.delete("/api/v1/books/{book_id}/relations", tags=["world"])
def delete_all_relations(
    book_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    deleted_count = db.execute(
        sql_delete(Relation).where(Relation.book_id == book_id)
    ).rowcount or 0
    db.commit()
    return {"deleted_count": deleted_count}


@app.get("/api/v1/books/{book_id}/factions", tags=["world"])
def list_factions(
    book_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    factions = db.execute(
        select(Faction).where(Faction.book_id == book_id).order_by(Faction.name.asc(), Faction.id.asc())
    ).scalars().all()
    return {"items": [serialize_faction(faction) for faction in factions]}


@app.post("/api/v1/books/{book_id}/factions", status_code=status.HTTP_201_CREATED, tags=["world"])
def create_faction(
    book_id: int,
    payload: FactionCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    name = str(payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Faction name is required.")
    existing = db.execute(
        select(Faction).where(Faction.book_id == book_id, Faction.name == name)
    ).scalars().first()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Faction name already exists.")
    faction = Faction(
        book_id=book_id,
        name=name,
        description=str(payload.description or "").strip() or None,
        color=str(payload.color or "").strip() or None,
        extra_data=payload.extra_data or {},
    )
    db.add(faction)
    db.commit()
    db.refresh(faction)
    return serialize_faction(faction)


@app.get("/api/v1/books/{book_id}/factions/{faction_id}", tags=["world"])
def get_faction(
    book_id: int,
    faction_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    faction = get_faction_or_404(db, book_id, faction_id)
    return serialize_faction(faction, include_memberships=True)


@app.put("/api/v1/books/{book_id}/factions/{faction_id}", tags=["world"])
def update_faction(
    book_id: int,
    faction_id: int,
    payload: FactionUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    faction = get_faction_or_404(db, book_id, faction_id)
    updates = get_model_updates(payload)
    if "name" in updates:
        name = str(updates.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Faction name is required.")
        duplicate = db.execute(
            select(Faction).where(Faction.book_id == book_id, Faction.name == name, Faction.id != faction.id)
        ).scalars().first()
        if duplicate is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Faction name already exists.")
        faction.name = name
    if "description" in updates:
        faction.description = str(updates.get("description") or "").strip() or None
    if "color" in updates:
        faction.color = str(updates.get("color") or "").strip() or None
    if "extra_data" in updates:
        faction.extra_data = _merge_relation_extra(faction.extra_data, updates.get("extra_data"))
    db.add(faction)
    db.commit()
    db.refresh(faction)
    return serialize_faction(faction, include_memberships=True)


@app.delete("/api/v1/books/{book_id}/factions/{faction_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["world"])
def delete_faction(
    book_id: int,
    faction_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    faction = get_faction_or_404(db, book_id, faction_id)
    db.delete(faction)
    db.commit()


@app.get("/api/v1/books/{book_id}/faction-memberships", tags=["world"])
def list_faction_memberships(
    book_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    memberships = db.execute(
        select(FactionMembership)
        .where(FactionMembership.book_id == book_id)
        .order_by(FactionMembership.id.asc())
    ).scalars().all()
    return {"items": [serialize_faction_membership(item) for item in memberships]}


@app.post("/api/v1/books/{book_id}/faction-memberships", status_code=status.HTTP_201_CREATED, tags=["world"])
def create_faction_membership(
    book_id: int,
    payload: FactionMembershipCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    faction_id, character_id = validate_faction_membership_refs(
        db,
        book_id=book_id,
        faction_id=payload.faction_id,
        character_id=payload.character_id,
    )
    membership = FactionMembership(
        book_id=book_id,
        faction_id=faction_id,
        character_id=character_id,
        role_label=str(payload.role_label or "").strip() or None,
        loyalty=payload.loyalty,
        status=normalize_faction_status(payload.status),
        start_chapter_id=validate_book_chapter_reference(db, book_id, payload.start_chapter_id),
        end_chapter_id=validate_book_chapter_reference(db, book_id, payload.end_chapter_id),
        notes=str(payload.notes or "").strip() or None,
    )
    db.add(membership)
    db.commit()
    db.refresh(membership)
    return serialize_faction_membership(membership)


@app.put("/api/v1/books/{book_id}/faction-memberships/{membership_id}", tags=["world"])
def update_faction_membership(
    book_id: int,
    membership_id: int,
    payload: FactionMembershipUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    membership = get_faction_membership_or_404(db, book_id, membership_id)
    updates = get_model_updates(payload)
    if "faction_id" in updates or "character_id" in updates:
        faction_id, character_id = validate_faction_membership_refs(
            db,
            book_id=book_id,
            faction_id=int(updates.get("faction_id", membership.faction_id)),
            character_id=int(updates.get("character_id", membership.character_id)),
        )
        membership.faction_id = faction_id
        membership.character_id = character_id
    if "role_label" in updates:
        membership.role_label = str(updates.get("role_label") or "").strip() or None
    if "loyalty" in updates:
        membership.loyalty = updates.get("loyalty")
    if "status" in updates:
        membership.status = normalize_faction_status(updates.get("status"))
    if "start_chapter_id" in updates:
        membership.start_chapter_id = validate_book_chapter_reference(db, book_id, updates.get("start_chapter_id"))
    if "end_chapter_id" in updates:
        membership.end_chapter_id = validate_book_chapter_reference(db, book_id, updates.get("end_chapter_id"))
    if "notes" in updates:
        membership.notes = str(updates.get("notes") or "").strip() or None
    db.add(membership)
    db.commit()
    db.refresh(membership)
    return serialize_faction_membership(membership)


@app.delete("/api/v1/books/{book_id}/faction-memberships/{membership_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["world"])
def delete_faction_membership(
    book_id: int,
    membership_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    membership = get_faction_membership_or_404(db, book_id, membership_id)
    db.delete(membership)
    db.commit()


@app.get("/api/v1/ai-configs", tags=["ai-configs"])
def list_ai_configs(
    scope: Optional[AIScope] = Query(default=None),
    module: Optional[AIModule] = Query(default=None),
    book_id: Optional[int] = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if book_id is not None:
        book = get_book_or_404(db, book_id)
        ensure_book_http_access(book, current_user)

    stmt = select(AIConfig).order_by(
        AIConfig.scope.asc(),
        AIConfig.module.asc(),
        AIConfig.priority.asc(),
        AIConfig.id.asc(),
    )
    if scope is not None:
        stmt = stmt.where(AIConfig.scope == scope)
    if module is not None:
        stmt = stmt.where(AIConfig.module == module)

    if not is_admin(current_user):
        visible_book_ids = select(Book.id).where(Book.owner_id == current_user.id)
        book_scope_condition = and_(
            AIConfig.scope == AIScope.BOOK,
            AIConfig.book_id.in_(visible_book_ids),
        )
        if book_id is not None:
            book_scope_condition = and_(
                AIConfig.scope == AIScope.BOOK,
                AIConfig.book_id == book_id,
            )

        stmt = stmt.where(
            or_(
                AIConfig.scope == AIScope.SYSTEM,
                and_(AIConfig.scope == AIScope.USER, AIConfig.user_id == current_user.id),
                book_scope_condition,
            )
        )

    configs = db.execute(stmt).scalars().all()
    return {
        "items": [
            serialize_ai_config(
                config,
                allow_runtime_env=is_admin(current_user),
                include_sensitive_fields=is_admin(current_user) or config.scope != AIScope.SYSTEM,
            )
            for config in configs
        ]
    }


@app.post("/api/v1/ai-configs/discover-models", tags=["ai-configs"])
def discover_ai_models(
    payload: AIModelDiscoveryRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    config, _api_format, base_url, api_key, timeout_seconds = resolve_ai_connection_inputs(
        db,
        payload,
        current_user,
    )

    try:
        items = fetch_openai_compatible_models(
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        _raise_ai_http_error(exc)

    return {
        "items": items,
        "count": len(items),
        "source_config_id": config.id if config else None,
    }


@app.post("/api/v1/ai-configs/test-connection", tags=["ai-configs"])
def test_ai_connection(
    payload: AIModelDiscoveryRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    config, _api_format, base_url, api_key, timeout_seconds = resolve_ai_connection_inputs(
        db,
        payload,
        current_user,
    )

    try:
        items = fetch_openai_compatible_models(
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        _raise_ai_http_error(exc)

    return {
        "ok": True,
        "source_config_id": config.id if config else None,
        "model_count": len(items),
        "sample_models": [item["id"] for item in items[:5]],
        "message": (
            f"Connection succeeded and returned {len(items)} models."
            if items
            else "Connection succeeded, but the provider did not return any models."
        ),
    }


@app.post("/api/v1/ai-configs", status_code=status.HTTP_201_CREATED, tags=["ai-configs"])
def create_ai_config(
    payload: AIConfigCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ensure_ai_env_var_permission(current_user, payload.model_dump(exclude_none=True))
    validated_base_url = validate_ai_base_url_for_user(current_user, payload.base_url, resolve_dns=False)
    user_id, book_id = coerce_ai_scope_targets(
        db,
        scope=payload.scope,
        user_id=payload.user_id,
        book_id=payload.book_id,
        current_user=current_user,
    )
    config = AIConfig(
        name=payload.name.strip(),
        scope=payload.scope,
        module=payload.module,
        user_id=user_id,
        book_id=book_id,
        provider_name=payload.provider_name,
        api_format=payload.api_format,
        base_url=validated_base_url,
        base_url_env_var=payload.base_url_env_var,
        api_key=encrypt_secret(payload.api_key),
        api_key_env_var=payload.api_key_env_var,
        model_name=payload.model_name,
        model_name_env_var=payload.model_name_env_var,
        reasoning_effort=payload.reasoning_effort,
        temperature=payload.temperature,
        top_p=payload.top_p,
        max_tokens=payload.max_tokens,
        timeout_seconds=payload.timeout_seconds,
        priority=payload.priority,
        is_enabled=payload.is_enabled,
        is_default=payload.is_default,
        system_prompt_template=payload.system_prompt_template,
        extra_headers=payload.extra_headers or {},
        extra_body=payload.extra_body or {},
        notes=payload.notes,
    )
    db.add(config)
    db.flush()
    unset_competing_defaults(db, config)
    db.commit()
    db.refresh(config)
    return serialize_ai_config(config, allow_runtime_env=is_admin(current_user))


@app.put("/api/v1/ai-configs/{config_id}", tags=["ai-configs"])
def update_ai_config(
    config_id: int,
    payload: AIConfigUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    config = get_ai_config_or_404(db, config_id)
    ensure_ai_config_access(config, current_user)

    updates = get_model_updates(payload)
    ensure_ai_env_var_permission(current_user, updates)
    target_scope = updates.get("scope", config.scope)
    target_user_id = updates["user_id"] if "user_id" in updates else config.user_id
    target_book_id = updates["book_id"] if "book_id" in updates else config.book_id
    if "base_url" in updates:
        updates["base_url"] = validate_ai_base_url_for_user(
            current_user,
            updates.get("base_url"),
            resolve_dns=False,
        )

    resolved_user_id, resolved_book_id = coerce_ai_scope_targets(
        db,
        scope=target_scope,
        user_id=target_user_id,
        book_id=target_book_id,
        current_user=current_user,
    )
    config.scope = target_scope
    config.user_id = resolved_user_id
    config.book_id = resolved_book_id

    for field in (
        "name",
        "module",
        "provider_name",
        "api_format",
        "base_url",
        "base_url_env_var",
        "api_key_env_var",
        "model_name",
        "model_name_env_var",
        "reasoning_effort",
        "temperature",
        "top_p",
        "max_tokens",
        "timeout_seconds",
        "priority",
        "is_enabled",
        "is_default",
        "system_prompt_template",
        "extra_headers",
        "extra_body",
        "notes",
    ):
        if field in updates:
            value = updates[field]
            if field == "name" and value is not None:
                value = value.strip()
            setattr(config, field, value)

    if updates.get("clear_api_key"):
        config.api_key = None
    elif "api_key" in updates:
        config.api_key = encrypt_secret(updates["api_key"])

    db.add(config)
    db.flush()
    unset_competing_defaults(db, config)
    db.commit()
    db.refresh(config)
    return serialize_ai_config(config, allow_runtime_env=is_admin(current_user))


@app.delete("/api/v1/ai-configs/{config_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["ai-configs"])
def delete_ai_config(
    config_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    config = get_ai_config_or_404(db, config_id)
    ensure_ai_config_access(config, current_user)
    db.delete(config)
    db.commit()


@app.get("/api/v1/books/{book_id}/ai/extract-world/jobs", tags=["ai"])
def list_world_extraction_jobs(
    book_id: int,
    limit: int = Query(default=10, ge=1, le=50),
    status_filter: Optional[WorldExtractionJobStatus] = Query(default=None, alias="status"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)

    stmt = select(WorldExtractionJob).where(WorldExtractionJob.book_id == book_id)
    if status_filter is not None:
        stmt = stmt.where(WorldExtractionJob.status == status_filter)
    stmt = stmt.order_by(WorldExtractionJob.created_at.desc(), WorldExtractionJob.id.desc()).limit(limit)

    jobs = db.execute(stmt).scalars().all()
    return {"items": [serialize_world_extraction_job(job) for job in jobs]}


@app.get("/api/v1/books/{book_id}/ai/extract-world/jobs/{job_id}", tags=["ai"])
def get_world_extraction_job(
    book_id: int,
    job_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    job = get_world_extraction_job_or_404(db, book_id, job_id)
    return serialize_world_extraction_job(job)


@app.post("/api/v1/books/{book_id}/ai/extract-world/jobs/{job_id}/cancel", tags=["ai"])
def cancel_world_extraction_job(
    book_id: int,
    job_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    job = get_world_extraction_job_or_404(db, book_id, job_id)

    if job_is_terminated(job):
        return serialize_world_extraction_job(job)

    options = dict(job.options_json or {})
    options["termination_reason"] = "user_cancelled"
    options["terminated_by_id"] = current_user.id

    if job.status == WorldExtractionJobStatus.PENDING:
        mark_job_terminated(job, message="提取任务在开始前已终止。")
        options.update(job.options_json or {})
        job.options_json = options
        db.add(job)
        db.commit()
        db.refresh(job)
        return serialize_world_extraction_job(job)

    if job.status != WorldExtractionJobStatus.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="只有排队中或进行中的提取任务才能终止。",
        )

    options["cancel_requested"] = True
    options["termination_requested_at"] = datetime.now(timezone.utc).isoformat()
    job.options_json = options
    job.message = "已收到终止请求，系统会在当前片段处理完后停止。"
    db.add(job)
    db.commit()
    db.refresh(job)
    return serialize_world_extraction_job(job)


@app.post("/api/v1/books/{book_id}/ai/extract-world/jobs/{job_id}/resume", status_code=status.HTTP_202_ACCEPTED, tags=["ai"])
def resume_world_extraction_job(
    book_id: int,
    job_id: int,
    payload: WorldExtractionJobResumeRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    ensure_world_extraction_capacity(db, current_user, book_id)
    job = get_world_extraction_job_or_404(db, book_id, job_id)

    if payload.failed_only:
        if not world_extraction_job_can_retry_failed(job):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="只有存在失败片段的已完成或失败任务，才能重试失败片段。",
            )
    elif not world_extraction_job_can_resume(job):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="只有失败且仍保留源数据的提取任务才能继续。",
        )

    previous_options = dict(job.options_json or {})
    cloned_options = {
        "origin": previous_options.get("origin") or (
            "uploaded_document" if job.source_type == WorldExtractionSource.IMPORTED_DOCUMENT else "internal_book"
        ),
        "manual_conflict_review": bool(previous_options.get("manual_conflict_review")),
        "resume_from_job_id": job.id,
        "skip_unchanged_chapters": bool(previous_options.get("skip_unchanged_chapters", True)),
    }
    if payload.failed_only:
        failed_segment_labels: list[str] = []
        failed_chapter_ids: list[int] = []
        for item in (job.result_payload or {}).get("errors") or []:
            if not isinstance(item, dict):
                continue
            label = str(item.get("segment_label") or "").strip()
            if label and label not in failed_segment_labels:
                failed_segment_labels.append(label)
            chapter_id = item.get("chapter_id")
            if isinstance(chapter_id, int) and chapter_id > 0 and chapter_id not in failed_chapter_ids:
                failed_chapter_ids.append(chapter_id)
        if not failed_segment_labels and not failed_chapter_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="当前任务没有可重试的失败片段。",
            )
        cloned_options.update(
            {
                "retry_failed_only": True,
                "failed_segment_labels": failed_segment_labels,
                "failed_chapter_ids": failed_chapter_ids,
                "retry_failed_from_job_id": job.id,
            }
        )
    if job.source_type == WorldExtractionSource.IMPORTED_DOCUMENT:
        stored_path = previous_options.get("stored_path")
        if not stored_path or not Path(str(stored_path)).exists():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="原始导入文档已不存在，无法继续该提取任务。",
            )
        cloned_options.update(
            {
                "stored_path": stored_path,
                "original_filename": previous_options.get("original_filename"),
                "content_type": previous_options.get("content_type"),
            }
        )

    resumed_job = WorldExtractionJob(
        book_id=job.book_id,
        created_by_id=current_user.id,
        source_type=job.source_type,
        source_name=job.source_name,
        status=WorldExtractionJobStatus.PENDING,
        conflict_strategy=job.conflict_strategy,
        update_world_bible=job.update_world_bible,
        chapter_scope=job.chapter_scope,
        segment_unit_limit=normalize_segment_unit_limit(job.segment_unit_limit),
        message="已排队重试上次失败片段。" if payload.failed_only else "已排队继续之前的提取任务。",
        options_json=cloned_options,
    )
    db.add(resumed_job)
    db.commit()
    db.refresh(resumed_job)

    previous_options["replaced_by_job_id"] = resumed_job.id
    previous_options["resumed_at"] = datetime.now(timezone.utc).isoformat()
    job.options_json = previous_options
    db.add(job)
    db.commit()

    if payload.delete_previous:
        cleanup_world_extraction_job_artifacts(db, job)
        db.delete(job)
        db.commit()

    launch_world_extraction_job(resumed_job.id)
    return serialize_world_extraction_job(resumed_job)


@app.delete("/api/v1/books/{book_id}/ai/extract-world/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["ai"])
def delete_world_extraction_job(
    book_id: int,
    job_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    job = get_world_extraction_job_or_404(db, book_id, job_id)

    if job.status in {WorldExtractionJobStatus.PENDING, WorldExtractionJobStatus.RUNNING} and not job_is_terminated(job):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请先终止正在运行的提取任务，再删除记录。",
        )

    cleanup_world_extraction_job_artifacts(db, job)
    db.delete(job)
    db.commit()


@app.post("/api/v1/books/{book_id}/ai/extract-world/jobs/{job_id}/resolve-conflict", tags=["ai"])
def resolve_world_job_conflict(
    book_id: int,
    job_id: int,
    payload: WorldConflictResolutionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    job = get_world_extraction_job_or_404(db, book_id, job_id)
    try:
        resolve_world_extraction_conflict(
            db,
            job=job,
            conflict_id=payload.conflict_id,
            decision=WorldConflictStrategy(payload.decision),
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    db.refresh(job)
    return serialize_world_extraction_job(job)


@app.post("/api/v1/books/{book_id}/ai/extract-world/jobs", status_code=status.HTTP_202_ACCEPTED, tags=["ai"])
def start_world_extraction_job(
    book_id: int,
    payload: WorldExtractionJobCreateRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    ensure_world_extraction_capacity(db, current_user, book_id)
    manual_conflict_review = payload.conflict_strategy == WorldConflictStrategy.MANUAL_REVIEW

    job = WorldExtractionJob(
        book_id=book_id,
        created_by_id=current_user.id,
        source_type=WorldExtractionSource.INTERNAL_BOOK,
        source_name=book.title,
        status=WorldExtractionJobStatus.PENDING,
        conflict_strategy=WorldConflictStrategy.MERGE if manual_conflict_review else payload.conflict_strategy,
        update_world_bible=payload.update_world_bible,
        chapter_scope=payload.chapter_scope,
        segment_unit_limit=normalize_segment_unit_limit(payload.segment_unit_limit),
        message="已进入提取队列。",
        options_json={
            "origin": "internal_book",
            "manual_conflict_review": manual_conflict_review,
            "skip_unchanged_chapters": payload.skip_unchanged_chapters,
        },
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    launch_world_extraction_job(job.id)
    return serialize_world_extraction_job(job)


@app.post("/api/v1/books/{book_id}/ai/extract-world/jobs/import", status_code=status.HTTP_202_ACCEPTED, tags=["ai"])
def start_world_import_job(
    book_id: int,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    conflict_strategy: WorldConflictStrategy = Form(default=WorldConflictStrategy.MERGE),
    update_world_bible: bool = Form(default=True),
    segment_unit_limit: int = Form(default=36000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)
    ensure_world_extraction_capacity(db, current_user, book_id)
    manual_conflict_review = conflict_strategy == WorldConflictStrategy.MANUAL_REVIEW

    job = WorldExtractionJob(
        book_id=book_id,
        created_by_id=current_user.id,
        source_type=WorldExtractionSource.IMPORTED_DOCUMENT,
        source_name=file.filename,
        status=WorldExtractionJobStatus.PENDING,
        conflict_strategy=WorldConflictStrategy.MERGE if manual_conflict_review else conflict_strategy,
        update_world_bible=update_world_bible,
        segment_unit_limit=normalize_segment_unit_limit(segment_unit_limit),
        message="文件已上传，等待开始提取。",
        options_json={
            "origin": "uploaded_document",
            "manual_conflict_review": manual_conflict_review,
        },
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    try:
        save_world_import_upload(job, file)
        db.add(job)
        db.commit()
        db.refresh(job)
    except Exception:
        db.delete(job)
        db.commit()
        raise
    finally:
        file.file.close()

    launch_world_extraction_job(job.id)
    return serialize_world_extraction_job(job)


@app.post("/api/v1/books/{book_id}/ai/extract-world/import-estimate", tags=["ai"])
def estimate_world_import_job(
    book_id: int,
    file: UploadFile = File(...),
    update_world_bible: bool = Form(default=True),
    segment_unit_limit: int = Form(default=36000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    book = get_book_or_404(db, book_id)
    ensure_book_http_access(book, current_user)

    temp_path: Optional[Path] = None
    try:
        temp_path, total_bytes, filename = save_temporary_world_import_upload(file)
        estimate = estimate_import_document(
            temp_path,
            source_name=filename,
            segment_unit_limit=segment_unit_limit,
            update_world_bible=update_world_bible,
        )
        estimate["file_size_bytes"] = total_bytes
        return estimate
    finally:
        file.file.close()
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


@app.post("/api/v1/books/{book_id}/chapters/{chapter_id}/ai/extract-world", tags=["ai"])
def extract_world_from_chapter(
    book_id: int,
    chapter_id: int,
    payload: AIWorldExtractionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        book, chapter = get_book_and_chapter(db, book_id, chapter_id)
        ensure_book_access(book, current_user)
        if payload.dry_run:
            return legacy_run_world_extraction(
                db,
                book=book,
                chapter=chapter,
                current_user=current_user,
                dry_run=True,
                update_world_bible=payload.update_world_bible,
            )
        character_config = resolve_ai_config_with_fallback(
            db,
            [
                AIModule.CHARACTER_EXTRACTION,
                AIModule.SETTING_EXTRACTION,
                AIModule.SUMMARY,
                AIModule.CO_WRITING,
            ],
            current_user,
            book,
        )
        relation_config = resolve_ai_config_with_fallback(
            db,
            [
                AIModule.RELATION_EXTRACTION,
                AIModule.CHARACTER_EXTRACTION,
                AIModule.SETTING_EXTRACTION,
                AIModule.SUMMARY,
                AIModule.CO_WRITING,
            ],
            current_user,
            book,
        )
        segment_payload = build_chapter_extraction_segment(chapter)
        result = run_segment_world_extraction(
            db,
            book=book,
            current_user=current_user,
            segment=ExtractionSegment(**segment_payload),
            character_config=character_config,
            relation_config=relation_config,
            conflict_strategy=WorldConflictStrategy.MERGE,
            update_world_bible=payload.update_world_bible,
        )
        return {
            "dry_run": payload.dry_run,
            "chapter_id": chapter.id,
            "chapter_title": chapter.title,
            "character_config": character_config.public_dict(),
            "relation_config": relation_config.public_dict(),
            **result,
        }
    except Exception as exc:
        _raise_ai_http_error(exc)


@app.post("/api/v1/books/{book_id}/ai/extract-world", tags=["ai"])
def extract_world_from_book(
    book_id: int,
    payload: AIBookWorldExtractionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        book = db.get(Book, book_id)
        if book is None:
            raise ResourceNotFoundError(f"Book `{book_id}` not found.")
        ensure_book_access(book, current_user)

        selected_chapters, skipped_chapters = select_book_chapters_for_world_extraction(
            db,
            book_id,
            chapter_scope=payload.chapter_scope,
        )

        if selected_chapters:
            resolve_ai_config_with_fallback(
                db,
                [
                    AIModule.CHARACTER_EXTRACTION,
                    AIModule.SETTING_EXTRACTION,
                    AIModule.SUMMARY,
                    AIModule.CO_WRITING,
                ],
                current_user,
                book,
            )
            resolve_ai_config_with_fallback(
                db,
                [
                    AIModule.RELATION_EXTRACTION,
                    AIModule.CHARACTER_EXTRACTION,
                    AIModule.SETTING_EXTRACTION,
                    AIModule.SUMMARY,
                    AIModule.CO_WRITING,
                ],
                current_user,
                book,
            )

        totals = {
            "created_character_count": 0,
            "updated_character_count": 0,
            "created_relation_count": 0,
            "updated_relation_count": 0,
            "world_facts_count": 0,
            "world_facts_appended_count": 0,
        }
        items: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        for chapter_item in selected_chapters:
            chapter = db.get(Chapter, chapter_item["chapter_id"])
            book = db.get(Book, book_id)
            if chapter is None or book is None:
                db.rollback()
                errors.append(
                    {
                        "chapter_id": chapter_item["chapter_id"],
                        "chapter_title": chapter_item["chapter_title"],
                        "detail": "Chapter or book was not available during batch extraction.",
                    }
                )
                continue

            try:
                if payload.dry_run:
                    result = legacy_run_world_extraction(
                        db,
                        book=book,
                        chapter=chapter,
                        current_user=current_user,
                        dry_run=True,
                        update_world_bible=payload.update_world_bible,
                    )
                    item = {
                        "chapter_id": result["chapter_id"],
                        "chapter_title": result["chapter_title"],
                        "created_character_count": result["created_character_count"],
                        "updated_character_count": result["updated_character_count"],
                        "created_relation_count": result["created_relation_count"],
                        "updated_relation_count": result["updated_relation_count"],
                        "world_facts_count": result["world_facts_count"],
                        "world_facts_appended_count": result["world_facts_appended_count"],
                    }
                    items.append(item)
                    for key in totals:
                        totals[key] += item.get(key, 0)
                    continue
                character_config = resolve_ai_config_with_fallback(
                    db,
                    [
                        AIModule.CHARACTER_EXTRACTION,
                        AIModule.SETTING_EXTRACTION,
                        AIModule.SUMMARY,
                        AIModule.CO_WRITING,
                    ],
                    current_user,
                    book,
                )
                relation_config = resolve_ai_config_with_fallback(
                    db,
                    [
                        AIModule.RELATION_EXTRACTION,
                        AIModule.CHARACTER_EXTRACTION,
                        AIModule.SETTING_EXTRACTION,
                        AIModule.SUMMARY,
                        AIModule.CO_WRITING,
                    ],
                    current_user,
                    book,
                )
                segment_payload = build_chapter_extraction_segment(chapter)
                result = run_segment_world_extraction(
                    db,
                    book=book,
                    current_user=current_user,
                    segment=ExtractionSegment(**segment_payload),
                    character_config=character_config,
                    relation_config=relation_config,
                    conflict_strategy=WorldConflictStrategy.MERGE,
                    update_world_bible=payload.update_world_bible,
                )
                item = {
                    "chapter_id": chapter.id,
                    "chapter_title": chapter.title,
                    "created_character_count": result["created_character_count"],
                    "updated_character_count": result["updated_character_count"],
                    "created_relation_count": result["created_relation_count"],
                    "updated_relation_count": result["updated_relation_count"],
                    "world_facts_count": len(result["world_facts"]),
                    "world_facts_appended_count": len(result["world_facts_appended"]),
                }
                items.append(item)

                totals["created_character_count"] += item["created_character_count"]
                totals["updated_character_count"] += item["updated_character_count"]
                totals["created_relation_count"] += item["created_relation_count"]
                totals["updated_relation_count"] += item["updated_relation_count"]
                totals["world_facts_count"] += item["world_facts_count"]
                totals["world_facts_appended_count"] += item["world_facts_appended_count"]
            except Exception as exc:
                db.rollback()
                error_item = {
                    "chapter_id": chapter_item["chapter_id"],
                    "chapter_title": chapter_item["chapter_title"],
                    "detail": str(exc),
                }
                items.append({**error_item, "error": True})
                errors.append(error_item)

        return {
            "dry_run": payload.dry_run,
            "book_id": book_id,
            "book_title": book.title,
            "chapter_scope": payload.chapter_scope,
            "selected_chapter_count": len(selected_chapters),
            "skipped_chapter_count": len(skipped_chapters),
            "processed_chapter_count": len(selected_chapters) - len(errors),
            "failed_chapter_count": len(errors),
            "skipped_chapters": skipped_chapters,
            "errors": errors,
            "totals": totals,
            "items": items,
        }
    except Exception as exc:
        _raise_ai_http_error(exc)


@app.post("/api/v1/assistant/chat", tags=["ai"])
def assistant_chat(
    payload: AssistantChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        book = get_book_or_404(db, payload.book_id)
        ensure_book_http_access(book, current_user)

        chapter: Optional[Chapter] = None
        if payload.chapter_id is not None:
            chapter = get_chapter_or_404(db, book.id, payload.chapter_id)

        message = str(payload.message or "").strip()
        if not message:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Assistant message cannot be empty.",
            )

        matched_resolution = _assistant_match_conflict_resolution(message)
        if matched_resolution is not None:
            decision, label = matched_resolution
            job, pending_conflicts = _assistant_find_latest_pending_conflict_job(db, book_id=book.id)
            if job is None or not pending_conflicts:
                return {
                    "reply": "当前这本书没有待处理的人物卡或关系冲突。",
                    "book": {"id": book.id, "title": book.title},
                    "action": {
                        "type": "world_conflict_resolution",
                        "decision": decision.value,
                        "resolved_count": 0,
                        "should_reload": False,
                    },
                }

            resolved_count = 0
            for item in list(pending_conflicts):
                resolve_world_extraction_conflict(
                    db,
                    job=job,
                    conflict_id=str(item.get("id")),
                    decision=decision,
                )
                resolved_count += 1

            return {
                "reply": (
                    f"已处理任务 #{job.id} 的 {resolved_count} 条待确认冲突，当前已按“{label}”执行。"
                ),
                "book": {"id": book.id, "title": book.title},
                "action": {
                    "type": "world_conflict_resolution",
                    "decision": decision.value,
                    "job_id": job.id,
                    "resolved_count": resolved_count,
                    "should_reload": True,
                },
            }

        mode: Literal["general", "trend"] = "trend" if _assistant_is_trend_request(message) else "general"
        reply, config_info, edit_proposal = _assistant_run_book_chat(
            db,
            current_user=current_user,
            book=book,
            chapter=chapter,
            page=payload.page,
            message=message,
            history=payload.history,
            mode=mode,
            selected_character_ids=payload.selected_character_ids,
            selected_chapter_ids=payload.selected_chapter_ids,
            custom_prompt=payload.custom_prompt,
            current_chapter_title=payload.current_chapter_title,
            current_chapter_summary=payload.current_chapter_summary,
            current_chapter_outline=payload.current_chapter_outline,
            current_chapter_content=payload.current_chapter_content,
        )
        return {
            "reply": reply,
            "book": {"id": book.id, "title": book.title},
            "chapter": (
                {"id": chapter.id, "title": chapter.title}
                if chapter is not None
                else None
            ),
            "action": {
                "type": "chat",
                "mode": mode,
                "should_reload": False,
            },
            "ai_config": config_info,
            "edit_proposal": edit_proposal,
        }
    except HTTPException:
        raise
    except Exception as exc:
        _raise_ai_http_error(exc)


@app.post("/api/v1/assistant/chat-stream", tags=["ai"])
def assistant_chat_stream(
    payload: AssistantChatRequest,
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    current_user_id = current_user.id

    def event_stream():
        stream_db = SessionLocal()
        try:
            stream_user = stream_db.get(User, current_user_id)
            if stream_user is None:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Current user was not found.")

            book = get_book_or_404(stream_db, payload.book_id)
            ensure_book_http_access(book, stream_user)

            chapter: Optional[Chapter] = None
            if payload.chapter_id is not None:
                chapter = get_chapter_or_404(stream_db, book.id, payload.chapter_id)

            message = str(payload.message or "").strip()
            if not message:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Assistant message cannot be empty.",
                )

            matched_resolution = _assistant_match_conflict_resolution(message)
            if matched_resolution is not None:
                decision, label = matched_resolution
                job, pending_conflicts = _assistant_find_latest_pending_conflict_job(stream_db, book_id=book.id)
                if job is None or not pending_conflicts:
                    yield json.dumps(
                        {
                            "type": "final",
                            "response": {
                                "reply": "当前这本书没有待处理的人物卡或关系冲突。",
                                "book": {"id": book.id, "title": book.title},
                                "action": {
                                    "type": "world_conflict_resolution",
                                    "decision": decision.value,
                                    "resolved_count": 0,
                                    "should_reload": False,
                                },
                                "edit_proposal": None,
                            },
                        },
                        ensure_ascii=False,
                    ) + "\n"
                    return

                resolved_count = 0
                for item in list(pending_conflicts):
                    resolve_world_extraction_conflict(
                        stream_db,
                        job=job,
                        conflict_id=str(item.get("id")),
                        decision=decision,
                    )
                    resolved_count += 1

                yield json.dumps(
                    {
                        "type": "final",
                        "response": {
                            "reply": f"已处理任务 #{job.id} 的 {resolved_count} 条待确认冲突，当前已按“{label}”执行。",
                            "book": {"id": book.id, "title": book.title},
                            "action": {
                                "type": "world_conflict_resolution",
                                "decision": decision.value,
                                "job_id": job.id,
                                "resolved_count": resolved_count,
                                "should_reload": True,
                            },
                            "edit_proposal": None,
                        },
                    },
                    ensure_ascii=False,
                ) + "\n"
                return

            mode: Literal["general", "trend"] = "trend" if _assistant_is_trend_request(message) else "general"
            for event in _assistant_stream_book_chat_events(
                stream_db,
                current_user=stream_user,
                book=book,
                chapter=chapter,
                page=payload.page,
                message=message,
                history=payload.history,
                mode=mode,
                selected_character_ids=payload.selected_character_ids,
                selected_chapter_ids=payload.selected_chapter_ids,
                custom_prompt=payload.custom_prompt,
                current_chapter_title=payload.current_chapter_title,
                current_chapter_summary=payload.current_chapter_summary,
                current_chapter_outline=payload.current_chapter_outline,
                current_chapter_content=payload.current_chapter_content,
            ):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail, ensure_ascii=False)
            yield json.dumps({"type": "error", "message": detail}, ensure_ascii=False) + "\n"
        except Exception as exc:
            logger.exception(
                "assistant_chat_stream_failed user_id=%s book_id=%s chapter_id=%s page=%s",
                current_user_id,
                payload.book_id,
                payload.chapter_id,
                payload.page,
            )
            message = str(exc) if isinstance(exc, AIInvocationError) else "AI 助手流式输出失败。"
            yield json.dumps({"type": "error", "message": message}, ensure_ascii=False) + "\n"
        finally:
            stream_db.close()

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/v1/books/{book_id}/chapters/{chapter_id}/ai/context-preview", tags=["ai"])
def preview_ai_context(
    book_id: int,
    chapter_id: int,
    payload: AIContextRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        book, chapter = get_book_and_chapter(db, book_id, chapter_id)
        ensure_book_access(book, current_user)
        return run_generation(
            db,
            book=book,
            chapter=chapter,
            current_user=current_user,
            module=payload.module,
            user_prompt=payload.user_prompt,
            target_field=payload.target_field,
            apply_mode=payload.apply_mode,
            target_units=payload.target_units,
            previous_chapters=payload.previous_chapters,
            character_limit=payload.character_limit,
            system_prompt_override=payload.system_prompt_override,
            chunk_size=max(payload.target_units or 900, 200),
            use_reasoner_planning=False,
            dry_run=True,
            store_snapshot=False,
        )
    except Exception as exc:
        _raise_ai_http_error(exc)


@app.post("/api/v1/books/{book_id}/chapters/{chapter_id}/ai/generate", tags=["ai"])
def generate_ai_content(
    book_id: int,
    chapter_id: int,
    payload: AIGenerationRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        book, chapter = get_book_and_chapter(db, book_id, chapter_id)
        ensure_book_access(book, current_user)
        return run_generation(
            db,
            book=book,
            chapter=chapter,
            current_user=current_user,
            module=payload.module,
            user_prompt=payload.user_prompt,
            target_field=payload.target_field,
            apply_mode=payload.apply_mode,
            target_units=payload.target_units,
            previous_chapters=payload.previous_chapters,
            character_limit=payload.character_limit,
            system_prompt_override=payload.system_prompt_override,
            chunk_size=payload.chunk_size,
            use_reasoner_planning=payload.use_reasoner_planning,
            dry_run=payload.dry_run,
            store_snapshot=payload.store_snapshot,
        )
    except Exception as exc:
        _raise_ai_http_error(exc)


@app.post("/api/v1/books/{book_id}/chapters/{chapter_id}/ai/generate-draft", tags=["ai"])
def generate_ai_draft(
    book_id: int,
    chapter_id: int,
    payload: AIGenerationRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        book, chapter = get_book_and_chapter(db, book_id, chapter_id)
        ensure_book_access(book, current_user)
        result = run_generation(
            db,
            book=book,
            chapter=chapter,
            current_user=current_user,
            module=payload.module,
            user_prompt=payload.user_prompt,
            target_field=payload.target_field,
            apply_mode=payload.apply_mode,
            target_units=payload.target_units,
            previous_chapters=payload.previous_chapters,
            character_limit=payload.character_limit,
            system_prompt_override=payload.system_prompt_override,
            chunk_size=payload.chunk_size,
            use_reasoner_planning=payload.use_reasoner_planning,
            dry_run=False,
            store_snapshot=False,
            apply_result=False,
            enforce_target_units=False,
        )
        generated_text = str(result.get("generated_text") or "").strip()
        if generated_text:
            store_latest_ai_draft_text(db, chapter, generated_text)
        return result
    except Exception as exc:
        _raise_ai_http_error(exc)


@app.post("/api/v1/books/{book_id}/chapters/{chapter_id}/ai/generate-draft-stream", tags=["ai"])
def generate_ai_draft_stream(
    book_id: int,
    chapter_id: int,
    payload: AIGenerationRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    try:
        book, chapter = get_book_and_chapter(db, book_id, chapter_id)
        ensure_book_access(book, current_user)
    except Exception as exc:
        _raise_ai_http_error(exc)

    current_user_id = current_user.id
    module = payload.module
    user_prompt = payload.user_prompt
    target_field = payload.target_field
    apply_mode = payload.apply_mode
    target_units = payload.target_units
    previous_chapters = payload.previous_chapters
    character_limit = payload.character_limit
    system_prompt_override = payload.system_prompt_override
    chunk_size = payload.chunk_size
    use_reasoner_planning = payload.use_reasoner_planning

    def iter_events():
        stream_db = SessionLocal()
        try:
            stream_user = stream_db.get(User, current_user_id)
            if stream_user is None:
                raise AccessDeniedError("Current user is no longer available.")

            stream_book, stream_chapter = get_book_and_chapter(stream_db, book_id, chapter_id)
            ensure_book_access(stream_book, stream_user)

            for event in stream_generation_draft_events(
                stream_db,
                book=stream_book,
                chapter=stream_chapter,
                current_user=stream_user,
                module=module,
                user_prompt=user_prompt,
                target_field=target_field,
                apply_mode=apply_mode,
                target_units=target_units,
                previous_chapters=previous_chapters,
                character_limit=character_limit,
                system_prompt_override=system_prompt_override,
                chunk_size=chunk_size,
                use_reasoner_planning=use_reasoner_planning,
            ):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as exc:
            try:
                _raise_ai_http_error(exc)
            except HTTPException as http_exc:
                message = str(http_exc.detail)
            else:
                message = str(exc)
            yield json.dumps({"type": "error", "message": message}, ensure_ascii=False) + "\n"
        finally:
            stream_db.close()

    return StreamingResponse(
        iter_events(),
        media_type="application/x-ndjson; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/v1/books/{book_id}/chapters/{chapter_id}/ai/refine-draft", tags=["ai"])
def refine_ai_draft(
    book_id: int,
    chapter_id: int,
    payload: AIDraftRefineRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        book, chapter = get_book_and_chapter(db, book_id, chapter_id)
        ensure_book_access(book, current_user)
        result = refine_generation_draft(
            db,
            book=book,
            chapter=chapter,
            current_user=current_user,
            module=payload.module,
            user_prompt=payload.user_prompt,
            target_field=payload.target_field,
            apply_mode=payload.apply_mode,
            target_units=payload.target_units,
            previous_chapters=payload.previous_chapters,
            character_limit=payload.character_limit,
            system_prompt_override=payload.system_prompt_override,
            planning_text=payload.planning_text,
            draft_text=payload.draft_text,
            adjustment_mode=payload.adjustment_mode,
        )
        generated_text = str(result.get("generated_text") or "").strip()
        if generated_text:
            store_latest_ai_draft_text(db, chapter, generated_text)
        return result
    except Exception as exc:
        _raise_ai_http_error(exc)


@app.post("/api/v1/books/{book_id}/chapters/{chapter_id}/ai/apply-draft", tags=["ai"])
def apply_ai_draft(
    book_id: int,
    chapter_id: int,
    payload: AIDraftApplyRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        book, chapter = get_book_and_chapter(db, book_id, chapter_id)
        ensure_book_access(book, current_user)
        return apply_generation_draft(
            db,
            book=book,
            chapter=chapter,
            current_user=current_user,
            module=payload.module,
            user_prompt=payload.user_prompt,
            target_field=payload.target_field,
            apply_mode=payload.apply_mode,
            target_units=payload.target_units,
            previous_chapters=payload.previous_chapters,
            character_limit=payload.character_limit,
            system_prompt_override=payload.system_prompt_override,
            planning_text=payload.planning_text,
            generated_text=payload.generated_text,
            store_snapshot=payload.store_snapshot,
        )
    except Exception as exc:
        _raise_ai_http_error(exc)
