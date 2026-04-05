from __future__ import annotations

import re
from typing import Any, Optional


CHARACTER_LIFE_STATUS_ORDER = (
    "alive",
    "dead",
    "serious_injury",
    "minor_injury",
    "disabled",
)

CHARACTER_LIFE_STATUS_ALIASES = {
    "alive": "alive",
    "活着": "alive",
    "存活": "alive",
    "生还": "alive",
    "dead": "dead",
    "death": "dead",
    "deceased": "dead",
    "死亡": "dead",
    "已死": "dead",
    "身亡": "dead",
    "serious_injury": "serious_injury",
    "seriously_injured": "serious_injury",
    "severe_injury": "serious_injury",
    "重伤": "serious_injury",
    "重创": "serious_injury",
    "minor_injury": "minor_injury",
    "lightly_injured": "minor_injury",
    "light_injury": "minor_injury",
    "轻伤": "minor_injury",
    "disabled": "disabled",
    "disability": "disabled",
    "crippled": "disabled",
    "残疾": "disabled",
    "残废": "disabled",
}

CHARACTER_CARD_TEXT_FIELDS = (
    "age",
    "short_term_goal",
    "long_term_goal",
    "motivation",
    "personality",
    "appearance",
    "weakness",
)


def _string_or_none(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def coerce_chapter_number(value: Any) -> Optional[int]:
    if value in (None, "", False):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None

    text = str(value).strip()
    if not text:
        return None

    match = re.search(r"(\d+)", text)
    if not match:
        return None
    number = int(match.group(1))
    return number if number > 0 else None


def normalize_character_life_statuses(raw_value: Any) -> list[str]:
    items = (
        raw_value
        if isinstance(raw_value, (list, tuple, set))
        else re.split(r"[\s,，、/|;；]+", raw_value)
        if isinstance(raw_value, str)
        else []
        if raw_value is None
        else [raw_value]
    )
    values: list[str] = []
    custom_values: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.lower()
        normalized = CHARACTER_LIFE_STATUS_ALIASES.get(key)
        if normalized:
            if normalized not in values:
                values.append(normalized)
            continue
        if text not in custom_values:
            custom_values.append(text)
    if "dead" in values and "alive" in values:
        values.remove("alive")
    ordered_values = [item for item in CHARACTER_LIFE_STATUS_ORDER if item in values]
    return ordered_values + custom_values


def normalize_character_timeline_entries(raw_value: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_value, (list, tuple)):
        return []

    normalized_entries: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    for item in raw_value:
        if not isinstance(item, dict):
            continue

        chapter_title = _string_or_none(
            item.get("chapter_title")
            or item.get("chapter_name")
        )
        chapter_label = _string_or_none(
            item.get("chapter_label")
            or chapter_title
        )
        chapter_number = coerce_chapter_number(
            item.get("chapter_number")
            or item.get("chapter")
            or item.get("sequence_number")
            or item.get("chapter_index")
            or item.get("order")
            or chapter_label
        )
        if chapter_number is None:
            continue

        event = _string_or_none(
            item.get("event")
            or item.get("summary")
            or item.get("action")
            or item.get("doing")
        )
        location = _string_or_none(item.get("location") or item.get("place"))
        status = _string_or_none(item.get("status") or item.get("state"))
        notes = _string_or_none(item.get("notes") or item.get("remark") or item.get("task"))

        if not any((event, location, status, notes)):
            continue

        normalized: dict[str, Any] = {
            "chapter_number": chapter_number,
            "chapter_label": chapter_label or f"第{chapter_number}章",
        }
        if chapter_title:
            normalized["chapter_title"] = chapter_title
        if event:
            normalized["event"] = event
        if location:
            normalized["location"] = location
        if status:
            normalized["status"] = status
        if notes:
            normalized["notes"] = notes

        dedupe_key = (
            normalized["chapter_number"],
            normalized.get("event"),
            normalized.get("location"),
            normalized.get("status"),
            normalized.get("notes"),
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized_entries.append(normalized)

    normalized_entries.sort(
        key=lambda item: (
            item.get("chapter_number") or 0,
            item.get("event") or "",
            item.get("location") or "",
            item.get("status") or "",
        )
    )
    return normalized_entries


def timeline_entries_up_to_chapter(
    raw_value: Any,
    chapter_number: Optional[int],
    *,
    max_items: Optional[int] = None,
) -> list[dict[str, Any]]:
    entries = normalize_character_timeline_entries(raw_value)
    if chapter_number is not None:
        entries = [
            item
            for item in entries
            if (item.get("chapter_number") or 0) <= chapter_number
        ]
    if max_items is not None and max_items > 0:
        entries = entries[-max_items:]
    return entries


def merge_character_card_json(
    card_json: Any,
    *,
    life_statuses: Any = None,
    timeline_entries: Any = None,
) -> dict[str, Any]:
    payload = dict(card_json) if isinstance(card_json, dict) else {}

    for key in CHARACTER_CARD_TEXT_FIELDS:
        normalized_value = _string_or_none(payload.get(key))
        if normalized_value is None:
            payload.pop(key, None)
        else:
            payload[key] = normalized_value

    normalized_life_statuses = normalize_character_life_statuses(
        payload.get("life_statuses") if life_statuses is None else life_statuses
    )
    if normalized_life_statuses:
        payload["life_statuses"] = normalized_life_statuses
    else:
        payload.pop("life_statuses", None)

    normalized_timeline_entries = normalize_character_timeline_entries(
        payload.get("timeline_entries") if timeline_entries is None else timeline_entries
    )
    if normalized_timeline_entries:
        payload["timeline_entries"] = normalized_timeline_entries
    else:
        payload.pop("timeline_entries", None)

    return payload
