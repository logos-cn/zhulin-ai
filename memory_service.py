from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Optional
from xml.sax.saxutils import escape

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from database import SessionLocal
from models import (
    AIModule,
    Book,
    Chapter,
    ChapterNodeType,
    ChapterEpisodicMemory,
    SemanticKnowledgeBase,
    User,
)


logger = logging.getLogger("bamboo_ai.memory")

MEMORY_CONSOLIDATION_EXECUTOR = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="memory-consolidate",
)
_MEMORY_CONSOLIDATION_LOCK = threading.Lock()
_MEMORY_CONSOLIDATION_FUTURES: set[Future] = set()
_MAX_SUMMARY_CHARS = 200
_MAX_SEMANTIC_FACTS = 3
_DERIVED_STYLE_SUMMARY_KEY = "derived_style_summary"
_DERIVED_STYLE_SUMMARY_UPDATED_AT_KEY = "derived_style_summary_updated_at"


@dataclass
class DynamicMemoryContext:
    recent_summaries: list[str]
    immediate_context: str
    semantic_rules: list[str]


def _clean_text(value: Optional[str]) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _truncate_chars(value: str, limit: int) -> str:
    normalized = _clean_text(value)
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit].rstrip()


def _normalize_character_csv(value: str) -> str:
    seen: set[str] = set()
    names: list[str] = []
    for raw_item in value.replace("，", ",").replace("、", ",").split(","):
        item = _clean_text(raw_item)
        if not item or item in seen:
            continue
        seen.add(item)
        names.append(item)
    return ",".join(names)


def _extract_xml_field(raw_text: str, root_tag: str, field_name: str) -> str:
    text = _clean_text(raw_text)
    if not text:
        return ""

    root = None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        start = text.find(f"<{root_tag}>")
        end = text.rfind(f"</{root_tag}>")
        if start >= 0 and end >= start:
            chunk = text[start : end + len(root_tag) + 3]
            try:
                root = ET.fromstring(chunk)
            except ET.ParseError:
                return ""
    if root is None:
        return ""

    node = root.find(field_name)
    if node is None:
        return ""
    return _clean_text("".join(node.itertext()))


def _parse_semantic_fact_lines(raw_text: str) -> list[tuple[str, str]]:
    facts: list[tuple[str, str]] = []
    seen_entities: set[str] = set()
    for raw_line in _clean_text(raw_text).splitlines():
        line = raw_line.lstrip("-*0123456789.、 ").strip()
        if not line:
            continue
        if "：" in line:
            entity_name, fact = line.split("：", 1)
        elif ":" in line:
            entity_name, fact = line.split(":", 1)
        else:
            continue
        entity_name = _clean_text(entity_name)
        fact = _clean_text(fact)
        if not entity_name or not fact or entity_name in seen_entities:
            continue
        seen_entities.add(entity_name)
        facts.append((entity_name, fact))
        if len(facts) >= _MAX_SEMANTIC_FACTS:
            break
    return facts


def resolve_style_anchor(_db: Session, book: Book) -> str:
    return _clean_text(book.global_style_prompt)


def get_derived_style_summary(book: Book) -> tuple[str, str]:
    extra_data = book.extra_data if isinstance(book.extra_data, dict) else {}
    content = _clean_text(extra_data.get(_DERIVED_STYLE_SUMMARY_KEY))
    updated_at = _clean_text(extra_data.get(_DERIVED_STYLE_SUMMARY_UPDATED_AT_KEY))
    return content, updated_at


