from __future__ import annotations

import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Iterator, Literal, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from character_cards import (
    CHARACTER_CARD_TEXT_FIELDS,
    merge_character_card_json,
    normalize_character_timeline_entries,
    timeline_entries_up_to_chapter,
)
from memory_service import build_deepseek_memory_prompt, resolve_style_anchor, retrieve_dynamic_context
from network_security import UnsafeOutboundURLError, validate_outbound_base_url
from secret_storage import SecretStorageError, decrypt_secret
from models import AIConfig, AIModule, AIScope, Book, Chapter, ChapterNodeType, Character, Relation, Snapshot, SnapshotKind, User, UserRole


TargetField = Literal["content", "outline", "summary"]
ApplyMode = Literal["append", "replace"]
logger = logging.getLogger("bamboo_ai.ai_service")

_GENERATION_CONFIG_FALLBACKS: dict[AIModule, list[AIModule]] = {
    AIModule.CO_WRITING: [
        AIModule.OUTLINE_EXPANSION,
        AIModule.SUMMARY,
        AIModule.CHARACTER_EXTRACTION,
        AIModule.SETTING_EXTRACTION,
        AIModule.RELATION_EXTRACTION,
    ],
    AIModule.OUTLINE_EXPANSION: [
        AIModule.CO_WRITING,
        AIModule.SUMMARY,
        AIModule.CHARACTER_EXTRACTION,
        AIModule.SETTING_EXTRACTION,
        AIModule.RELATION_EXTRACTION,
    ],
    AIModule.SUMMARY: [
        AIModule.CO_WRITING,
        AIModule.OUTLINE_EXPANSION,
        AIModule.CHARACTER_EXTRACTION,
        AIModule.SETTING_EXTRACTION,
        AIModule.RELATION_EXTRACTION,
    ],
}


class AIServiceError(RuntimeError):
    pass


class ResourceNotFoundError(AIServiceError):
    pass


class AccessDeniedError(AIServiceError):
    pass


class AIConfigNotFoundError(AIServiceError):
    pass


class AIInvocationError(AIServiceError):
    pass


_MODEL_METADATA_CACHE: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_MODEL_METADATA_CACHE_TTL_SECONDS = 300
_OPENAI_COMPAT_MAX_RETRIES = max(1, int(os.getenv("OPENAI_COMPAT_MAX_RETRIES", "3")))
_WORLD_FACT_MAX_LENGTH = max(24, int(os.getenv("WORLD_FACT_MAX_LENGTH", "80")))
_WORLD_FACT_MAX_ITEMS = max(100, int(os.getenv("WORLD_FACT_MAX_ITEMS", "1200")))
_WORLD_FACT_SIMILARITY_THRESHOLD = 0.9
_RELATION_DESCRIPTION_MAX_LENGTH = max(48, int(os.getenv("RELATION_DESCRIPTION_MAX_LENGTH", "180")))
_RELATION_DESCRIPTION_PREVIEW_LENGTH = max(
    24,
    min(
        _RELATION_DESCRIPTION_MAX_LENGTH,
        int(os.getenv("RELATION_DESCRIPTION_PREVIEW_LENGTH", "72")),
    ),
)


@dataclass
class ResolvedAIConfig:
    id: Optional[int]
    name: str
    module: AIModule
    source: str
    scope: str
    provider_name: Optional[str]
    api_format: str
    base_url: str
    api_key: Optional[str]
    model_name: str
    timeout_seconds: int
    temperature: Optional[float]
    top_p: Optional[float]
    max_tokens: Optional[int]
    reasoning_effort: Optional[str]
    system_prompt_template: Optional[str]
    extra_headers: dict[str, Any]
    extra_body: dict[str, Any]

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "module": self.module.value,
            "source": self.source,
            "scope": self.scope,
            "provider_name": self.provider_name,
            "api_format": self.api_format,
            "base_url": self.base_url,
            "model_name": self.model_name,
            "timeout_seconds": self.timeout_seconds,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "reasoning_effort": self.reasoning_effort,
        }


@dataclass
class PromptContext:
    system_prompt: str
    user_prompt: str
    messages: list[dict[str, str]]
    related_characters: list[dict[str, Any]]
    previous_chapters: list[dict[str, Any]]
    context_sections: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "system_prompt": self.system_prompt,
            "user_prompt": self.user_prompt,
            "messages": self.messages,
            "related_characters": self.related_characters,
            "previous_chapters": self.previous_chapters,
            "context_sections": self.context_sections,
        }


def estimate_text_units(text: str | None) -> int:
    if not text:
        return 0

    cjk_count = sum(
        1
        for char in text
        if (
            "\u4e00" <= char <= "\u9fff"
            or "\u3400" <= char <= "\u4dbf"
            or "\u3040" <= char <= "\u30ff"
            or "\uac00" <= char <= "\ud7a3"
        )
    )
    latin_words = len(re.findall(r"[A-Za-z0-9_'-]+", text))
    return cjk_count + latin_words


def _is_cjk_char(char: str) -> bool:
    return (
        "\u4e00" <= char <= "\u9fff"
        or "\u3400" <= char <= "\u4dbf"
        or "\u3040" <= char <= "\u30ff"
        or "\uac00" <= char <= "\ud7a3"
    )


