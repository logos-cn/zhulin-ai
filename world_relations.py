from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from models import Relation, RelationEvent
from world_schema import normalize_relation_importance, normalize_relation_label, normalize_relation_type


def _same_event_snapshot(
    event: RelationEvent,
    *,
    chapter_id: Optional[int],
    relation_type: str,
    label: Optional[str],
    description: Optional[str],
    strength: Optional[float],
    importance_level: str,
    is_bidirectional: bool,
    event_summary: Optional[str],
) -> bool:
    return (
        event.chapter_id == chapter_id
        and normalize_relation_type(event.relation_type) == relation_type
        and normalize_relation_label(event.label) == normalize_relation_label(label)
        and (event.description or None) == (description or None)
        and event.strength == strength
        and normalize_relation_importance(event.importance_level) == importance_level
        and bool(event.is_bidirectional) == bool(is_bidirectional)
        and (event.event_summary or None) == (event_summary or None)
    )


def record_relation_event(
    db: Session,
    relation: Relation,
    *,
    chapter_id: Optional[int],
    segment_label: Optional[str],
    relation_type: str,
    label: Optional[str],
    description: Optional[str],
    strength: Optional[float],
    importance_level: str,
    is_bidirectional: bool,
    event_summary: Optional[str],
) -> Optional[RelationEvent]:
    normalized_type = normalize_relation_type(relation_type)
    normalized_importance = normalize_relation_importance(importance_level)
    normalized_label = normalize_relation_label(label)
    latest = relation.events[-1] if relation.events else None
    if latest is not None and _same_event_snapshot(
        latest,
        chapter_id=chapter_id,
        relation_type=normalized_type,
        label=normalized_label,
        description=description,
        strength=strength,
        importance_level=normalized_importance,
        is_bidirectional=is_bidirectional,
        event_summary=event_summary,
    ):
        return None

    event = RelationEvent(
        relation_id=relation.id,
        book_id=relation.book_id,
        source_character_id=relation.source_character_id,
        target_character_id=relation.target_character_id,
        chapter_id=chapter_id,
        segment_label=segment_label,
        relation_type=normalized_type,
        label=normalized_label,
        description=description,
        strength=strength,
        importance_level=normalized_importance,
        is_bidirectional=bool(is_bidirectional),
        event_summary=event_summary,
    )
    db.add(event)
    relation.events.append(event)
    return event