def build_deepseek_memory_prompt(
    *,
    style_anchor: str,
    recent_summaries: list[str],
    immediate_context: str,
    semantic_rules: list[str],
    character_cards: list[dict[str, Any]],
    current_outline: str,
) -> str:
    recent_summaries_block = "\n".join(
        escape(_clean_text(item)) for item in recent_summaries if _clean_text(item)
    )
    semantic_rules_block = "\n".join(
        escape(_clean_text(item)) for item in semantic_rules if _clean_text(item)
    )
    compact_character_cards: list[dict[str, Any]] = []
    for item in character_cards:
        if not isinstance(item, dict):
            continue
        compact: dict[str, Any] = {}
        for key in (
            "name",
            "aliases",
            "role_label",
            "biography",
            "description",
            "short_term_goal",
            "long_term_goal",
            "motivation",
            "personality",
            "appearance",
            "weakness",
            "life_statuses",
            "current_location",
            "current_status",
            "current_focus",
            "latest_timeline_entry",
            "timeline_entries",
        ):
            value = item.get(key)
            if value in (None, "", [], {}):
                continue
            if key == "timeline_entries" and isinstance(value, list):
                value = value[-3:]
            compact[key] = value
        biography = _clean_text(compact.get("biography"))
        description = _clean_text(compact.get("description"))
        if biography and description and biography == description:
            compact.pop("description", None)
        if compact.get("name"):
            compact_character_cards.append(compact)
    character_cards_block = escape(json.dumps(compact_character_cards, ensure_ascii=False, indent=2))
    return (
        "<system_instructions>\n"
        "你是一位顶尖的专业长篇小说家。请基于提供的记忆上下文，将当前大纲扩写为引人入胜的完整章节。\n"
        "严格遵循作者的文风范本，并在世界观与角色设定上保持绝对准确，禁止偏离已有设定。\n"
        "必须严格遵守人物卡中的身份、目标、状态、经历与行为逻辑，禁止擅自篡改或凭空补设。\n"
        "</system_instructions>\n\n"
        "<style_anchor>\n"
        f"{escape(_clean_text(style_anchor))}\n"
        "</style_anchor>\n\n"
        "<character_cards>\n"
        f"{character_cards_block}\n"
        "</character_cards>\n\n"
        "<episodic_memory>\n"
        "  <recent_summaries>\n"
        f"{recent_summaries_block}\n"
        "  </recent_summaries>\n"
        "  <immediate_context>\n"
        f"{escape(_clean_text(immediate_context))}\n"
        "  </immediate_context>\n"
        "</episodic_memory>\n\n"
        "<semantic_rules>\n"
        f"{semantic_rules_block}\n"
        "</semantic_rules>\n\n"
        "<current_task>\n"
        "  <outline>\n"
        f"{escape(_clean_text(current_outline))}\n"
        "  </outline>\n"
        "  <execution>\n"
        "  请紧承 <immediate_context> 的结尾，严格参考 <character_cards>、<semantic_rules> 与 <outline> 的约束撰写本章正文。无需输出任何解释，直接输出正文。\n"
        "  </execution>\n"
        "</current_task>"
    )


def retrieve_dynamic_context(
    db: Session,
    *,
    book_id: int,
    current_outline: str,
    current_chapter_seq: int,
) -> DynamicMemoryContext:
    ordered_rows = db.execute(
        select(Chapter.id, Chapter.sequence_number, Chapter.sort_order)
        .where(
            Chapter.book_id == book_id,
            Chapter.node_type.in_([ChapterNodeType.CHAPTER, ChapterNodeType.SCENE]),
        )
        .order_by(
            Chapter.sequence_number.is_(None).asc(),
            Chapter.sequence_number.asc(),
            Chapter.sort_order.asc(),
            Chapter.id.asc(),
        )
    ).all()

    indexed_ids: list[int] = []
    for row in ordered_rows:
        chapter_id = getattr(row, "id", None)
        if isinstance(chapter_id, int):
            indexed_ids.append(chapter_id)

    if not indexed_ids:
        return DynamicMemoryContext(recent_summaries=[], immediate_context="", semantic_rules=[])

    current_index = max(int(current_chapter_seq or 1) - 1, 0)
    summary_start_index = max(0, current_index - 4)
    summary_end_index = max(-1, current_index - 2)
    immediate_index = current_index - 1

    summary_ids = indexed_ids[summary_start_index : summary_end_index + 1] if summary_end_index >= summary_start_index else []
    immediate_id = indexed_ids[immediate_index] if immediate_index >= 0 else None
    target_ids = [chapter_id for chapter_id in [*summary_ids, immediate_id] if isinstance(chapter_id, int)]

    chapters = db.execute(
        select(Chapter)
        .options(selectinload(Chapter.episodic_memory))
        .where(Chapter.id.in_(target_ids))
    ).scalars().all()
    chapter_map = {chapter.id: chapter for chapter in chapters}

    immediate_context = ""
    if immediate_id is not None and immediate_id in chapter_map:
        immediate_context = _clean_text(chapter_map[immediate_id].content)

    recent_summaries: list[str] = []
    for chapter_id in summary_ids:
        chapter = chapter_map.get(chapter_id)
        if chapter is None:
            continue
        summary_text = _clean_text(
            chapter.episodic_memory.summary if chapter.episodic_memory else chapter.summary
        )
        if summary_text:
            recent_summaries.append(summary_text)

    semantic_rows = db.execute(
        select(SemanticKnowledgeBase)
        .where(
            SemanticKnowledgeBase.book_id == book_id,
            func.instr(current_outline or "", SemanticKnowledgeBase.entity_name) > 0,
        )
        .order_by(SemanticKnowledgeBase.updated_at.desc(), SemanticKnowledgeBase.id.desc())
    ).scalars().all()
    semantic_rules = [_clean_text(item.core_fact) for item in semantic_rows if _clean_text(item.core_fact)]

    return DynamicMemoryContext(
        recent_summaries=recent_summaries,
        immediate_context=immediate_context,
        semantic_rules=semantic_rules,
    )