_TEXT_UNIT_PATTERN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\uac00-\ud7a3]|[A-Za-z0-9_'-]+")
_TRAILING_PUNCTUATION = set(" \t\r\n，。！？；：、,.!?;:…\"'”’）)]】》」』")

_WRITING_QUALITY_RULES = (
    "[写作质检]\n"
    "- 禁止总结主题、升华意义、替读者下结论，也不要提前展望未来。\n"
    "- 禁止出现“等待你的回答”“你打算怎么做”这类旁白式引导或催促语。\n"
    "- 不要把设定、人物卡、长期摘要或大纲原文直接抄进正文；必须转译成场景、动作、感官、对白和人物反应。\n"
    "- 情绪优先通过动作、视线、停顿、呼吸、环境噪点、衣物与空间关系显影，少用空泛抽象词。\n"
    "- 角色必须带着欲望、顾虑、立场和代价行动，不要把人物写成只为推动剧情出现的工具。\n"
    "- 人物关系靠试探、互动、误解、照顾、利益交换和共同经历递进，不要突然跳级。\n"
    "- 环境不是背景板，要通过天气、光线、噪音、空间阻力和物件位置参与叙事。\n"
    "- 避免油腻腔、套路霸总腔、廉价深情、夸张副词堆叠和空洞比喻。\n"
    "- 避免网文套话、套路反应、过度鸡汤和作者替角色发言。\n"
    "- 多人同场时保持群像流动，不要让所有角色都只围着主角转。\n"
    "- 严守当前可感知的信息边界，不要替角色做上帝视角解读。\n"
)

_SUMMARY_QUALITY_RULES = (
    "[摘要质检]\n"
    "- 只记录已发生且可确认的事实，不脑补动机，不预判未来。\n"
    "- 以事件推进、关键动作、冲突变化和人物状态为主，不写空泛抒情。\n"
    "- 保留关键台词、关键决定和不可逆变化，避免流水账。\n"
    "- 优先提炼转折、后果、关系变化和信息增量，不要机械复述每一段正文。\n"
)

_WORLD_EXTRACTION_QUALITY_RULES = (
    "[提取质检]\n"
    "- 只提取文本中明确支持的稳定信息，禁止猜测、脑补或情绪化总结。\n"
    "- 人物优先主角、重要配角和持续出现者；路人或一次性角色可省略或标为 minor。\n"
    "- 人物描述抓稳定身份、行为特征、目标和关系线索，不要写章节复述。\n"
    "- 世界事实只保留耐久设定、人物/组织/地点规则与背景事实，避免临时动作和对白复述。\n"
    "- 能合并同名、别名或明显同一人的信息时优先合并，避免重复卡片。\n"
    "- 不要把一时情绪、临场行为或单次寒暄提炼成永久设定。\n"
)

_RELATION_EXTRACTION_QUALITY_RULES = (
    "[关系提取质检]\n"
    "- 只抽取有明确证据的关系，不做臆测配对或未来走向推断。\n"
    "- 优先稳定关系、长期冲突、阵营关系和清晰依赖/对立，不要把一次对话硬升格为关系。\n"
    "- 关系描述要简洁具体，尽量说明依据而不是空泛评价。\n"
    "- 关系变化必须能在文本中找到触发事件、互动模式或持续证据。\n"
)


def _safe_text(value: Optional[str], fallback: str = "暂无") -> str:
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _normalize_extra_data(extra_data: Optional[dict[str, Any]]) -> dict[str, Any]:
    return dict(extra_data) if isinstance(extra_data, dict) else {}


def store_latest_ai_draft_text(db: Session, chapter: Chapter, draft_text: str) -> None:
    normalized = str(draft_text or "").strip()
    extra_data = _normalize_extra_data(chapter.extra_data)
    if normalized:
        extra_data["last_ai_draft_text"] = normalized
    else:
        extra_data.pop("last_ai_draft_text", None)
    chapter.extra_data = extra_data
    db.add(chapter)
    db.commit()
    db.refresh(chapter)


def _shorten(text: str | None, max_chars: int) -> str:
    value = _safe_text(text, "")
    if len(value) <= max_chars:
        return value
    return f"{value[:max_chars].rstrip()}..."


def _is_deepseek_config(config: Optional[ResolvedAIConfig]) -> bool:
    if config is None:
        return False
    candidates = [
        str(config.provider_name or ""),
        str(config.model_name or ""),
        str(config.base_url or ""),
    ]
    return any("deepseek" in item.lower() for item in candidates if item)


def _resolve_effective_chapter_sequence(all_chapters: list[Chapter], chapter: Chapter) -> int:
    if isinstance(chapter.sequence_number, int) and chapter.sequence_number > 0:
        return chapter.sequence_number

    ordered_chapters = sorted(
        all_chapters,
        key=lambda item: (
            item.sequence_number is None,
            item.sequence_number if item.sequence_number is not None else item.sort_order,
            item.sort_order,
            item.id or 0,
        ),
    )
    for index, item in enumerate(ordered_chapters, start=1):
        if item.id == chapter.id:
            return index
    return 1


def _should_use_deepseek_memory_prompt(
    *,
    config: Optional[ResolvedAIConfig],
    module: AIModule,
    target_field: TargetField,
    apply_mode: ApplyMode,
    current_existing_text: str,
    system_prompt_override: Optional[str],
) -> bool:
    if not _is_deepseek_config(config):
        return False
    if module not in {AIModule.CO_WRITING, AIModule.OUTLINE_EXPANSION}:
        return False
    if target_field != "content":
        return False
    return True


def _module_quality_sections(
    module: AIModule,
    target_field: TargetField,
) -> list[str]:
    sections: list[str] = []
    if module in {AIModule.CO_WRITING, AIModule.OUTLINE_EXPANSION} or target_field in {"content", "outline"}:
        sections.append(_WRITING_QUALITY_RULES)
    if module == AIModule.SUMMARY or target_field == "summary":
        sections.append(_SUMMARY_QUALITY_RULES)
    if module in {AIModule.SETTING_EXTRACTION, AIModule.CHARACTER_EXTRACTION}:
        sections.append(_WORLD_EXTRACTION_QUALITY_RULES)
    if module == AIModule.RELATION_EXTRACTION:
        sections.append(_RELATION_EXTRACTION_QUALITY_RULES)
    return sections


def _chapter_order_value(chapter: Chapter) -> tuple[int, int, int]:
    primary = chapter.sequence_number if chapter.sequence_number is not None else chapter.sort_order
    depth = chapter.depth if chapter.depth is not None else 0
    return (primary, depth, chapter.id)


def _chapter_ref_value(chapter: Chapter | None, fallback_id: int | None) -> int:
    if chapter is not None:
        return _chapter_order_value(chapter)[0]
    if fallback_id is not None:
        return fallback_id
    return 0


def _env_name_for_module(module: AIModule, suffix: str) -> str:
    return f"AI_{module.value.upper()}_{suffix}"


def _resolve_env(*names: Optional[str]) -> Optional[str]:
    for name in names:
        if not name:
            continue
        value = os.getenv(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _resolve_int_env(*names: Optional[str], default: int) -> int:
    value = _resolve_env(*names)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _normalize_json_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _resolve_candidate_config(
    config: AIConfig,
    module: AIModule,
) -> Optional[ResolvedAIConfig]:
    module_base_url_env = _env_name_for_module(module, "BASE_URL")
    module_api_key_env = _env_name_for_module(module, "API_KEY")
    module_model_env = _env_name_for_module(module, "MODEL_NAME")
    module_timeout_env = _env_name_for_module(module, "TIMEOUT_SECONDS")

    base_url = _resolve_env(
        config.base_url_env_var,
        module_base_url_env,
        "OPENAI_COMPAT_BASE_URL",
        "OPENAI_BASE_URL",
    ) or config.base_url
    api_key = _resolve_env(
        config.api_key_env_var,
        module_api_key_env,
        "OPENAI_COMPAT_API_KEY",
        "OPENAI_API_KEY",
    ) or decrypt_secret(config.api_key)
    model_name = _resolve_env(
        config.model_name_env_var,
        module_model_env,
        "OPENAI_COMPAT_MODEL_NAME",
        "OPENAI_MODEL_NAME",
    ) or config.model_name

    if not base_url or not model_name:
        return None

    timeout_seconds = _resolve_int_env(
        module_timeout_env,
        "OPENAI_COMPAT_TIMEOUT_SECONDS",
        default=config.timeout_seconds or 120,
    )

    return ResolvedAIConfig(
        id=config.id,
        name=config.name,
        module=module,
        source="database",
        scope=config.scope.value,
        provider_name=config.provider_name,
        api_format=config.api_format,
        base_url=base_url,
        api_key=api_key,
        model_name=model_name,
        timeout_seconds=timeout_seconds,
        temperature=config.temperature,
        top_p=config.top_p,
        max_tokens=config.max_tokens,
        reasoning_effort=config.reasoning_effort,
        system_prompt_template=config.system_prompt_template,
        extra_headers=_normalize_json_dict(config.extra_headers),
        extra_body=_normalize_json_dict(config.extra_body),
    )


def _resolve_env_only_config(module: AIModule) -> Optional[ResolvedAIConfig]:
    base_url = _resolve_env(
        _env_name_for_module(module, "BASE_URL"),
        "OPENAI_COMPAT_BASE_URL",
        "OPENAI_BASE_URL",
    )
    model_name = _resolve_env(
        _env_name_for_module(module, "MODEL_NAME"),
        "OPENAI_COMPAT_MODEL_NAME",
        "OPENAI_MODEL_NAME",
    )
    if not base_url or not model_name:
        return None

    return ResolvedAIConfig(
        id=None,
        name=f"env-{module.value}",
        module=module,
        source="environment",
        scope="environment",
        provider_name="env",
        api_format="openai_v1",
        base_url=base_url,
        api_key=_resolve_env(
            _env_name_for_module(module, "API_KEY"),
            "OPENAI_COMPAT_API_KEY",
            "OPENAI_API_KEY",
        ),
        model_name=model_name,
        timeout_seconds=_resolve_int_env(
            _env_name_for_module(module, "TIMEOUT_SECONDS"),
            "OPENAI_COMPAT_TIMEOUT_SECONDS",
            default=120,
        ),
        temperature=None,
        top_p=None,
        max_tokens=None,
        reasoning_effort=_resolve_env(_env_name_for_module(module, "REASONING_EFFORT")),
        system_prompt_template=None,
        extra_headers={},
        extra_body={},
    )


def _is_safe_for_non_admin_user(config: ResolvedAIConfig) -> bool:
    try:
        validate_outbound_base_url(
            config.base_url,
            allow_private_network=False,
            resolve_dns=True,
        )
    except UnsafeOutboundURLError:
        logger.warning(
            "unsafe_ai_base_url_rejected config_id=%s scope=%s base_url=%s",
            config.id,
            config.scope,
            config.base_url,
        )
        return False
    return True


def resolve_ai_config(
    db: Session,
    module: AIModule,
    user: User,
    book: Book,
) -> ResolvedAIConfig:
    configs = db.execute(
        select(AIConfig).where(
            AIConfig.module == module,
            AIConfig.is_enabled.is_(True),
        )
    ).scalars().all()

    ranked: list[tuple[tuple[int, int, int, int], AIConfig]] = []
    for config in configs:
        if config.scope == AIScope.BOOK and config.book_id == book.id:
            scope_rank = 0
        elif config.scope == AIScope.USER and config.user_id == user.id:
            scope_rank = 1
        elif config.scope == AIScope.SYSTEM:
            scope_rank = 2
        else:
            continue

        ranked.append(
            (
                (
                    scope_rank,
                    0 if config.is_default else 1,
                    config.priority,
                    config.id,
                ),
                config,
            )
        )

    ranked.sort(key=lambda item: item[0])
    skipped_unsafe = False
    for _, config in ranked:
        try:
            resolved = _resolve_candidate_config(config, module)
        except SecretStorageError:
            logger.warning(
                "invalid_ai_config_secret config_id=%s module=%s",
                config.id,
                module.value,
            )
            skipped_unsafe = True
            continue
        if resolved is not None:
            if (
                user.role not in {UserRole.SUPER_ADMIN, UserRole.ADMIN}
                and config.scope in {AIScope.USER, AIScope.BOOK}
                and not _is_safe_for_non_admin_user(resolved)
            ):
                skipped_unsafe = True
                continue
            return resolved

    env_config = _resolve_env_only_config(module)
    if env_config is not None:
        return env_config

    if skipped_unsafe:
        raise AIConfigNotFoundError(
            f"No safe enabled AI config or environment fallback found for module `{module.value}`."
        )

    raise AIConfigNotFoundError(
        f"No enabled AI config or environment fallback found for module `{module.value}`."
    )


def resolve_ai_config_with_fallback(
    db: Session,
    modules: list[AIModule],
    user: User,
    book: Book,
) -> ResolvedAIConfig:
    errors: list[str] = []
    for module in modules:
        try:
            return resolve_ai_config(db, module, user, book)
        except AIConfigNotFoundError as exc:
            errors.append(str(exc))
    raise AIConfigNotFoundError(" / ".join(errors))


def _adapt_generation_fallback_config(
    requested_module: AIModule,
    resolved: ResolvedAIConfig,
) -> ResolvedAIConfig:
    if resolved.module == requested_module:
        return resolved
    return ResolvedAIConfig(
        id=resolved.id,
        name=resolved.name,
        module=requested_module,
        source=resolved.source,
        scope=resolved.scope,
        provider_name=resolved.provider_name,
        api_format=resolved.api_format,
        base_url=resolved.base_url,
        api_key=resolved.api_key,
        model_name=resolved.model_name,
        timeout_seconds=resolved.timeout_seconds,
        temperature=resolved.temperature,
        top_p=resolved.top_p,
        max_tokens=resolved.max_tokens,
        reasoning_effort=resolved.reasoning_effort,
        system_prompt_template=None,
        extra_headers=dict(resolved.extra_headers or {}),
        extra_body=dict(resolved.extra_body or {}),
    )


def _resolve_generation_config(
    db: Session,
    *,
    module: AIModule,
    current_user: User,
    book: Book,
) -> tuple[ResolvedAIConfig, Optional[AIModule]]:
    try:
        return resolve_ai_config(db, module, current_user, book), None
    except AIConfigNotFoundError:
        fallback_modules = _GENERATION_CONFIG_FALLBACKS.get(module) or []
        if not fallback_modules:
            raise
        fallback_resolved = resolve_ai_config_with_fallback(
            db,
            fallback_modules,
            current_user,
            book,
        )
        logger.warning(
            "generation_ai_config_fallback requested_module=%s fallback_module=%s config_id=%s model_name=%s",
            module.value,
            fallback_resolved.module.value,
            fallback_resolved.id,
            fallback_resolved.model_name,
        )
        return (
            _adapt_generation_fallback_config(module, fallback_resolved),
            fallback_resolved.module,
        )


def get_book_and_chapter(
    db: Session,
    book_id: int,
    chapter_id: int,
) -> tuple[Book, Chapter]:
    book = db.get(Book, book_id)
    if book is None:
        raise ResourceNotFoundError(f"Book `{book_id}` not found.")

    chapter = db.get(Chapter, chapter_id)
    if chapter is None or chapter.book_id != book_id:
        raise ResourceNotFoundError(
            f"Chapter `{chapter_id}` not found in book `{book_id}`."
        )

    return book, chapter


def ensure_book_access(book: Book, current_user: User) -> None:
    if current_user.role in {UserRole.SUPER_ADMIN, UserRole.ADMIN}:
        return
    if book.owner_id != current_user.id:
        raise AccessDeniedError("You do not have access to this book.")


def _serialize_character(character: Character, *, current_chapter_order: Optional[int] = None) -> dict[str, Any]:
    payload = merge_character_card_json(character.card_json)
    relevant_timeline = timeline_entries_up_to_chapter(
        payload.get("timeline_entries"),
        current_chapter_order,
        max_items=6,
    )
    latest_timeline = relevant_timeline[-1] if relevant_timeline else None
    base = {
        "id": character.id,
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
        "life_statuses": payload.get("life_statuses", []),
        "timeline_entries": relevant_timeline,
        "latest_timeline_entry": latest_timeline,
        "current_location": latest_timeline.get("location") if latest_timeline else None,
        "current_status": latest_timeline.get("status") if latest_timeline else None,
        "current_focus": latest_timeline.get("event") if latest_timeline else None,
    }
    for key in CHARACTER_CARD_TEXT_FIELDS:
        base[key] = payload.get(key)
    if payload:
        merged = dict(payload)
        merged.update({key: value for key, value in base.items() if value not in (None, [], "")})
        return merged
    return base


def _select_related_characters(
    characters: list[Character],
    chapter_map: dict[int, Chapter],
    current_chapter: Chapter,
    limit: Optional[int],
) -> list[dict[str, Any]]:
    current_order = _chapter_order_value(current_chapter)[0]
    selected: list[Character] = []

    for character in characters:
        if not character.is_active:
            continue
        first_ref = _chapter_ref_value(
            chapter_map.get(character.first_appearance_chapter_id),
            character.first_appearance_chapter_id,
        )
        last_ref = _chapter_ref_value(
            chapter_map.get(character.last_appearance_chapter_id),
            character.last_appearance_chapter_id,
        )
        if first_ref and first_ref > current_order:
            continue
        selected.append(character)

    if not selected:
        selected = [character for character in characters if character.is_active]

    selected.sort(key=lambda item: (item.role_label or "", item.name))
    payloads = [
        _serialize_character(item, current_chapter_order=current_order)
        for item in selected
    ]
    if limit is None or int(limit or 0) <= 0:
        return payloads
    return payloads[:limit]


def _character_match_score(character_payload: dict[str, Any], context_text: str) -> int:
    text = str(context_text or "").strip()
    if not text:
        return 0

    score = 0
    name = _safe_text(character_payload.get("name"))
    if name:
        score += text.count(name) * 12

    aliases = character_payload.get("aliases")
    if isinstance(aliases, list):
        for alias in aliases:
            alias_text = _safe_text(alias)
            if alias_text:
                score += text.count(alias_text) * 8

    seen_text_fields: set[str] = set()
    for key, weight in (
        ("role_label", 4),
        ("current_location", 4),
        ("current_status", 4),
        ("current_focus", 5),
        ("short_term_goal", 3),
        ("long_term_goal", 3),
        ("motivation", 3),
        ("personality", 2),
        ("appearance", 2),
        ("weakness", 2),
        ("biography", 2),
        ("description", 2),
    ):
        value = _safe_text(character_payload.get(key))
        if not value or value in seen_text_fields:
            continue
        seen_text_fields.add(value)
        if value in text:
            score += weight

    latest_timeline = character_payload.get("latest_timeline_entry")
    if isinstance(latest_timeline, dict):
        for key in ("event", "location", "status", "notes"):
            value = _safe_text(latest_timeline.get(key))
            if value and value in text:
                score += 3

    return score


def _select_memory_character_payloads(
    character_payloads: list[dict[str, Any]],
    *,
    current_outline: str,
    dynamic_memory: Any,
    limit: int,
) -> list[dict[str, Any]]:
    resolved_limit = max(1, min(int(limit or 0) or 5, 8))
    if not character_payloads:
        return []

    context_text = "\n".join(
        item
        for item in [
            _safe_text(current_outline),
            _safe_text(getattr(dynamic_memory, "immediate_context", "")),
            "\n".join(getattr(dynamic_memory, "recent_summaries", []) or []),
            "\n".join(getattr(dynamic_memory, "semantic_rules", []) or []),
        ]
        if _safe_text(item)
    )
    if not context_text:
        return character_payloads[:resolved_limit]

    ranked = [
        (
            _character_match_score(payload, context_text),
            index,
            payload,
        )
        for index, payload in enumerate(character_payloads)
    ]
    matched = [item for item in ranked if item[0] > 0]
    if matched:
        matched.sort(key=lambda item: (-item[0], item[1]))
        return [item[2] for item in matched[:resolved_limit]]
    return character_payloads[:resolved_limit]


def _previous_chapter_payloads(
    chapters: list[Chapter],
    current_chapter: Chapter,
    limit: int,
) -> list[dict[str, Any]]:
    current_order = _chapter_order_value(current_chapter)
    candidates = [
        chapter
        for chapter in chapters
        if chapter.id != current_chapter.id
        and chapter.node_type in {ChapterNodeType.CHAPTER, ChapterNodeType.SCENE}
        and isinstance(chapter.content, str)
        and chapter.content.strip()
        and _chapter_order_value(chapter) < current_order
    ]
    candidates.sort(key=_chapter_order_value)
    selected = candidates[-max(limit, 0):]

    return [
        {
            "id": chapter.id,
            "title": chapter.title,
            "summary": chapter.summary,
            "content": _shorten(chapter.content, 2800),
        }
        for chapter in selected
    ]


def _previous_chapters_heading(requested_count: int, actual_count: Optional[int] = None) -> str:
    requested = max(0, int(requested_count or 0))
    actual = max(0, int(actual_count if actual_count is not None else requested))
    display_count = min(requested, actual) if requested > 0 else actual
    if display_count <= 1:
        return "前 1 章原文"
    return f"前 1-{display_count} 章原文"


def _base_system_instruction(
    module: AIModule,
    target_field: TargetField,
    target_units: Optional[int],
    apply_mode: ApplyMode,
) -> str:
    field_map = {
        "content": "章节正文",
        "outline": "章节大纲",
        "summary": "章节摘要",
    }
    module_map = {
        AIModule.CO_WRITING: "小说伴写",
        AIModule.OUTLINE_EXPANSION: "大纲扩写",
        AIModule.SUMMARY: "总结摘要",
        AIModule.SETTING_EXTRACTION: "设定提取",
        AIModule.CHARACTER_EXTRACTION: "人物提取",
        AIModule.RELATION_EXTRACTION: "关系提取",
        AIModule.REASONER: "推理规划",
    }

    target_part = f"目标长度约 {target_units} 字。" if target_units else "目标长度由上下文自然决定。"
    apply_part = "结果将追加到现有内容后方。" if apply_mode == "append" else "结果将覆盖现有内容。"
    return (
        f"你正在执行 `{module_map[module]}` 任务，目标字段为 `{field_map[target_field]}`。"
        f"{target_part}{apply_part}"
        "请保持结构一致、细节准确，不要编造与既有设定冲突的信息。"
    )


def build_prompt_context(
    db: Session,
    *,
    book: Book,
    chapter: Chapter,
    current_user: User,
    module: AIModule,
    target_field: TargetField,
    apply_mode: ApplyMode,
    user_prompt: str,
    previous_chapters: int,
    character_limit: int,
    target_units: Optional[int],
    config: Optional[ResolvedAIConfig] = None,
    system_prompt_override: Optional[str] = None,
    planning_text: Optional[str] = None,
    chunk_index: Optional[int] = None,
    total_chunks: Optional[int] = None,
    chunk_target_units: Optional[int] = None,
    accumulated_text: Optional[str] = None,
) -> PromptContext:
    all_chapters = db.execute(
        select(Chapter).where(Chapter.book_id == book.id)
    ).scalars().all()
    all_characters = db.execute(
        select(Character).where(Character.book_id == book.id)
    ).scalars().all()

    chapter_map = {item.id: item for item in all_chapters}
    previous_payloads = _previous_chapter_payloads(all_chapters, chapter, previous_chapters)
    all_character_payloads = _select_related_characters(
        all_characters,
        chapter_map,
        chapter,
        None,
    )
    character_payloads = all_character_payloads[: max(int(character_limit or 0), 0)] if int(character_limit or 0) > 0 else all_character_payloads
    current_existing_text = chapter.content or ""
    continuation_mode = target_field == "content" and apply_mode == "append"
    continuation_anchor = (
        _build_continuation_anchor(current_existing_text, accumulated_text)
        if continuation_mode
        else ""
    )
    resolved_system_prompt_override = (
        system_prompt_override.strip() if isinstance(system_prompt_override, str) and system_prompt_override.strip() else None
    )

    if _should_use_deepseek_memory_prompt(
        config=config,
        module=module,
        target_field=target_field,
        apply_mode=apply_mode,
        current_existing_text=current_existing_text,
        system_prompt_override=resolved_system_prompt_override,
    ):
        chapter_sequence = _resolve_effective_chapter_sequence(all_chapters, chapter)
        dynamic_memory = retrieve_dynamic_context(
            db,
            book_id=book.id,
            current_outline=chapter.outline or user_prompt,
            current_chapter_seq=chapter_sequence,
        )
        memory_character_payloads = _select_memory_character_payloads(
            all_character_payloads,
            current_outline=chapter.outline or user_prompt,
            dynamic_memory=dynamic_memory,
            limit=character_limit,
        )
        style_anchor = resolve_style_anchor(db, book)
        xml_prompt = build_deepseek_memory_prompt(
            style_anchor=style_anchor,
            recent_summaries=dynamic_memory.recent_summaries,
            immediate_context=dynamic_memory.immediate_context,
            semantic_rules=dynamic_memory.semantic_rules,
            character_cards=memory_character_payloads,
            current_outline=chapter.outline or user_prompt,
        )
        context_sections: dict[str, Any] = {
            "book": {
                "id": book.id,
                "title": book.title,
                "genre": book.genre,
                "language": book.language,
            },
            "chapter": {
                "id": chapter.id,
                "title": chapter.title,
                "outline": chapter.outline,
                "summary": chapter.summary,
                "existing_content": "",
                "context_summary": chapter.context_summary,
                "version": chapter.version,
                "sequence_number": chapter_sequence,
            },
            "deepseek_memory_mode": True,
            "style_anchor": style_anchor,
            "episodic_memory": {
                "recent_summaries": dynamic_memory.recent_summaries,
                "immediate_context": dynamic_memory.immediate_context,
            },
            "semantic_rules": dynamic_memory.semantic_rules,
            "related_characters_json": memory_character_payloads,
            "previous_chapters": previous_payloads,
            "user_instruction": user_prompt,
            "requested_target_units": target_units,
        }
        memory_system_instruction = (
            "请严格按照用户消息中的 XML 结构理解记忆上下文并直接输出正文，不要输出解释、标题或 XML。"
        )
        system_prompt = (
            f"{resolved_system_prompt_override}\n\n{memory_system_instruction}"
            if resolved_system_prompt_override
            else memory_system_instruction
        )
        logger.info(
            "deepseek_memory_prompt_enabled book_id=%s chapter_id=%s module=%s apply_mode=%s "
            "target_field=%s recent_summaries=%s semantic_rules=%s immediate_context_chars=%s "
            "character_cards=%s "
            "system_override_used=%s",
            book.id,
            chapter.id,
            module.value,
            apply_mode,
            target_field,
            len(dynamic_memory.recent_summaries),
            len(dynamic_memory.semantic_rules),
            len(dynamic_memory.immediate_context),
            len(memory_character_payloads),
            bool(resolved_system_prompt_override),
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": xml_prompt},
        ]
        return PromptContext(
            system_prompt=system_prompt,
            user_prompt=xml_prompt,
            messages=messages,
            related_characters=memory_character_payloads,
            previous_chapters=previous_payloads,
            context_sections=context_sections,
        )

    system_sections = []
    if config and config.system_prompt_template:
        system_sections.append(config.system_prompt_template.strip())
    system_sections.append(
        _base_system_instruction(module, target_field, target_units, apply_mode)
    )
    if continuation_mode:
        system_sections.append(
            "[续写硬规则]\n"
            "只输出当前结尾之后的全新正文。\n"
            "不要重复、改写、总结或引用任何已有正文。\n"
            "直接接着当前最后一句往后写，不要回头重写前文。\n"
            "不要输出标题、章节名、分块说明、前言、后记、注释、Markdown 或代码块。\n"
            "不要出现“以下是续写部分”“第一章（续）”“第1块内容已完成”这类说明话术。\n"
            "接近目标字数后自然收束。"
        )
    system_sections.extend(_module_quality_sections(module, target_field))
    system_sections.append(f"[系统文风]\n{_safe_text(book.global_style_prompt, '未设置全局文风提示词。')}")

    if chunk_index is not None and total_chunks is not None:
        system_sections.append(
            f"[分块生成]\n当前为第 {chunk_index}/{total_chunks} 块，目标长度约 {chunk_target_units or target_units or 0} 字。"
            "这是内部执行信息，不得在输出正文里提及“第几块”或完成说明。"
        )

    context_sections: dict[str, Any] = {
        "book": {
            "id": book.id,
            "title": book.title,
            "genre": book.genre,
            "language": book.language,
        },
        "chapter": {
            "id": chapter.id,
            "title": chapter.title,
            "outline": chapter.outline,
            "summary": chapter.summary,
            "existing_content": _shorten(current_existing_text, 3200),
            "context_summary": chapter.context_summary,
            "version": chapter.version,
        },
        "long_term_summary": _safe_text(book.long_term_summary, "暂无远期摘要。"),
        "world_bible": _safe_text(book.world_bible, "暂无世界观补充。"),
        "related_characters_json": character_payloads,
        "previous_chapters": previous_payloads,
        "user_instruction": user_prompt,
        "requested_target_units": target_units,
        "system_prompt_override": resolved_system_prompt_override,
    }
    if continuation_anchor:
        context_sections["continuation_anchor"] = continuation_anchor

    user_sections = [
        f"[书籍]\n《{book.title}》 / 类型：{_safe_text(book.genre, '未分类')}",
        f"[当前章节]\n标题：{chapter.title}\n大纲：{_safe_text(chapter.outline, '暂无大纲')}\n摘要：{_safe_text(chapter.summary, '暂无摘要')}",
        f"[远期摘要]\n{context_sections['long_term_summary']}",
        f"[世界观补充]\n{context_sections['world_bible']}",
        "[关联人物卡 JSON]\n" + json.dumps(character_payloads, ensure_ascii=False, indent=2),
    ]

    if previous_payloads:
        previous_text = "\n\n".join(
            f"### {item['title']}\n{_safe_text(item['content'], '')}" for item in previous_payloads
        )
        user_sections.append(
            f"[{_previous_chapters_heading(previous_chapters, len(previous_payloads))}]\n{previous_text}"
        )

    if continuation_anchor:
        user_sections.append(
            "[当前续写落点]\n"
            "以下片段是当前正文结尾，你的输出必须从它之后开始，只写新的续写内容：\n"
            f"{continuation_anchor}"
        )
    elif current_existing_text.strip():
        user_sections.append(f"[当前已有内容]\n{_shorten(current_existing_text, 3200)}")

    if planning_text:
        context_sections["reasoner_plan"] = planning_text
        user_sections.append(f"[Reasoner 规划]\n{planning_text}")

    if accumulated_text:
        context_sections["generated_so_far"] = accumulated_text
        if not continuation_mode:
            user_sections.append(f"[已经生成的内容]\n{_shorten(accumulated_text, 2200)}")

    if resolved_system_prompt_override and chunk_index is not None and total_chunks is not None:
        user_sections.append(
            "[本轮生成控制]\n"
            f"当前为第 {chunk_index}/{total_chunks} 块，目标长度约 {chunk_target_units or target_units or 0} 字。"
            "这是内部控制信息，不要在输出里提及。"
        )

    user_sections.append(
        f"[用户要求]\n{_safe_text(user_prompt, '请根据上下文继续创作，不偏离已知设定。')}"
    )

    if resolved_system_prompt_override:
        system_sections.append(f"[自定义系统补充要求]\n{resolved_system_prompt_override}")
    system_prompt = "\n\n".join(system_sections)
    user_prompt_text = "\n\n".join(user_sections)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt_text},
    ]
    return PromptContext(
        system_prompt=system_prompt,
        user_prompt=user_prompt_text,
        messages=messages,
        related_characters=character_payloads,
        previous_chapters=previous_payloads,
        context_sections=context_sections,
    )


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


