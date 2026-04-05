from __future__ import annotations

import re
from typing import Any, Optional


RELATION_TYPE_LABELS = {
    "kinship": "亲属",
    "affinity": "友好",
    "hostility": "敌对",
    "authority": "权势",
    "other": "其他",
}

RELATION_IMPORTANCE_LABELS = {
    "core": "核心",
    "major": "主要",
    "minor": "次要",
    "background": "背景",
}

FACTION_STATUS_LABELS = {
    "active": "活跃",
    "former": "已退出",
}

_RELATION_KEYWORD_RULES = (
    ("kinship", ("亲属", "家人", "亲子", "母子", "母女", "父子", "父女", "夫妻", "配偶", "妻子", "丈夫", "兄弟", "姐妹", "手足", "血缘", "亲人", "监护")),
    ("affinity", ("朋友", "同伴", "伙伴", "搭档", "队友", "盟友", "同盟", "知己", "合作", "结盟", "恋人", "情侣", "爱慕", "暧昧", "信任", "喜欢", "心动")),
    ("hostility", ("敌", "敌对", "敌人", "对手", "宿敌", "仇", "竞争", "冲突", "报复", "追杀", "追捕", "防备", "背叛", "猜忌")),
    ("authority", ("师徒", "师生", "导师", "学生", "徒弟", "师父", "弟子", "上下级", "上司", "下属", "雇佣", "主从", "君臣", "监护")),
)

_RELATION_TRANSLATIONS = {
    "friend": "朋友",
    "friends": "朋友",
    "ally": "盟友",
    "allies": "盟友",
    "companion": "同伴",
    "partner": "搭档",
    "teammate": "队友",
    "lover": "恋人",
    "mentor student": "师徒",
    "rival": "对手",
    "enemy": "敌人",
    "mentor": "导师",
    "student": "学生",
    "teacher student": "师徒",
    "master disciple": "师徒",
    "parent child": "亲子",
    "father son": "父子",
    "father daughter": "父女",
    "mother son": "母子",
    "mother daughter": "母女",
    "spouse": "配偶",
    "boss subordinate": "上下级",
    "leader subordinate": "上下级",
    "employer employee": "雇佣关系",
}


def canonical_relation_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def localize_relation_label(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    canonical = canonical_relation_text(text)
    if canonical in _RELATION_TRANSLATIONS:
        return _RELATION_TRANSLATIONS[canonical]
    return text


def normalize_relation_type(value: Any, *, fallback: str = "other") -> str:
    text = canonical_relation_text(localize_relation_label(value))
    if not text:
        return fallback
    if text in RELATION_TYPE_LABELS:
        return text
    for relation_type, keywords in _RELATION_KEYWORD_RULES:
        if any(keyword in text for keyword in keywords):
            return relation_type
    return fallback


def relation_type_label(value: Any) -> str:
    normalized = normalize_relation_type(value, fallback="other")
    return RELATION_TYPE_LABELS.get(normalized, RELATION_TYPE_LABELS["other"])


def normalize_relation_label(value: Any) -> Optional[str]:
    text = localize_relation_label(value)
    if not text:
        return None
    canonical = canonical_relation_text(text)
    if canonical in RELATION_TYPE_LABELS:
        text = RELATION_TYPE_LABELS[canonical]
    cleaned = re.sub(r"\s+", " ", text).strip()
    return cleaned[:128] if cleaned else None


def normalize_relation_importance(value: Any, *, fallback: str = "major") -> str:
    raw = canonical_relation_text(value)
    if raw in {"core", "核心"}:
        return "core"
    if raw in {"major", "main", "primary", "重要", "主要"}:
        return "major"
    if raw in {"minor", "secondary", "次要"}:
        return "minor"
    if raw in {"background", "passerby", "npc", "路人", "背景"}:
        return "background"
    return fallback


def relation_importance_label(value: Any) -> str:
    normalized = normalize_relation_importance(value)
    return RELATION_IMPORTANCE_LABELS.get(normalized, RELATION_IMPORTANCE_LABELS["major"])


def normalize_faction_status(value: Any, *, fallback: str = "active") -> str:
    raw = canonical_relation_text(value)
    if raw in {"former", "inactive", "left", "已退出", "前成员"}:
        return "former"
    if raw in {"active", "current", "现成员", "活跃"}:
        return "active"
    return fallback


def faction_status_label(value: Any) -> str:
    normalized = normalize_faction_status(value)
    return FACTION_STATUS_LABELS.get(normalized, FACTION_STATUS_LABELS["active"])