def _resolve_memory_ai_config(db: Session, owner: User, book: Book):
    import ai_service

    return ai_service.resolve_ai_config_with_fallback(
        db,
        [AIModule.SUMMARY, AIModule.CO_WRITING, AIModule.OUTLINE_EXPANSION],
        owner,
        book,
    )


def _call_memory_chat(
    db: Session,
    *,
    book: Book,
    owner: User,
    messages: list[dict[str, str]],
    max_tokens_override: int,
) -> Optional[dict[str, object]]:
    import ai_service

    try:
        config = _resolve_memory_ai_config(db, owner, book)
    except Exception:
        logger.exception("memory_ai_config_resolve_failed book_id=%s", book.id)
        return None

    effective_max_tokens = int(max_tokens_override or 0)
    model_name = str(getattr(config, "model_name", "") or "").strip().lower()
    if "reasoner" in model_name or "reasoning" in model_name:
        effective_max_tokens = max(effective_max_tokens, 4096)
    else:
        effective_max_tokens = max(effective_max_tokens, 256)

    try:
        payload = ai_service.call_openai_compatible_chat(
            config,
            messages=messages,
            max_tokens_override=effective_max_tokens,
        )
    except Exception:
        logger.exception(
            "memory_ai_call_failed book_id=%s model_name=%s",
            book.id,
            config.model_name,
        )
        return None

    return {
        "config": config,
        "payload": payload,
    }


def extract_chapter_episodic_memory(
    db: Session,
    *,
    book: Book,
    owner: User,
    chapter: Chapter,
) -> Optional[dict[str, str]]:
    chapter_text = _clean_text(chapter.content)
    if not chapter_text:
        return None

    messages = [
        {
            "role": "system",
            "content": (
                "你是小说章节记忆压缩器。"
                "请只输出 XML，不要输出任何解释。"
                "严格按如下结构输出："
                "<memory><summary>不超过200字的客观剧情摘要</summary>"
                "<characters>出场核心角色名，使用英文逗号分隔</characters></memory>"
            ),
        },
        {
            "role": "user",
            "content": (
                f"书名：{book.title}\n"
                f"章节：{chapter.title}\n"
                "请提炼这章正文的核心剧情与核心出场人物。\n"
                f"正文：\n{chapter_text}"
            ),
        },
    ]
    result = _call_memory_chat(
        db,
        book=book,
        owner=owner,
        messages=messages,
        max_tokens_override=256,
    )
    if result is None:
        return None

    payload = result["payload"]
    raw_text = str(payload["text"] or "").strip()
    summary = _truncate_chars(_extract_xml_field(raw_text, "memory", "summary"), _MAX_SUMMARY_CHARS)
    involved_characters = _normalize_character_csv(_extract_xml_field(raw_text, "memory", "characters"))
    if not summary:
        logger.warning(
            "episodic_memory_parse_failed chapter_id=%s response_preview=%s",
            chapter.id,
            raw_text[:240],
        )
        return None

    return {
        "summary": summary,
        "involved_characters": involved_characters,
    }