def _models_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        normalized = normalized[: -len("/chat/completions")]
    if normalized.endswith("/models"):
        return normalized
    return f"{normalized}/models"


def _cache_key_for_models(base_url: str, api_key: Optional[str]) -> tuple[str, str]:
    masked_key = f"len:{len(api_key or '')}:{hash(api_key or '')}" if api_key else "no-key"
    return (base_url.rstrip("/"), masked_key)


def _coerce_positive_int(value: Any) -> Optional[int]:
    if value in (None, "", False):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _extract_context_window(raw_item: dict[str, Any]) -> Optional[int]:
    direct_candidates = [
        raw_item.get("context_window"),
        raw_item.get("context_length"),
        raw_item.get("max_context_length"),
        raw_item.get("max_input_tokens"),
        raw_item.get("input_token_limit"),
    ]
    for candidate in direct_candidates:
        number = _coerce_positive_int(candidate)
        if number is not None:
            return number

    for nested_key in ("top_provider", "capabilities", "limits", "metadata"):
        nested = raw_item.get(nested_key)
        if not isinstance(nested, dict):
            continue
        for field in ("context_window", "context_length", "max_context_length", "max_input_tokens", "input_token_limit"):
            number = _coerce_positive_int(nested.get(field))
            if number is not None:
                return number
    return None


def _extract_max_output_tokens(raw_item: dict[str, Any]) -> Optional[int]:
    direct_candidates = [
        raw_item.get("max_output_tokens"),
        raw_item.get("output_token_limit"),
        raw_item.get("max_completion_tokens"),
    ]
    for candidate in direct_candidates:
        number = _coerce_positive_int(candidate)
        if number is not None:
            return number

    for nested_key in ("top_provider", "capabilities", "limits", "metadata"):
        nested = raw_item.get(nested_key)
        if not isinstance(nested, dict):
            continue
        for field in ("max_output_tokens", "output_token_limit", "max_completion_tokens"):
            number = _coerce_positive_int(nested.get(field))
            if number is not None:
                return number
    return None


def _format_context_window_label(context_window: Optional[int]) -> str:
    if not context_window:
        return ""
    if context_window >= 1000:
        value = context_window / 1000
        return f"{value:.0f}k" if context_window % 1000 == 0 else f"{value:.1f}k"
    return str(context_window)


def _extract_text_from_openai_payload(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict):
                    if isinstance(item.get("text"), str):
                        text_parts.append(item["text"])
                    elif item.get("type") == "output_text" and isinstance(item.get("text"), str):
                        text_parts.append(item["text"])
            return "\n".join(part.strip() for part in text_parts if part.strip()).strip()

    output_text = payload.get("output_text")
    if isinstance(output_text, str):
        return output_text.strip()

    raise AIInvocationError("Model response did not contain any text output.")


def _extract_text_from_openai_stream_payload(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        output_text = payload.get("output_text")
        return output_text if isinstance(output_text, str) else ""

    choice = choices[0] if isinstance(choices[0], dict) else {}
    delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
    content = delta.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("text"), str):
                text_parts.append(item["text"])
            elif item.get("type") == "output_text" and isinstance(item.get("text"), str):
                text_parts.append(item["text"])
        return "".join(text_parts)

    text = choice.get("text")
    if isinstance(text, str):
        return text
    return ""


def _build_openai_chat_request_body(
    config: ResolvedAIConfig,
    *,
    messages: list[dict[str, str]],
    max_tokens_override: Optional[int] = None,
    stream: bool = False,
) -> dict[str, Any]:
    request_body: dict[str, Any] = {
        "model": config.model_name,
        "messages": messages,
    }
    if config.temperature is not None:
        request_body["temperature"] = config.temperature
    if config.top_p is not None:
        request_body["top_p"] = config.top_p
    if config.reasoning_effort:
        request_body["reasoning_effort"] = config.reasoning_effort
    if config.extra_body:
        request_body.update(config.extra_body)
    resolved_max_tokens = config.max_tokens
    if max_tokens_override is not None:
        resolved_max_tokens = (
            min(config.max_tokens, max_tokens_override)
            if config.max_tokens is not None
            else max_tokens_override
        )
    if resolved_max_tokens is not None:
        request_body["max_tokens"] = resolved_max_tokens
    if stream:
        request_body["stream"] = True
    return request_body


def _build_openai_chat_headers(config: ResolvedAIConfig, *, stream: bool = False) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        **{key: str(value) for key, value in config.extra_headers.items()},
    }
    if stream:
        headers["Accept"] = "text/event-stream"
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    return headers


def _iter_sse_event_data(response: Any) -> Iterator[str]:
    data_lines: list[str] = []
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace")
        stripped = line.strip("\r\n")
        if not stripped:
            if data_lines:
                yield "\n".join(data_lines)
                data_lines = []
            continue
        if stripped.startswith(":"):
            continue
        if stripped.startswith("data:"):
            data_lines.append(stripped[5:].lstrip())
    if data_lines:
        yield "\n".join(data_lines)