def extract_semantic_revision_facts(
    db: Session,
    *,
    book: Book,
    owner: User,
    chapter: Chapter,
    draft_text: str,
) -> list[tuple[str, str]]:
    final_text = _clean_text(chapter.content)
    draft_value = _clean_text(draft_text)
    if not draft_value or not final_text or draft_value == final_text:
        return []

    messages = [
        {
            "role": "system",
            "content": (
                "你是小说设定复盘器。"
                "对比 AI 初稿与作者定稿，只提炼作者修改时重点强调的稳定设定。"
                "最多输出3行。"
                "每行格式必须是“实体名：事实描述”。"
                "不要输出序号、解释、前言、总结或额外文字。"
            ),
        },
        {
            "role": "user",
            "content": (
                "对比 AI 初稿和作者定稿。请提取出作者在修改中重点强调的核心人物身份、性格设定或通用世界观规则"
                "（不超过3条）。请以客观陈述句输出，格式为“实体名：事实描述”。\n\n"
                f"AI 初稿：\n{draft_value}\n\n"
                f"作者定稿：\n{final_text}"
            ),
        },
    ]
    result = _call_memory_chat(
        db,
        book=book,
        owner=owner,
        messages=messages,
        max_tokens_override=320,
    )
    if result is None:
        return []

    payload = result["payload"]
    raw_text = str(payload["text"] or "").strip()
    facts = _parse_semantic_fact_lines(raw_text)
    if not facts and raw_text:
        logger.warning(
            "semantic_memory_parse_failed chapter_id=%s response_preview=%s",
            chapter.id,
            raw_text[:240],
        )
    return facts


def summarize_book_derived_style(
    db: Session,
    *,
    book: Book,
    owner: User,
) -> str:
    chapters = db.execute(
        select(Chapter)
        .where(Chapter.book_id == book.id)
        .order_by(Chapter.updated_at.desc(), Chapter.id.desc())
    ).scalars().all()
    samples: list[str] = []
    for chapter in chapters:
        content = _clean_text(chapter.content)
        if not content:
            continue
        samples.append(f"【{chapter.title or '未命名章节'}】\n{content[:2400]}")
        if len(samples) >= 4:
            break

    if not samples:
        return ""

    messages = [
        {
            "role": "system",
            "content": (
                "你是小说文风观察器。"
                "请基于给定章节正文，总结这本书当前已经形成的写作风格。"
                "只总结叙事视角、句式倾向、节奏、对白风格、动作描写、心理描写、转场方式、氛围控制等写法。"
                "不要复述剧情，不要引用角色名和世界设定，不要说空话。"
                "输出给作者看的候选写作要求，使用中文，6到10条短句，每条独立成行。"
                "不要加标题、前言、结语或解释。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"书名：{book.title}\n"
                "以下是最近章节正文，请总结已经稳定呈现出来的文风：\n\n"
                + "\n\n".join(samples)
            ),
        },
    ]
    result = _call_memory_chat(
        db,
        book=book,
        owner=owner,
        messages=messages,
        max_tokens_override=1200,
    )
    if result is None:
        return ""

    payload = result["payload"]
    text = _clean_text(str(payload["text"] or ""))
    if not text:
        logger.warning("derived_style_summary_parse_failed book_id=%s", book.id)
        return ""
    return text[:2000].rstrip()


def refresh_book_derived_style_summary(
    db: Session,
    *,
    book: Book,
    owner: User,
) -> None:
    derived_summary = summarize_book_derived_style(
        db,
        book=book,
        owner=owner,
    )
    if not derived_summary:
        return

    extra_data = dict(book.extra_data or {})
    extra_data[_DERIVED_STYLE_SUMMARY_KEY] = derived_summary
    extra_data[_DERIVED_STYLE_SUMMARY_UPDATED_AT_KEY] = datetime.now(timezone.utc).isoformat()
    book.extra_data = extra_data
    db.add(book)


def _upsert_chapter_episodic_memory(
    db: Session,
    *,
    chapter_id: int,
    summary: str,
    involved_characters: str,
) -> None:
    memory = db.execute(
        select(ChapterEpisodicMemory).where(ChapterEpisodicMemory.chapter_id == chapter_id)
    ).scalar_one_or_none()
    if memory is None:
        memory = ChapterEpisodicMemory(
            chapter_id=chapter_id,
            summary=summary,
            involved_characters=involved_characters or None,
        )
        db.add(memory)
        return

    memory.summary = summary
    memory.involved_characters = involved_characters or None
    db.add(memory)