def call_openai_compatible_chat(
    config: ResolvedAIConfig,
    *,
    messages: list[dict[str, str]],
    max_tokens_override: Optional[int] = None,
) -> dict[str, Any]:
    request_body = _build_openai_chat_request_body(
        config,
        messages=messages,
        max_tokens_override=max_tokens_override,
        stream=False,
    )
    headers = _build_openai_chat_headers(config, stream=False)

    request = urllib.request.Request(
        _chat_completions_url(config.base_url),
        data=json.dumps(request_body).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    last_error: Optional[Exception] = None
    for attempt in range(1, _OPENAI_COMPAT_MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < _OPENAI_COMPAT_MAX_RETRIES and _is_retryable_request_error(exc):
                time.sleep(min(4.0, 0.8 * (2 ** (attempt - 1))))
                continue
            error_message = _format_request_error("Model request", exc)
            if attempt > 1:
                error_message = f"{error_message} after {attempt} attempts"
            raise AIInvocationError(error_message) from exc
    else:
        raise AIInvocationError("Model request failed without a usable response.")

    return {
        "text": _extract_text_from_openai_payload(payload),
        "raw_response": payload,
        "request_body": request_body,
        "url": _chat_completions_url(config.base_url),
    }


def iter_openai_compatible_chat_stream(
    config: ResolvedAIConfig,
    *,
    messages: list[dict[str, str]],
    max_tokens_override: Optional[int] = None,
) -> Iterator[dict[str, Any]]:
    request_body = _build_openai_chat_request_body(
        config,
        messages=messages,
        max_tokens_override=max_tokens_override,
        stream=True,
    )
    headers = _build_openai_chat_headers(config, stream=True)
    request = urllib.request.Request(
        _chat_completions_url(config.base_url),
        data=json.dumps(request_body).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    last_error: Optional[Exception] = None
    for attempt in range(1, _OPENAI_COMPAT_MAX_RETRIES + 1):
        emitted_text = False
        text_parts: list[str] = []
        try:
            with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
                for event_data in _iter_sse_event_data(response):
                    if event_data == "[DONE]":
                        yield {
                            "type": "done",
                            "text": "".join(text_parts),
                            "request_body": request_body,
                            "url": _chat_completions_url(config.base_url),
                        }
                        return

                    try:
                        payload = json.loads(event_data)
                    except json.JSONDecodeError as exc:
                        raise AIInvocationError("Model stream returned malformed JSON.") from exc

                    text_delta = _extract_text_from_openai_stream_payload(payload)
                    if not text_delta:
                        continue

                    emitted_text = True
                    text_parts.append(text_delta)
                    yield {
                        "type": "delta",
                        "delta": text_delta,
                        "text": "".join(text_parts),
                        "payload": payload,
                    }

                yield {
                    "type": "done",
                    "text": "".join(text_parts),
                    "request_body": request_body,
                    "url": _chat_completions_url(config.base_url),
                }
                return
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, AIInvocationError) as exc:
            last_error = exc
            if (
                attempt < _OPENAI_COMPAT_MAX_RETRIES
                and not emitted_text
                and _is_retryable_request_error(exc if not isinstance(exc, AIInvocationError) else OSError(str(exc)))
            ):
                time.sleep(min(4.0, 0.8 * (2 ** (attempt - 1))))
                continue
            error_message = _format_request_error("Model stream request", exc)
            if attempt > 1:
                error_message = f"{error_message} after {attempt} attempts"
            raise AIInvocationError(error_message) from exc

    if last_error is not None:
        raise AIInvocationError(_format_request_error("Model stream request", last_error)) from last_error
    raise AIInvocationError("Model stream request failed without a usable response.")


def fetch_openai_compatible_models(
    *,
    base_url: str,
    api_key: Optional[str],
    timeout_seconds: int = 30,
    use_cache: bool = True,
) -> list[dict[str, Any]]:
    cache_key = _cache_key_for_models(base_url, api_key)
    now = time.time()
    if use_cache:
        cached = _MODEL_METADATA_CACHE.get(cache_key)
        if cached and now - cached[0] < _MODEL_METADATA_CACHE_TTL_SECONDS:
            return [dict(item) for item in cached[1]]

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    request = urllib.request.Request(
        _models_url(base_url),
        headers=headers,
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise AIInvocationError(
            f"Model list request failed with HTTP {exc.code}: {body[:500]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise AIInvocationError(f"Model list request failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise AIInvocationError("Model list request timed out.") from exc
    except json.JSONDecodeError as exc:
        raise AIInvocationError("Model list response was not valid JSON.") from exc

    raw_items = payload.get("data")
    if not isinstance(raw_items, list):
        raise AIInvocationError("Model list response did not contain a `data` array.")

    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        model_id = str(raw_item.get("id") or raw_item.get("name") or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        context_window = _extract_context_window(raw_item)
        max_output_tokens = _extract_max_output_tokens(raw_item)
        context_label = _format_context_window_label(context_window)
        label = model_id if not context_label else f"{model_id} · {context_label} 上下文"
        items.append(
            {
                "id": model_id,
                "label": label,
                "owned_by": raw_item.get("owned_by"),
                "object": raw_item.get("object"),
                "created": raw_item.get("created"),
                "context_window": context_window,
                "max_output_tokens": max_output_tokens,
            }
        )

    items.sort(key=lambda item: item["id"].lower())
    _MODEL_METADATA_CACHE[cache_key] = (now, [dict(item) for item in items])
    return items


def get_openai_compatible_model_metadata(
    *,
    base_url: str,
    api_key: Optional[str],
    model_name: str,
    timeout_seconds: int = 30,
) -> Optional[dict[str, Any]]:
    normalized_model_name = str(model_name or "").strip()
    if not normalized_model_name:
        return None

    items = fetch_openai_compatible_models(
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
    )
    for item in items:
        if item["id"] == normalized_model_name:
            return dict(item)
    return None


def _extract_json_block(text: str) -> Any:
    value = text.strip()
    candidates = [value]

    fence_matches = re.findall(r"```(?:json)?\s*(.*?)```", value, flags=re.DOTALL | re.IGNORECASE)
    candidates.extend(match.strip() for match in fence_matches if match.strip())

    for start_char, end_char in (("{", "}"), ("[", "]")):
        start = value.find(start_char)
        end = value.rfind(end_char)
        if start != -1 and end != -1 and end > start:
            candidates.append(value[start : end + 1])

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    raise AIInvocationError("Model did not return valid JSON.")


def _normalized_name(value: str | None) -> str:
    return (value or "").strip().lower()


def _clean_character_name(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [item.strip() for item in re.split(r"[,\n，、]+", value) if item.strip()]
    return []


def _select_existing_relation(
    db: Session,
    *,
    book_id: int,
    source_character_id: int,
    target_character_id: int,
    relation_type: str,
) -> Optional[Relation]:
    matches = db.execute(
        select(Relation)
        .where(
            Relation.book_id == book_id,
            Relation.source_character_id == source_character_id,
            Relation.target_character_id == target_character_id,
            Relation.relation_type == relation_type,
        )
        .order_by(Relation.id.asc())
    ).scalars().all()
    if len(matches) > 1:
        logger.warning(
            "duplicate_relations_detected book_id=%s source_character_id=%s target_character_id=%s relation_type=%s relation_ids=%s",
            book_id,
            source_character_id,
            target_character_id,
            relation_type,
            [item.id for item in matches],
        )
    return matches[0] if matches else None


def _world_character_prompt(
    book: Book,
    chapter: Chapter,
    existing_characters: list[Character],
) -> list[dict[str, str]]:
    existing_names = [character.name for character in existing_characters]
    return [
        {
            "role": "system",
            "content": (
                "You extract novel world data. Return strict JSON only. "
                "Use Chinese text when the source is Chinese. "
                "Do not wrap the answer in markdown. "
                "Only keep source-supported, durable information. "
                "Do not speculate about motives, hidden settings, or future plot."
            ),
        },
        {
            "role": "user",
            "content": (
                "Extract characters and world facts from the current chapter.\n"
                "Return a JSON object with this shape:\n"
                "{\n"
                '  "characters": [\n'
                "    {\n"
                '      "name": "string",\n'
                '      "aliases": ["string"],\n'
                '      "role_label": "string",\n'
                '      "importance_level": "major | minor",\n'
                '      "description": "string",\n'
                '      "traits": ["string"],\n'
                '      "background": "string",\n'
                '      "goals": "string",\n'
                '      "secrets": "string",\n'
                '      "notes": "string",\n'
                '      "age": "string",\n'
                '      "short_term_goal": "string",\n'
                '      "long_term_goal": "string",\n'
                '      "motivation": "string",\n'
                '      "personality": "string",\n'
                '      "appearance": "string",\n'
                '      "weakness": "string",\n'
                '      "life_statuses": ["alive | dead | serious_injury | minor_injury | disabled"],\n'
                '      "timeline_entries": [\n'
                "        {\n"
                '          "event": "string",\n'
                '          "location": "string",\n'
                '          "status": "string"\n'
                "        }\n"
                "      ]\n"
                "    }\n"
                "  ],\n"
                '  "world_facts": ["string"]\n'
                "}\n\n"
                f"Book title: {book.title}\n"
                f"Genre: {_safe_text(book.genre, 'unknown')}\n"
                f"Existing characters: {json.dumps(existing_names, ensure_ascii=False)}\n"
                f"Book world bible: {_safe_text(book.world_bible, 'none')}\n"
                f"Chapter title: {chapter.title}\n"
                f"Chapter content:\n{_safe_text(chapter.content, 'none')}\n\n"
                "Merge aliases into existing names when obviously the same person. "
                "Only include characters that are meaningfully present in this chapter. "
                "For temporary passersby or low-importance characters, either omit them or mark importance_level as minor. "
                "Only fill age, personality, appearance, weakness, motivation, short_term_goal, long_term_goal, background, secrets, and life_statuses when the chapter clearly supports them. "
                "Use short_term_goal for an immediate chapter-visible objective, use long_term_goal only for a stable long-range pursuit, and do not guess hidden motivation. "
                "For each kept character, add at most 1 timeline_entries item describing what they are doing in this chapter, where they are, and what state they are in. "
                "timeline_entries only records chapter-local observable facts, not future prediction or long biography summary. "
                "For world_facts, only keep durable setting facts or stable人物/组织/地点事实. "
                "Do not include plot recap, temporary actions, dialogue, or repeated facts. "
                "Return at most 5 world_facts, each as one concise sentence.\n\n"
                f"{_WORLD_EXTRACTION_QUALITY_RULES}"
            ),
        },
    ]


def _world_relation_prompt(
    book: Book,
    chapter: Chapter,
    characters: list[dict[str, Any]],
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You extract relationships between known novel characters. "
                "Return strict JSON only, no markdown. "
                "Use Chinese for relation_type, label, and description when the source text is Chinese. "
                "Only keep relationships that are explicitly supported by the text. "
                "Do not speculate about future developments or hidden motives. "
                "Keep description to one short sentence, ideally within 18 to 40 Chinese characters."
            ),
        },
        {
            "role": "user",
            "content": (
                "Return a JSON object with this shape:\n"
                "{\n"
                '  "relations": [\n'
                "    {\n"
                '      "source_name": "string",\n'
                '      "target_name": "string",\n'
                '      "relation_type": "string",\n'
                '      "label": "string",\n'
                '      "description": "string",\n'
                '      "strength": 0.0,\n'
                '      "is_bidirectional": false\n'
                "    }\n"
                "  ]\n"
                "}\n\n"
                f"Book title: {book.title}\n"
                f"Known characters JSON: {json.dumps(characters, ensure_ascii=False)}\n"
                f"Chapter title: {chapter.title}\n"
                f"Chapter content:\n{_safe_text(chapter.content, 'none')}\n\n"
                "Only use source_name and target_name values from the known characters list. "
                "Skip speculative relationships. "
                "relation_type、label、description 必须优先输出简洁中文，不要输出英文关系词。"
                "description 只保留一条简短关系说明，不要写剧情复述、长摘要或多句分析。\n\n"
                f"{_RELATION_EXTRACTION_QUALITY_RULES}"
            ),
        },
    ]


def _merge_unique_strings(*values: Any) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for items in values:
        if not items:
            continue
        for item in items:
            normalized = item.strip()
            if not normalized or normalized.lower() in seen:
                continue
            seen.add(normalized.lower())
            merged.append(normalized)
    return merged


def _merge_appearance_chapter_ids(
    existing: Optional[Character],
    chapter_id: Optional[int],
) -> tuple[Optional[int], Optional[int]]:
    first_appearance = existing.first_appearance_chapter_id if existing else None
    last_appearance = existing.last_appearance_chapter_id if existing else None
    if chapter_id is None:
        return first_appearance, last_appearance

    if first_appearance is None:
        first_appearance = chapter_id
    else:
        first_appearance = min(first_appearance, chapter_id)

    if last_appearance is None:
        last_appearance = chapter_id
    else:
        last_appearance = max(last_appearance, chapter_id)

    return first_appearance, last_appearance


def _clean_world_fact(value: str | None) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = re.sub(r"^[\s\-\*\u2014\u2022]+", "", text)
    text = re.sub(r"[\s,.;:!?\u3001\u3002\uff0c\uff1b\uff1a\uff01\uff1f]+$", "", text)
    if len(text) <= _WORLD_FACT_MAX_LENGTH:
        return text
    trimmed = text[:_WORLD_FACT_MAX_LENGTH].rstrip(" ,.;:!?")
    trimmed = re.sub(r"[\u3001\u3002\uff0c\uff1b\uff1a\uff01\uff1f]+$", "", trimmed)
    return f"{trimmed}..."


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
    "mentor student": "师徒",
    "master disciple": "师徒",
    "teacher student": "师生",
    "lover": "恋人",
    "lovers": "恋人",
    "couple": "情侣",
    "spouse": "配偶",
    "married": "夫妻",
    "romantic interest": "暧昧对象",
    "enemy": "敌人",
    "enemies": "敌对",
    "rival": "对手",
    "rivals": "对手",
    "boss subordinate": "上下级",
    "leader subordinate": "上下级",
    "employer employee": "雇佣关系",
    "guardian ward": "监护关系",
    "parent child": "亲子",
    "mother son": "母子",
    "mother daughter": "母女",
    "father son": "父子",
    "father daughter": "父女",
    "siblings": "手足",
    "brothers": "兄弟",
    "sisters": "姐妹",
}


def _localize_relation_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text or _contains_cjk_text(text):
        return text or None
    return _RELATION_TEXT_TRANSLATIONS.get(_canonical_relation_text(text), text)


def normalize_relation_description(
    value: Any,
    *,
    max_chars: int = _RELATION_DESCRIPTION_MAX_LENGTH,
) -> Optional[str]:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return None
    first_sentence = re.split(r"[。！？!?；;]\s*", text, maxsplit=1)[0].strip()
    if first_sentence:
        text = first_sentence
    if len(text) <= max_chars:
        return text

    trimmed = text[:max_chars].rstrip(" ,.;:!?")
    trimmed = re.sub(r"[\u3001\u3002\uff0c\uff1b\uff1a\uff01\uff1f]+$", "", trimmed)
    return f"{trimmed}..."


def relation_description_preview(value: Any) -> Optional[str]:
    return normalize_relation_description(value, max_chars=_RELATION_DESCRIPTION_PREVIEW_LENGTH)


def _canonical_world_fact(value: str | None) -> str:
    text = _clean_world_fact(value)
    return re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE).lower()


def _world_fact_is_similar(candidate: str, existing: str) -> bool:
    candidate_key = _canonical_world_fact(candidate)
    existing_key = _canonical_world_fact(existing)
    if not candidate_key or not existing_key:
        return False
    if candidate_key == existing_key:
        return True

    shorter, longer = sorted((candidate_key, existing_key), key=len)
    if len(shorter) >= 10 and shorter in longer:
        return True

    if len(shorter) >= 12:
        similarity = SequenceMatcher(None, candidate_key, existing_key).ratio()
        if similarity >= _WORLD_FACT_SIMILARITY_THRESHOLD:
            return True
    return False


def merge_world_facts(
    existing_lines: list[str],
    incoming_lines: list[str],
) -> tuple[list[str], list[str]]:
    merged: list[str] = []
    appended: list[str] = []

    def add_fact(value: str, *, track_appended: bool) -> None:
        cleaned = _clean_world_fact(value)
        if not cleaned:
            return
        if any(_world_fact_is_similar(cleaned, item) for item in merged):
            return
        if len(merged) >= _WORLD_FACT_MAX_ITEMS:
            return
        merged.append(cleaned)
        if track_appended:
            appended.append(cleaned)

    for line in existing_lines:
        add_fact(line, track_appended=False)
    for line in incoming_lines:
        add_fact(line, track_appended=True)

    return merged, appended


def _parse_booleanish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    if isinstance(value, (int, float)):
        return value != 0
    return bool(value)


def _safe_parse_relation_strength(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None

    try:
        strength = float(value)
    except (TypeError, ValueError):
        return None

    if math.isnan(strength):
        return None
    if strength < 0.0:
        return 0.0
    if strength > 1.0:
        return 1.0
    return strength


def _is_retryable_request_error(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in {408, 409, 425, 429, 500, 502, 503, 504}
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, BaseException):
            return _is_retryable_request_error(reason)
        return any(
            token in str(reason).lower()
            for token in ("timed out", "timeout", "connection reset", "temporarily unavailable")
        )
    if isinstance(exc, ConnectionResetError):
        return True
    if isinstance(exc, OSError):
        if getattr(exc, "errno", None) in {54, 104, 110, 111, 10054}:
            return True
        return any(
            token in str(exc).lower()
            for token in ("timed out", "timeout", "connection reset", "temporarily unavailable")
        )
    return False


def _format_request_error(action: str, exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        body = exc.read().decode("utf-8", errors="replace")
        return f"{action} failed with HTTP {exc.code}: {body[:500]}"
    if isinstance(exc, urllib.error.URLError):
        return f"{action} failed: {exc.reason}"
    if isinstance(exc, TimeoutError):
        return f"{action} timed out."
    return f"{action} failed: {exc}"


def _default_target_units(module: AIModule, target_field: TargetField) -> int:
    if target_field == "outline":
        return 500
    if module == AIModule.OUTLINE_EXPANSION:
        return 800
    if module == AIModule.SUMMARY:
        return 350
    return 1200


def _is_reasoning_model_config(config: ResolvedAIConfig | None) -> bool:
    model_name = str(getattr(config, "model_name", "") or "").strip().lower()
    return "reasoner" in model_name or "reasoning" in model_name


def _derive_output_max_tokens(target_units: int, config: ResolvedAIConfig | None = None) -> int:
    normalized_target = max(int(target_units or 0), 1)
    if _is_reasoning_model_config(config):
        estimated = math.ceil(normalized_target * 4.8) + 1200
        return max(1024, min(8192, estimated))
    estimated = math.ceil(normalized_target * 2.6) + 160
    return max(192, min(4096, estimated))


def _trim_text_to_units(text: str, max_units: int) -> str:
    if not text or max_units <= 0:
        return ""

    matches = list(_TEXT_UNIT_PATTERN.finditer(text))
    if len(matches) <= max_units:
        return text.strip()

    end_index = matches[max_units - 1].end()
    while end_index < len(text) and text[end_index] in _TRAILING_PUNCTUATION:
        end_index += 1
    return text[:end_index].rstrip()


def _trim_text_to_natural_units(text: str, target_units: int, *, soft_overrun_units: int = 90) -> str:
    if not text or target_units <= 0:
        return ""

    value = text.strip()
    matches = list(_TEXT_UNIT_PATTERN.finditer(value))
    if len(matches) <= target_units:
        return value

    target_end = matches[target_units - 1].end()
    soft_limit_units = min(len(matches), target_units + max(24, soft_overrun_units))
    soft_limit_end = matches[soft_limit_units - 1].end()
    search_window = value[target_end:soft_limit_end]
    natural_break = re.search(r"[。！？!?；;…](?:[”’」』】》）\)\]]*)|\n\s*\n", search_window)
    if natural_break:
        return value[: target_end + natural_break.end()].rstrip()

    backward_window = value[:soft_limit_end]
    candidates = list(
        re.finditer(r"[。！？!?；;…](?:[”’」』】》）\)\]]*)|\n\s*\n", backward_window)
    )
    minimum_units = max(1, target_units - max(80, target_units // 5))
    if candidates:
        for match in reversed(candidates):
            candidate = value[: match.end()].rstrip()
            if estimate_text_units(candidate) >= minimum_units:
                return candidate

    return _trim_text_to_units(value, target_units)


def _draft_chunk_unit_limit(target_units: int) -> int:
    normalized_target = max(int(target_units or 0), 1)
    return normalized_target + max(80, normalized_target // 4)


def _draft_total_unit_limit(target_units: int) -> int:
    normalized_target = max(int(target_units or 0), 1)
    return normalized_target + max(180, normalized_target // 3)


def _refinement_suggestion(actual_units: int, target_units: Optional[int]) -> dict[str, Any]:
    if not target_units or target_units <= 0:
        return {
            "mode": "none",
            "delta_units": 0,
            "within_tolerance": True,
            "tolerance_units": 0,
            "label": "当前字数无需二次调整",
        }

    delta_units = int(actual_units or 0) - int(target_units)
    tolerance_units = max(60, int(target_units * 0.12))
    if abs(delta_units) <= tolerance_units:
        mode = "none"
        label = "当前字数已接近目标，可直接插入正文"
    elif delta_units < 0:
        mode = "expand"
        label = "当前草稿偏短，建议二次扩写到目标字数"
    else:
        mode = "trim"
        label = "当前草稿偏长，建议二次精简到目标字数"

    return {
        "mode": mode,
        "delta_units": delta_units,
        "within_tolerance": mode == "none",
        "tolerance_units": tolerance_units,
        "label": label,
    }


def _tail_text_by_units(text: str | None, max_units: int) -> str:
    if not text or max_units <= 0:
        return ""

    value = text.strip()
    matches = list(_TEXT_UNIT_PATTERN.finditer(value))
    if len(matches) <= max_units:
        return value

    start_index = matches[-max_units].start()
    return value[start_index:].lstrip()


def _build_continuation_anchor(
    current_text: str | None,
    accumulated_text: str | None,
    max_units: int = 1800,
) -> str:
    anchor_source = "\n\n".join(
        part.strip()
        for part in (current_text or "", accumulated_text or "")
        if isinstance(part, str) and part.strip()
    )
    return _tail_text_by_units(anchor_source, max_units)


def _normalize_compare_text(text: str | None) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


_MODEL_ARTIFACT_PATTERNS = [
    re.compile(r"(?is)<[^>\n]{0,80}thinking[^>\n]{0,80}>"),
    re.compile(r"(?is)<[^>\n]{0,80}think[^>\n]{0,80}>"),
    re.compile(r"(?is)<think>.*?</think>"),
    re.compile(r"(?is)<thinking>.*?</thinking>"),
]

_LEADING_META_LINE_PATTERNS = [
    re.compile(r"^\s*#{1,6}\s*第[一二三四五六七八九十百千万0-9]+[章节回幕卷部篇].*$"),
    re.compile(r"^\s*第[一二三四五六七八九十百千万0-9]+[章节回幕卷部篇][（(].*[）)]\s*$"),
    re.compile(r"^\s*第\s*\d+\s*块.*$"),
    re.compile(r"^\s*(续写|正文|内容)?部分[:：]?\s*$"),
]

_LEADING_META_PARAGRAPH_PATTERNS = [
    re.compile(r"^\s*好(?:的|，|,).{0,60}(以下|下面).{0,40}(续写|正文|内容).{0,20}[:：]?\s*$"),
    re.compile(r"^\s*(以下|下面).{0,40}(续写|正文|内容).{0,20}[:：]?\s*$"),
    re.compile(r"^\s*这是.{0,40}(续写|正文|内容).{0,20}[:：]?\s*$"),
    re.compile(r"^\s*第一章第[一二三四五六七八九十百千万0-9]+块内容已完成。?\s*$"),
    re.compile(r"^\s*第[一二三四五六七八九十百千万0-9]+块内容已完成。?\s*$"),
]


def _remove_model_artifacts(text: str) -> str:
    cleaned = text or ""
    for pattern in _MODEL_ARTIFACT_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    return cleaned.strip()


def _strip_leading_meta_paragraphs(text: str) -> str:
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", text.strip()) if item.strip()]
    while paragraphs:
        first = paragraphs[0]
        if any(pattern.match(first) for pattern in _LEADING_META_PARAGRAPH_PATTERNS):
            paragraphs.pop(0)
            continue
        break
    return "\n\n".join(paragraphs).strip()


def _strip_leading_meta_lines(text: str) -> str:
    lines = text.strip().splitlines()
    while lines:
        first = lines[0].strip()
        if not first:
            lines.pop(0)
            continue
        if any(pattern.match(first) for pattern in _LEADING_META_LINE_PATTERNS):
            lines.pop(0)
            continue
        break
    return "\n".join(lines).strip()


def _clean_generated_content_artifacts(text: str) -> str:
    cleaned = _remove_model_artifacts(text)
    cleaned = _strip_leading_meta_paragraphs(cleaned)
    cleaned = _strip_leading_meta_lines(cleaned)
    cleaned = re.sub(
        r"^\s*好(?:的|，|,)\s*(以下|下面).{0,40}(续写|正文|内容).{0,20}[:：]?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"^\s*(以下|下面).{0,40}(续写|正文|内容).{0,20}[:：]?\s*", "", cleaned)
    return cleaned.strip()


def _strip_leading_overlap(reference_text: str, generated_text: str, min_units: int = 8) -> str:
    reference = reference_text.rstrip()
    generated = generated_text.lstrip()
    if not reference or not generated:
        return generated

    reference_tail = _tail_text_by_units(reference, 1800)
    max_length = min(len(reference_tail), len(generated))
    for overlap in range(max_length, 0, -1):
        candidate = generated[:overlap]
        if estimate_text_units(candidate) < min_units:
            break
        if reference_tail.endswith(candidate):
            return generated[overlap:].lstrip()
    return generated


def _drop_leading_repeated_paragraphs(reference_text: str, generated_text: str) -> str:
    reference_key = _normalize_compare_text(_tail_text_by_units(reference_text, 2400))
    if not reference_key:
        return generated_text.strip()

    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", generated_text.strip()) if item.strip()]
    while paragraphs:
        candidate = paragraphs[0]
        candidate_key = _normalize_compare_text(candidate)
        if len(candidate_key) < 8 or candidate_key not in reference_key:
            break
        paragraphs.pop(0)
    return "\n\n".join(paragraphs).strip()


def _sanitize_generated_continuation(
    current_text: str,
    generated_text: str,
    *,
    accumulated_text: Optional[str],
    target_field: TargetField,
    apply_mode: ApplyMode,
    max_units: Optional[int] = None,
) -> str:
    text = generated_text.strip()
    if not text:
        return ""
    text = _clean_generated_content_artifacts(text)
    if not text:
        return ""
    if target_field != "content" or apply_mode != "append":
        return _trim_text_to_natural_units(text, max_units) if max_units else text

    reference = current_text.strip()
    if accumulated_text:
        reference = f"{reference}\n\n{accumulated_text.strip()}".strip()

    text = _strip_leading_overlap(reference, text)
    text = _drop_leading_repeated_paragraphs(reference, text)
    text = _clean_generated_content_artifacts(text)
    if max_units:
        text = _trim_text_to_natural_units(text, max_units)
    return text.strip()


def split_text_into_unit_chunks(text: str, max_units: int) -> list[str]:
    if not text or max_units <= 0:
        return []

    remaining = text.strip()
    chunks: list[str] = []
    while remaining:
        chunk = _trim_text_to_units(remaining, max_units)
        if not chunk:
            break
        chunks.append(chunk)
        if len(chunk) >= len(remaining):
            break
        remaining = remaining[len(chunk):].lstrip()
    return chunks


def _chunk_plan(total_units: int, chunk_size: int) -> list[int]:
    if total_units <= 0:
        return [0]
    chunk_size = max(chunk_size, 200)
    chunk_count = max(1, math.ceil(total_units / chunk_size))
    base_size = total_units // chunk_count
    remainder = total_units % chunk_count
    return [base_size + (1 if index < remainder else 0) for index in range(chunk_count)]


def _planning_messages(
    book: Book,
    chapter: Chapter,
    module: AIModule,
    user_prompt: str,
    target_units: int,
    chunk_sizes: list[int],
) -> list[dict[str, str]]:
    chunk_text = "\n".join(
        f"- 第 {index + 1} 块：约 {size} 字"
        for index, size in enumerate(chunk_sizes)
    )
    return [
        {
            "role": "system",
            "content": (
                "你是长篇小说生成任务的规划器。"
                "请根据任务输出简洁的分块规划，优先保证剧情承接、角色动机和段落节奏。"
                "只输出编号规划，不要直接写正文，不要空泛抒情，不要总结主题。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"书名：{book.title}\n"
                f"章节：{chapter.title}\n"
                f"模块：{module.value}\n"
                f"目标总字数：约 {target_units} 字\n"
                f"建议分块：\n{chunk_text}\n\n"
                f"章节大纲：{_safe_text(chapter.outline, '暂无')}\n"
                f"远期摘要：{_safe_text(book.long_term_summary, '暂无')}\n"
                f"用户要求：{_safe_text(user_prompt, '无')}\n\n"
                "请直接输出带编号的分块规划，每块说明目标、推进点和承接方式。"
            ),
        },
    ]


def _resolve_reasoner_plan(
    db: Session,
    *,
    book: Book,
    chapter: Chapter,
    current_user: User,
    module: AIModule,
    user_prompt: str,
    target_units: int,
    chunk_sizes: list[int],
    primary_config: ResolvedAIConfig,
    use_reasoner_planning: bool,
) -> tuple[Optional[str], Optional[ResolvedAIConfig], Optional[dict[str, Any]]]:
    if not use_reasoner_planning:
        return None, None, None

    try:
        planning_config = resolve_ai_config(db, AIModule.REASONER, current_user, book)
    except AIConfigNotFoundError:
        planning_config = primary_config

    planning_payload = call_openai_compatible_chat(
        planning_config,
        messages=_planning_messages(
            book,
            chapter,
            module,
            user_prompt,
            target_units,
            chunk_sizes,
        ),
    )
    return planning_payload["text"], planning_config, planning_payload


def _merge_generated_text(current_text: str, generated_text: str, apply_mode: ApplyMode) -> str:
    if apply_mode == "replace":
        return generated_text.strip()
    if not current_text.strip():
        return generated_text.strip()
    if not generated_text.strip():
        return current_text.strip()
    return f"{current_text.rstrip()}\n\n{generated_text.strip()}"


def _snapshot_label(module: AIModule, target_field: TargetField) -> str:
    return f"AI {module.value} before {target_field} update"


def _update_book_aggregates(db: Session, book: Book) -> None:
    chapters = db.execute(
        select(Chapter).where(Chapter.book_id == book.id)
    ).scalars().all()
    book.chapter_count = len(
        [
            chapter
            for chapter in chapters
            if chapter.node_type in {ChapterNodeType.CHAPTER, ChapterNodeType.SCENE}
        ]
    )
    book.word_count = sum(chapter.word_count or estimate_text_units(chapter.content or "") for chapter in chapters)
    db.add(book)


def _apply_generated_text(
    db: Session,
    *,
    book: Book,
    chapter: Chapter,
    current_user: User,
    module: AIModule,
    user_prompt: str,
    target_field: TargetField,
    apply_mode: ApplyMode,
    target_units: Optional[int],
    previous_chapters: int,
    character_limit: int,
    system_prompt_override: Optional[str],
    planning_text: Optional[str],
    generated_text: str,
    config: Optional[ResolvedAIConfig],
    context_preview: Optional[PromptContext],
    generated_units_before_trim: Optional[int],
    was_trimmed: bool,
    raw_requests: Optional[list[dict[str, Any]]],
    store_snapshot: bool,
) -> dict[str, Any]:
    resolved_target_units = target_units or _default_target_units(module, target_field)
    effective_config = config or resolve_ai_config(db, module, current_user, book)
    preview_context = context_preview or build_prompt_context(
        db,
        book=book,
        chapter=chapter,
        current_user=current_user,
        module=module,
        target_field=target_field,
        apply_mode=apply_mode,
        user_prompt=user_prompt,
        previous_chapters=previous_chapters,
        character_limit=character_limit,
        target_units=resolved_target_units,
        config=effective_config,
        system_prompt_override=system_prompt_override,
        planning_text=planning_text,
    )

    current_field_value = getattr(chapter, target_field) or ""
    cleaned_text = _sanitize_generated_continuation(
        current_field_value,
        generated_text,
        accumulated_text=None,
        target_field=target_field,
        apply_mode=apply_mode,
        max_units=None,
    )
    if not cleaned_text:
        raise AIInvocationError("没有可写入的新内容，请先调整草稿后再插入。")

    generated_units = estimate_text_units(cleaned_text)
    before_version = chapter.version
    before_payload = {
        "target_field": target_field,
        "before_value": current_field_value,
        "planning_text": planning_text,
        "context_preview": preview_context.to_dict(),
        "generation_metrics": {
            "target_units": resolved_target_units,
            "generated_units_before_trim": generated_units_before_trim or generated_units,
            "generated_units": generated_units,
            "was_trimmed": was_trimmed,
        },
        "system_prompt_override": system_prompt_override,
        "llm_calls": raw_requests or [],
        "draft_apply": True,
    }

    snapshot_id: Optional[int] = None
    if store_snapshot:
        snapshot_ai_config_id: Optional[int] = None
        if effective_config.id is not None:
            config_row = db.get(AIConfig, effective_config.id)
            snapshot_ai_config_id = config_row.id if config_row is not None else None
        snapshot = Snapshot(
            book_id=book.id,
            chapter_id=chapter.id,
            created_by_id=current_user.id,
            ai_config_id=snapshot_ai_config_id,
            kind=SnapshotKind.BEFORE_AI_EDIT,
            label=_snapshot_label(module, target_field),
            chapter_title=chapter.title,
            chapter_version=chapter.version,
            outline=chapter.outline or "",
            content=chapter.content or "",
            summary=chapter.summary,
            source_model_name=effective_config.model_name,
            prompt_payload=before_payload,
            diff_summary=(
                f"Prepared {module.value} draft apply for {target_field}; "
                f"generated about {generated_units} text units."
            ),
            word_count=estimate_text_units(chapter.content or ""),
            character_count=len(chapter.content or ""),
        )
        db.add(snapshot)
        db.flush()
        snapshot_id = snapshot.id

    updated_value = _merge_generated_text(current_field_value, cleaned_text, apply_mode)
    setattr(chapter, target_field, updated_value)
    chapter.version += 1
    if cleaned_text:
        extra_data = _normalize_extra_data(chapter.extra_data)
        extra_data["last_ai_draft_text"] = cleaned_text
        chapter.extra_data = extra_data

    if target_field == "content":
        chapter.word_count = estimate_text_units(updated_value)

    db.add(chapter)
    _update_book_aggregates(db, book)
    db.commit()
    db.refresh(chapter)

    return {
        "module": module.value,
        "target_field": target_field,
        "apply_mode": apply_mode,
        "target_units": resolved_target_units,
        "ai_config": effective_config.public_dict(),
        "planning_text": planning_text,
        "context_preview": preview_context.to_dict(),
        "generated_text": cleaned_text,
        "generated_units": generated_units,
        "generated_units_before_trim": generated_units_before_trim or generated_units,
        "was_trimmed": was_trimmed,
        "applied_text": updated_value,
        "applied_units": estimate_text_units(updated_value),
        "snapshot_id": snapshot_id,
        "chapter_version_before": before_version,
        "chapter_version_after": chapter.version,
        "refinement_suggestion": _refinement_suggestion(generated_units, resolved_target_units),
    }


def run_generation(
    db: Session,
    *,
    book: Book,
    chapter: Chapter,
    current_user: User,
    module: AIModule,
    user_prompt: str,
    target_field: TargetField,
    apply_mode: ApplyMode,
    target_units: Optional[int],
    previous_chapters: int,
    character_limit: int,
    system_prompt_override: Optional[str],
    chunk_size: int,
    use_reasoner_planning: bool,
    dry_run: bool,
    store_snapshot: bool,
    apply_result: bool = True,
    enforce_target_units: bool = True,
) -> dict[str, Any]:
    config, fallback_from_module = _resolve_generation_config(
        db,
        module=module,
        current_user=current_user,
        book=book,
    )
    resolved_target_units = target_units or _default_target_units(module, target_field)
    chunk_sizes = _chunk_plan(resolved_target_units, chunk_size)

    planning_text: Optional[str] = None
    planning_config: Optional[ResolvedAIConfig] = None
    planning_payload: Optional[dict[str, Any]] = None
    if not dry_run:
        planning_text, planning_config, planning_payload = _resolve_reasoner_plan(
            db,
            book=book,
            chapter=chapter,
            current_user=current_user,
            module=module,
            user_prompt=user_prompt,
            target_units=resolved_target_units,
            chunk_sizes=chunk_sizes,
            primary_config=config,
            use_reasoner_planning=use_reasoner_planning,
        )

    preview_context = build_prompt_context(
        db,
        book=book,
        chapter=chapter,
        current_user=current_user,
        module=module,
        target_field=target_field,
        apply_mode=apply_mode,
        user_prompt=user_prompt,
        previous_chapters=previous_chapters,
        character_limit=character_limit,
        target_units=resolved_target_units,
        config=config,
        system_prompt_override=system_prompt_override,
        planning_text=planning_text,
    )

    result: dict[str, Any] = {
        "module": module.value,
        "target_field": target_field,
        "apply_mode": apply_mode,
        "target_units": resolved_target_units,
        "ai_config": config.public_dict(),
        "reasoner_config": planning_config.public_dict() if planning_config else None,
        "planning_text": planning_text,
        "context_preview": preview_context.to_dict(),
        "chunks": [],
        "dry_run": dry_run,
        "system_prompt_override_used": bool(
            isinstance(system_prompt_override, str) and system_prompt_override.strip()
        ),
        "ai_config_fallback_from": fallback_from_module.value if fallback_from_module else None,
    }

    if dry_run:
        return result

    current_field_value = getattr(chapter, target_field) or ""
    chunk_outputs: list[str] = []
    chunk_debug: list[dict[str, Any]] = []
    raw_requests: list[dict[str, Any]] = []
    generated_units_before_trim = 0
    was_trimmed = False

    for index, chunk_target in enumerate(chunk_sizes, start=1):
        chunk_apply_mode: ApplyMode = apply_mode if index == 1 else "append"
        accumulated_chunk_text = "\n\n".join(chunk_outputs) if chunk_outputs else None
        chunk_context = build_prompt_context(
            db,
            book=book,
            chapter=chapter,
            current_user=current_user,
            module=module,
            target_field=target_field,
            apply_mode=chunk_apply_mode,
            user_prompt=user_prompt,
            previous_chapters=previous_chapters,
            character_limit=character_limit,
            target_units=resolved_target_units,
            config=config,
            system_prompt_override=system_prompt_override,
            planning_text=planning_text,
            chunk_index=index,
            total_chunks=len(chunk_sizes),
            chunk_target_units=chunk_target,
            accumulated_text=accumulated_chunk_text,
        )
        payload = call_openai_compatible_chat(
            config,
            messages=chunk_context.messages,
            max_tokens_override=_derive_output_max_tokens(chunk_target, config),
        )
        raw_chunk_text = payload["text"].strip()
        raw_chunk_units = estimate_text_units(raw_chunk_text)
        generated_units_before_trim += raw_chunk_units
        chunk_limit_units = (
            chunk_target
            if enforce_target_units
            else _draft_chunk_unit_limit(chunk_target)
        )
        chunk_text = _sanitize_generated_continuation(
            current_field_value,
            raw_chunk_text,
            accumulated_text=accumulated_chunk_text,
            target_field=target_field,
            apply_mode=chunk_apply_mode,
            max_units=chunk_limit_units,
        )
        chunk_units = estimate_text_units(chunk_text)
        chunk_was_trimmed = chunk_text != raw_chunk_text
        chunk_was_skipped = not bool(chunk_text)
        was_trimmed = was_trimmed or chunk_was_trimmed
        if chunk_text:
            chunk_outputs.append(chunk_text)
        raw_requests.append(
            {
                "chunk_index": index,
                "request_body": payload["request_body"],
                "endpoint_url": payload["url"],
                "raw_response": payload["raw_response"],
            }
        )
        chunk_debug.append(
            {
                "chunk_index": index,
                "chunk_target_units": chunk_target,
                "output_units_before_trim": raw_chunk_units,
                "output_units": chunk_units,
                "was_trimmed": chunk_was_trimmed,
                "was_skipped": chunk_was_skipped,
            }
        )

    generated_text_before_final_trim = "\n\n".join(part for part in chunk_outputs if part.strip()).strip()
    final_limit_units = (
        resolved_target_units
        if enforce_target_units
        else _draft_total_unit_limit(resolved_target_units)
    )
    generated_text = _sanitize_generated_continuation(
        current_field_value,
        generated_text_before_final_trim,
        accumulated_text=None,
        target_field=target_field,
        apply_mode=apply_mode,
        max_units=final_limit_units,
    )
    was_trimmed = was_trimmed or generated_text != generated_text_before_final_trim

    if not generated_text:
        raise AIInvocationError("模型没有产出新的续写内容，返回结果与已有正文重复。")

    generated_units = estimate_text_units(generated_text)
    result.update(
        {
            "generated_text": generated_text,
            "generated_units_before_trim": generated_units_before_trim,
            "generated_units": generated_units,
            "was_trimmed": was_trimmed,
            "chunks": chunk_debug,
            "refinement_suggestion": _refinement_suggestion(generated_units, resolved_target_units),
        }
    )
    if planning_payload is not None:
        result["reasoner"] = {
            "model": planning_payload.get("model"),
            "usage": planning_payload.get("usage"),
            "elapsed_seconds": planning_payload.get("elapsed_seconds"),
        }
    if not apply_result:
        result["draft_only"] = True
        return result

    applied_result = _apply_generated_text(
        db,
        book=book,
        chapter=chapter,
        current_user=current_user,
        module=module,
        user_prompt=user_prompt,
        target_field=target_field,
        apply_mode=apply_mode,
        target_units=resolved_target_units,
        previous_chapters=previous_chapters,
        character_limit=character_limit,
        system_prompt_override=system_prompt_override,
        planning_text=planning_text,
        generated_text=generated_text,
        config=config,
        context_preview=preview_context,
        generated_units_before_trim=generated_units_before_trim,
        was_trimmed=was_trimmed,
        raw_requests=raw_requests,
        store_snapshot=store_snapshot,
    )
    result.update(applied_result)
    return result


def stream_generation_draft_events(
    db: Session,
    *,
    book: Book,
    chapter: Chapter,
    current_user: User,
    module: AIModule,
    user_prompt: str,
    target_field: TargetField,
    apply_mode: ApplyMode,
    target_units: Optional[int],
    previous_chapters: int,
    character_limit: int,
    system_prompt_override: Optional[str],
    chunk_size: int,
    use_reasoner_planning: bool,
) -> Iterator[dict[str, Any]]:
    config, fallback_from_module = _resolve_generation_config(
        db,
        module=module,
        current_user=current_user,
        book=book,
    )
    resolved_target_units = target_units or _default_target_units(module, target_field)
    chunk_sizes = _chunk_plan(resolved_target_units, chunk_size)

    yield {
        "type": "status",
        "phase": "planning",
        "message": "正在进行写前规划与上下文准备…"
        if use_reasoner_planning
        else "正在准备上下文…",
    }

    planning_text, planning_config, planning_payload = _resolve_reasoner_plan(
        db,
        book=book,
        chapter=chapter,
        current_user=current_user,
        module=module,
        user_prompt=user_prompt,
        target_units=resolved_target_units,
        chunk_sizes=chunk_sizes,
        primary_config=config,
        use_reasoner_planning=use_reasoner_planning,
    )

    preview_context = build_prompt_context(
        db,
        book=book,
        chapter=chapter,
        current_user=current_user,
        module=module,
        target_field=target_field,
        apply_mode=apply_mode,
        user_prompt=user_prompt,
        previous_chapters=previous_chapters,
        character_limit=character_limit,
        target_units=resolved_target_units,
        config=config,
        system_prompt_override=system_prompt_override,
        planning_text=planning_text,
    )

    result: dict[str, Any] = {
        "module": module.value,
        "target_field": target_field,
        "apply_mode": apply_mode,
        "target_units": resolved_target_units,
        "ai_config": config.public_dict(),
        "ai_config_fallback_from": fallback_from_module.value if fallback_from_module else None,
        "reasoner_config": planning_config.public_dict() if planning_config else None,
        "planning_text": planning_text,
        "context_preview": preview_context.to_dict(),
        "chunks": [],
        "dry_run": False,
        "system_prompt_override_used": bool(
            isinstance(system_prompt_override, str) and system_prompt_override.strip()
        ),
    }

    current_field_value = getattr(chapter, target_field) or ""
    chunk_outputs: list[str] = []
    chunk_debug: list[dict[str, Any]] = []
    raw_requests: list[dict[str, Any]] = []
    generated_units_before_trim = 0
    was_trimmed = False

    for index, chunk_target in enumerate(chunk_sizes, start=1):
        chunk_apply_mode: ApplyMode = apply_mode if index == 1 else "append"
        accumulated_chunk_text = "\n\n".join(chunk_outputs) if chunk_outputs else None
        chunk_context = build_prompt_context(
            db,
            book=book,
            chapter=chapter,
            current_user=current_user,
            module=module,
            target_field=target_field,
            apply_mode=chunk_apply_mode,
            user_prompt=user_prompt,
            previous_chapters=previous_chapters,
            character_limit=character_limit,
            target_units=resolved_target_units,
            config=config,
            system_prompt_override=system_prompt_override,
            planning_text=planning_text,
            chunk_index=index,
            total_chunks=len(chunk_sizes),
            chunk_target_units=chunk_target,
            accumulated_text=accumulated_chunk_text,
        )
        chunk_limit_units = _draft_chunk_unit_limit(chunk_target)
        yield {
            "type": "status",
            "phase": "chunk",
            "chunk_index": index,
            "total_chunks": len(chunk_sizes),
            "message": f"正在生成第 {index}/{len(chunk_sizes)} 段…",
        }

        stream_result: Optional[dict[str, Any]] = None
        for stream_event in iter_openai_compatible_chat_stream(
            config,
            messages=chunk_context.messages,
            max_tokens_override=_derive_output_max_tokens(chunk_target, config),
        ):
            if stream_event["type"] == "delta":
                raw_chunk_text = str(stream_event.get("text") or "").strip()
                visible_chunk_text = _sanitize_generated_continuation(
                    current_field_value,
                    raw_chunk_text,
                    accumulated_text=accumulated_chunk_text,
                    target_field=target_field,
                    apply_mode=chunk_apply_mode,
                    max_units=chunk_limit_units,
                )
                preview_outputs = list(chunk_outputs)
                if visible_chunk_text:
                    preview_outputs.append(visible_chunk_text)
                visible_text = "\n\n".join(part for part in preview_outputs if part.strip()).strip()
                yield {
                    "type": "draft",
                    "chunk_index": index,
                    "total_chunks": len(chunk_sizes),
                    "text": visible_text,
                }
                continue
            stream_result = stream_event

        if stream_result is None:
            raise AIInvocationError("模型流式生成未返回完成事件。")

        raw_chunk_text = str(stream_result.get("text") or "").strip()
        if not raw_chunk_text:
            fallback_payload = call_openai_compatible_chat(
                config,
                messages=chunk_context.messages,
                max_tokens_override=_derive_output_max_tokens(chunk_target, config),
            )
            raw_chunk_text = str(fallback_payload.get("text") or "").strip()
            raw_requests.append(
                {
                    "chunk_index": index,
                    "request_body": fallback_payload["request_body"],
                    "endpoint_url": fallback_payload["url"],
                    "raw_response": fallback_payload["raw_response"],
                    "streamed": False,
                    "fallback_after_empty_stream": True,
                }
            )
        raw_chunk_units = estimate_text_units(raw_chunk_text)
        generated_units_before_trim += raw_chunk_units
        chunk_text = _sanitize_generated_continuation(
            current_field_value,
            raw_chunk_text,
            accumulated_text=accumulated_chunk_text,
            target_field=target_field,
            apply_mode=chunk_apply_mode,
            max_units=chunk_limit_units,
        )
        chunk_units = estimate_text_units(chunk_text)
        chunk_was_trimmed = chunk_text != raw_chunk_text
        chunk_was_skipped = not bool(chunk_text)
        was_trimmed = was_trimmed or chunk_was_trimmed
        if chunk_text:
            chunk_outputs.append(chunk_text)
        if raw_chunk_text == str(stream_result.get("text") or "").strip():
            raw_requests.append(
                {
                    "chunk_index": index,
                    "request_body": stream_result["request_body"],
                    "endpoint_url": stream_result["url"],
                    "raw_response": None,
                    "streamed": True,
                }
            )
        chunk_debug.append(
            {
                "chunk_index": index,
                "chunk_target_units": chunk_target,
                "output_units_before_trim": raw_chunk_units,
                "output_units": chunk_units,
                "was_trimmed": chunk_was_trimmed,
                "was_skipped": chunk_was_skipped,
                "text": chunk_text,
            }
        )

    generated_text_before_final_trim = "\n\n".join(part for part in chunk_outputs if part.strip()).strip()
    final_limit_units = _draft_total_unit_limit(resolved_target_units)
    generated_text = _sanitize_generated_continuation(
        current_field_value,
        generated_text_before_final_trim,
        accumulated_text=None,
        target_field=target_field,
        apply_mode=apply_mode,
        max_units=final_limit_units,
    )
    was_trimmed = was_trimmed or generated_text != generated_text_before_final_trim

    if not generated_text:
        raise AIInvocationError("模型没有产出新的续写内容，返回结果与已有正文重复。")

    generated_units = estimate_text_units(generated_text)
    result.update(
        {
            "generated_text": generated_text,
            "generated_units_before_trim": generated_units_before_trim,
            "generated_units": generated_units,
            "was_trimmed": was_trimmed,
            "chunks": chunk_debug,
            "refinement_suggestion": _refinement_suggestion(generated_units, resolved_target_units),
            "draft_only": True,
        }
    )
    if planning_payload is not None:
        result["reasoner_payload"] = {
            "request_body": planning_payload["request_body"],
            "endpoint_url": planning_payload["url"],
            "raw_response": planning_payload["raw_response"],
        }
    try:
        store_latest_ai_draft_text(db, chapter, generated_text)
    except Exception:
        logger.exception("store_latest_ai_draft_text_failed chapter_id=%s mode=stream", chapter.id)
    yield {"type": "final", "response": result}


def refine_generation_draft(
    db: Session,
    *,
    book: Book,
    chapter: Chapter,
    current_user: User,
    module: AIModule,
    user_prompt: str,
    target_field: TargetField,
    apply_mode: ApplyMode,
    target_units: Optional[int],
    previous_chapters: int,
    character_limit: int,
    system_prompt_override: Optional[str],
    planning_text: Optional[str],
    draft_text: str,
    adjustment_mode: Literal["expand", "trim"],
) -> dict[str, Any]:
    config, fallback_from_module = _resolve_generation_config(
        db,
        module=module,
        current_user=current_user,
        book=book,
    )
    resolved_target_units = target_units or _default_target_units(module, target_field)
    preview_context = build_prompt_context(
        db,
        book=book,
        chapter=chapter,
        current_user=current_user,
        module=module,
        target_field=target_field,
        apply_mode=apply_mode,
        user_prompt=user_prompt,
        previous_chapters=previous_chapters,
        character_limit=character_limit,
        target_units=resolved_target_units,
        config=config,
        system_prompt_override=system_prompt_override,
        planning_text=planning_text,
    )

    draft_value = str(draft_text or "").strip()
    if not draft_value:
        raise AIInvocationError("没有可调整的草稿内容。")

    actual_units = estimate_text_units(draft_value)
    if adjustment_mode == "expand":
        adjust_instruction = (
            f"请在不改变已写内容核心事件的前提下，将这份草稿自然扩写到约 {resolved_target_units} 字。"
            "补足动作、环境、心理、对话承接与转场，让结尾自然，不要另起标题。"
            "输出完整修订后的全文，不要解释。"
        )
    else:
        adjust_instruction = (
            f"请在保留核心剧情、人物状态和关键信息的前提下，将这份草稿自然精简到约 {resolved_target_units} 字。"
            "不要生硬截断，不要列提纲，不要加标题。"
            "输出完整修订后的全文，不要解释。"
        )

    user_sections = [
        f"[当前章节]\n标题：{chapter.title}\n大纲：{_safe_text(chapter.outline, '暂无大纲')}\n摘要：{_safe_text(chapter.summary, '暂无摘要')}",
        f"[远期摘要]\n{preview_context.context_sections.get('long_term_summary') or '暂无远期摘要。'}",
        f"[世界观补充]\n{preview_context.context_sections.get('world_bible') or '暂无世界观补充。'}",
        "[关联人物卡 JSON]\n" + json.dumps(preview_context.related_characters, ensure_ascii=False, indent=2),
    ]
    if preview_context.previous_chapters:
        previous_text = "\n\n".join(
            f"### {item['title']}\n{_safe_text(item['content'], '')}" for item in preview_context.previous_chapters
        )
        user_sections.append(
            f"[{_previous_chapters_heading(previous_chapters, len(preview_context.previous_chapters))}]\n{previous_text}"
        )

    continuation_anchor = preview_context.context_sections.get("continuation_anchor")
    if continuation_anchor:
        user_sections.append(
            "[当前续写落点]\n"
            "以下片段是当前正文结尾，修订后的草稿必须从它之后开始，不能重复前文：\n"
            f"{continuation_anchor}"
        )

    user_sections.extend(
        [
            f"[原始用户要求]\n{_safe_text(user_prompt, '请根据上下文继续创作，不偏离已知设定。')}",
            f"[本次二次调整]\n{adjust_instruction}",
            f"[待调整草稿]\n{draft_value}",
        ]
    )
    if planning_text:
        user_sections.append(f"[写前规划]\n{planning_text}")

    messages = [
        {
            "role": "system",
            "content": (
                f"{preview_context.system_prompt}\n\n"
                "[二次调整硬规则]\n"
                "你正在修改一份已生成的草稿。\n"
                "必须输出修订后的完整全文。\n"
                "不要输出任何解释、标题、备注、字数说明或 Markdown。"
            ),
        },
        {"role": "user", "content": "\n\n".join(user_sections)},
    ]

    payload = call_openai_compatible_chat(
        config,
        messages=messages,
        max_tokens_override=_derive_output_max_tokens(max(resolved_target_units, actual_units), config),
    )
    refined_raw_text = payload["text"].strip()
    refined_limit_units = (
        resolved_target_units
        if adjustment_mode == "trim"
        else _draft_total_unit_limit(resolved_target_units)
    )
    refined_text = _sanitize_generated_continuation(
        chapter.content or "",
        refined_raw_text,
        accumulated_text=None,
        target_field=target_field,
        apply_mode=apply_mode,
        max_units=refined_limit_units,
    )
    if not refined_text:
        raise AIInvocationError("二次调整后没有得到可用内容。")

    refined_units = estimate_text_units(refined_text)
    return {
        "module": module.value,
        "target_field": target_field,
        "apply_mode": apply_mode,
        "target_units": resolved_target_units,
        "planning_text": planning_text,
        "context_preview": preview_context.to_dict(),
        "generated_text": refined_text,
        "generated_units_before_trim": estimate_text_units(refined_raw_text),
        "generated_units": refined_units,
        "was_trimmed": refined_text != refined_raw_text,
        "draft_only": True,
        "adjustment_mode": adjustment_mode,
        "refinement_suggestion": _refinement_suggestion(refined_units, resolved_target_units),
        "ai_config": config.public_dict(),
        "ai_config_fallback_from": fallback_from_module.value if fallback_from_module else None,
        "llm_call": {
            "request_body": payload["request_body"],
            "endpoint_url": payload["url"],
            "raw_response": payload["raw_response"],
        },
    }


def apply_generation_draft(
    db: Session,
    *,
    book: Book,
    chapter: Chapter,
    current_user: User,
    module: AIModule,
    user_prompt: str,
    target_field: TargetField,
    apply_mode: ApplyMode,
    target_units: Optional[int],
    previous_chapters: int,
    character_limit: int,
    system_prompt_override: Optional[str],
    planning_text: Optional[str],
    generated_text: str,
    store_snapshot: bool,
) -> dict[str, Any]:
    config, _fallback_from_module = _resolve_generation_config(
        db,
        module=module,
        current_user=current_user,
        book=book,
    )
    return _apply_generated_text(
        db,
        book=book,
        chapter=chapter,
        current_user=current_user,
        module=module,
        user_prompt=user_prompt,
        target_field=target_field,
        apply_mode=apply_mode,
        target_units=target_units,
        previous_chapters=previous_chapters,
        character_limit=character_limit,
        system_prompt_override=system_prompt_override,
        planning_text=planning_text,
        generated_text=generated_text,
        config=config,
        context_preview=None,
        generated_units_before_trim=estimate_text_units(generated_text),
        was_trimmed=False,
        raw_requests=[],
        store_snapshot=store_snapshot,
    )


def run_world_extraction(
    db: Session,
    *,
    book: Book,
    chapter: Chapter,
    current_user: User,
    dry_run: bool,
    update_world_bible: bool,
) -> dict[str, Any]:
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

    existing_characters = db.execute(
        select(Character).where(Character.book_id == book.id).order_by(Character.name.asc())
    ).scalars().all()
    character_payload = call_openai_compatible_chat(
        character_config,
        messages=_world_character_prompt(book, chapter, existing_characters),
    )
    character_json = _extract_json_block(character_payload["text"])
    if not isinstance(character_json, dict):
        raise AIInvocationError("Character extraction response must be a JSON object.")

    extracted_characters = character_json.get("characters") or []
    if not isinstance(extracted_characters, list):
        extracted_characters = []
    world_facts = _string_list(character_json.get("world_facts"))

    existing_by_name = {_normalized_name(item.name): item for item in existing_characters}
    merged_character_payloads: list[dict[str, Any]] = []
    created_character_count = 0
    updated_character_count = 0
    chapter_number = chapter.sequence_number or chapter.sort_order or chapter.id
    chapter_label = f"第{chapter_number}章"
    chapter_title = chapter.title or chapter_label

    for raw_item in extracted_characters:
        if not isinstance(raw_item, dict):
            continue
        name = _clean_character_name(raw_item.get("name"))
        if not name:
            continue

        normalized = _normalized_name(name)
        if not normalized:
            continue
        aliases = _string_list(raw_item.get("aliases"))
        traits = _string_list(raw_item.get("traits"))
        incoming_timeline_entries = normalize_character_timeline_entries(raw_item.get("timeline_entries"))
        if incoming_timeline_entries:
            stamped_timeline_entries = []
            for item in incoming_timeline_entries:
                entry = dict(item)
                entry["chapter_number"] = chapter_number
                entry["chapter_label"] = chapter_label
                entry["chapter_title"] = chapter_title
                stamped_timeline_entries.append(entry)
        else:
            stamped_timeline_entries = []
        existing = existing_by_name.get(normalized)
        first_appearance_chapter_id, last_appearance_chapter_id = _merge_appearance_chapter_ids(
            existing,
            chapter.id,
        )
        existing_timeline_entries = (
            (existing.card_json or {}).get("timeline_entries")
            if existing and isinstance(existing.card_json, dict)
            else []
        )

        merged_payload = {
            "name": name if existing is None else existing.name,
            "aliases": _merge_unique_strings(existing.aliases if existing else [], aliases),
            "role_label": str(raw_item.get("role_label") or (existing.role_label if existing else "")).strip() or None,
            "description": str(raw_item.get("description") or (existing.description if existing else "")).strip() or None,
            "traits": _merge_unique_strings(existing.traits if existing else [], traits),
            "goals": str(raw_item.get("goals") or (existing.goals if existing else "")).strip() or None,
            "notes": str(raw_item.get("notes") or (existing.notes if existing else "")).strip() or None,
            "first_appearance_chapter_id": first_appearance_chapter_id,
            "last_appearance_chapter_id": last_appearance_chapter_id,
            "is_active": True,
            "card_json": merge_character_card_json(
                {**(existing.card_json or {} if existing else {}), **raw_item},
                life_statuses=(raw_item.get("life_statuses") if isinstance(raw_item, dict) else None),
                timeline_entries=[*(existing_timeline_entries or []), *stamped_timeline_entries],
            ),
        }
        merged_character_payloads.append(merged_payload)

        if dry_run:
            continue

        if existing is None:
            existing = Character(book_id=book.id, name=name)
            created_character_count += 1
        else:
            updated_character_count += 1

        existing.aliases = merged_payload["aliases"]
        existing.role_label = merged_payload["role_label"]
        existing.description = merged_payload["description"]
        existing.traits = merged_payload["traits"]
        existing.goals = merged_payload["goals"]
        existing.notes = merged_payload["notes"]
        existing.first_appearance_chapter_id = merged_payload["first_appearance_chapter_id"]
        existing.last_appearance_chapter_id = merged_payload["last_appearance_chapter_id"]
        existing.is_active = True
        existing.card_json = merged_payload["card_json"]
        db.add(existing)
        db.flush()
        existing_by_name[normalized] = existing

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

    relation_payload = call_openai_compatible_chat(
        relation_config,
        messages=_world_relation_prompt(book, chapter, merged_character_payloads or [_serialize_character(item) for item in existing_characters]),
    )
    relation_json = _extract_json_block(relation_payload["text"])
    if not isinstance(relation_json, dict):
        raise AIInvocationError("Relation extraction response must be a JSON object.")

    extracted_relations = relation_json.get("relations") or []
    if not isinstance(extracted_relations, list):
        extracted_relations = []

    merged_relation_payloads: list[dict[str, Any]] = []
    created_relation_count = 0
    updated_relation_count = 0

    for raw_item in extracted_relations:
        if not isinstance(raw_item, dict):
            continue
        source_name = _normalized_name(str(raw_item.get("source_name") or ""))
        target_name = _normalized_name(str(raw_item.get("target_name") or ""))
        relation_type = _localize_relation_text(raw_item.get("relation_type")) or ""
        if not source_name or not target_name or not relation_type or source_name == target_name:
            continue

        source_character = existing_by_name.get(source_name)
        target_character = existing_by_name.get(target_name)
        if source_character is None or target_character is None:
            continue

        merged_payload = {
            "source_character_id": source_character.id if source_character.id else source_character.id,
            "source_name": source_character.name,
            "target_character_id": target_character.id if target_character.id else target_character.id,
            "target_name": target_character.name,
            "relation_type": relation_type,
            "label": _localize_relation_text(raw_item.get("label")),
            "description": normalize_relation_description(raw_item.get("description")),
            "strength": _safe_parse_relation_strength(raw_item.get("strength")),
            "is_bidirectional": _parse_booleanish(raw_item.get("is_bidirectional")),
        }
        merged_relation_payloads.append(merged_payload)

        if dry_run:
            continue

        existing_relation = _select_existing_relation(
            db,
            book_id=book.id,
            source_character_id=source_character.id,
            target_character_id=target_character.id,
            relation_type=relation_type,
        )

        if existing_relation is None:
            existing_relation = Relation(
                book_id=book.id,
                source_character_id=source_character.id,
                target_character_id=target_character.id,
                relation_type=relation_type,
            )
            created_relation_count += 1
        else:
            updated_relation_count += 1

        existing_relation.label = merged_payload["label"]
        existing_relation.description = merged_payload["description"]
        existing_relation.strength = merged_payload["strength"]
        existing_relation.is_bidirectional = merged_payload["is_bidirectional"]
        db.add(existing_relation)

    appended_world_facts: list[str] = []
    if update_world_bible:
        existing_lines = [line.strip() for line in (book.world_bible or "").splitlines() if line.strip()]
        merged_world_facts, appended_world_facts = merge_world_facts(existing_lines, world_facts)
        if not dry_run and "\n".join(merged_world_facts) != "\n".join(existing_lines):
            book.world_bible = "\n".join(merged_world_facts)
            db.add(book)

    if not dry_run:
        db.commit()

    return {
        "dry_run": dry_run,
        "chapter_id": chapter.id,
        "chapter_title": chapter.title,
        "character_config": character_config.public_dict(),
        "relation_config": relation_config.public_dict(),
        "characters": merged_character_payloads,
        "relations": merged_relation_payloads,
        "world_facts": world_facts,
        "world_facts_appended": appended_world_facts,
        "created_character_count": created_character_count,
        "updated_character_count": updated_character_count,
        "created_relation_count": created_relation_count,
        "updated_relation_count": updated_relation_count,
        "character_payload": {
            "request_body": character_payload["request_body"],
            "endpoint_url": character_payload["url"],
            "raw_response": character_payload["raw_response"],
        },
        "relation_payload": {
            "request_body": relation_payload["request_body"],
            "endpoint_url": relation_payload["url"],
            "raw_response": relation_payload["raw_response"],
        },
    }