def _upsert_semantic_facts(
    db: Session,
    *,
    book_id: int,
    facts: list[tuple[str, str]],
) -> None:
    if not facts:
        return

    existing_rows = db.execute(
        select(SemanticKnowledgeBase).where(
            SemanticKnowledgeBase.book_id == book_id,
            SemanticKnowledgeBase.entity_name.in_([entity for entity, _fact in facts]),
        )
    ).scalars().all()
    existing_map = {row.entity_name: row for row in existing_rows}

    for entity_name, fact in facts:
        normalized_entity = _clean_text(entity_name)
        normalized_fact = _clean_text(fact)
        if not normalized_entity or not normalized_fact:
            continue
        existing = existing_map.get(normalized_entity)
        if existing is None:
            existing = SemanticKnowledgeBase(
                book_id=book_id,
                entity_name=normalized_entity,
                core_fact=normalized_fact,
            )
            db.add(existing)
            existing_map[normalized_entity] = existing
            continue
        existing.core_fact = normalized_fact
        db.add(existing)


def consolidate_chapter_memory(chapter_id: int) -> None:
    db = SessionLocal()
    try:
        chapter = db.get(Chapter, chapter_id)
        if chapter is None:
            logger.warning("memory_consolidation_chapter_missing chapter_id=%s", chapter_id)
            return
        book = db.get(Book, chapter.book_id)
        if book is None:
            logger.warning("memory_consolidation_book_missing chapter_id=%s", chapter_id)
            return
        owner = db.get(User, book.owner_id)
        if owner is None:
            logger.warning("memory_consolidation_owner_missing chapter_id=%s book_id=%s", chapter_id, book.id)
            return

        chapter_text = _clean_text(chapter.content)
        if not chapter_text:
            logger.info("memory_consolidation_skipped_empty_content chapter_id=%s", chapter_id)
            return

        episodic_payload = extract_chapter_episodic_memory(
            db,
            book=book,
            owner=owner,
            chapter=chapter,
        )
        draft_text = ""
        if isinstance(chapter.extra_data, dict):
            draft_text = _clean_text(chapter.extra_data.get("last_ai_draft_text"))
        semantic_facts = extract_semantic_revision_facts(
            db,
            book=book,
            owner=owner,
            chapter=chapter,
            draft_text=draft_text,
        )

        if episodic_payload is not None:
            _upsert_chapter_episodic_memory(
                db,
                chapter_id=chapter.id,
                summary=episodic_payload["summary"],
                involved_characters=episodic_payload["involved_characters"],
            )
        if semantic_facts:
            _upsert_semantic_facts(
                db,
                book_id=book.id,
                facts=semantic_facts,
            )
        refresh_book_derived_style_summary(
            db,
            book=book,
            owner=owner,
        )
        if episodic_payload is None and not semantic_facts:
            logger.info("memory_consolidation_style_only chapter_id=%s", chapter_id)
        db.commit()
        logger.info(
            "memory_consolidation_completed chapter_id=%s summary_saved=%s semantic_facts=%s",
            chapter_id,
            bool(episodic_payload),
            len(semantic_facts),
        )
    except Exception:
        db.rollback()
        logger.exception("memory_consolidation_failed chapter_id=%s", chapter_id)
    finally:
        db.close()


def _memory_future_done_callback(chapter_id: int, future: Future) -> None:
    with _MEMORY_CONSOLIDATION_LOCK:
        _MEMORY_CONSOLIDATION_FUTURES.discard(future)
    try:
        future.result()
    except Exception:
        logger.exception("memory_consolidation_future_failed chapter_id=%s", chapter_id)


def schedule_chapter_memory_consolidation(chapter_id: int) -> None:
    with _MEMORY_CONSOLIDATION_LOCK:
        future = MEMORY_CONSOLIDATION_EXECUTOR.submit(consolidate_chapter_memory, chapter_id)
        _MEMORY_CONSOLIDATION_FUTURES.add(future)
    future.add_done_callback(lambda done_future: _memory_future_done_callback(chapter_id, done_future))
