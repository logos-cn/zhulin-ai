from __future__ import annotations

import unittest

from main import (
    _assistant_extract_edit_proposal,
    _assistant_finalize_reply_text,
    merge_character_card_json,
    normalize_character_life_statuses,
    normalize_character_timeline_entries,
    serialize_character,
)
from models import Character


class MainCharacterHelperTests(unittest.TestCase):
    def test_normalize_character_life_statuses_deduplicates_and_resolves_aliases(self) -> None:
        self.assertEqual(
            normalize_character_life_statuses(["活着", "重伤", "alive", "死亡"]),
            ["dead", "serious_injury"],
        )

    def test_merge_character_card_json_preserves_other_fields_and_normalizes_life_statuses(self) -> None:
        merged = merge_character_card_json(
            {"importance_level": "major", "life_statuses": ["轻伤"]},
            life_statuses=["残疾", "轻伤"],
        )

        self.assertEqual(merged["importance_level"], "major")
        self.assertEqual(merged["life_statuses"], ["minor_injury", "disabled"])

    def test_merge_character_card_json_normalizes_timeline_entries(self) -> None:
        merged = merge_character_card_json(
            {"timeline_entries": [{"chapter_number": "12", "event": "在东市盯梢", "location": "东市"}]},
            timeline_entries=[
                {"chapter_number": "12", "event": "在东市盯梢", "location": "东市"},
                {"chapter_number": "18", "event": "转移到山门", "status": "潜伏调查"},
            ],
        )

        self.assertEqual(
            merged["timeline_entries"],
            [
                {"chapter_number": 12, "chapter_label": "第12章", "event": "在东市盯梢", "location": "东市"},
                {"chapter_number": 18, "chapter_label": "第18章", "event": "转移到山门", "status": "潜伏调查"},
            ],
        )

    def test_normalize_character_timeline_entries_discards_invalid_rows(self) -> None:
        entries = normalize_character_timeline_entries(
            [
                {"chapter_number": "", "event": "无效"},
                {"chapter_number": "8", "event": "进入山门", "location": "山门"},
            ]
        )

        self.assertEqual(
            entries,
            [{"chapter_number": 8, "chapter_label": "第8章", "event": "进入山门", "location": "山门"}],
        )

    def test_serialize_character_exposes_normalized_life_statuses(self) -> None:
        character = Character(
            id=3,
            book_id=8,
            name="林秋",
            description="隐居剑修，后卷入宗门纷争。",
            card_json={
                "life_statuses": ["死亡", "残疾", "alive"],
                "timeline_entries": [
                    {"chapter_number": "12", "event": "在东市盯梢", "location": "东市"},
                ],
            },
            is_active=True,
        )

        payload = serialize_character(character)

        self.assertEqual(payload["biography"], "隐居剑修，后卷入宗门纷争。")
        self.assertEqual(payload["life_statuses"], ["dead", "disabled"])
        self.assertEqual(payload["card_json"]["life_statuses"], ["dead", "disabled"])
        self.assertEqual(
            payload["timeline_entries"],
            [{"chapter_number": 12, "chapter_label": "第12章", "event": "在东市盯梢", "location": "东市"}],
        )

    def test_assistant_extract_edit_proposal_returns_visible_text_and_structured_payload(self) -> None:
        text, proposal = _assistant_extract_edit_proposal(
            "这是说明文字。\n<assistant_edit>{\"target_field\":\"content\",\"title\":\"改写正文\",\"content\":\"新的正文内容\"}</assistant_edit>"
        )

        self.assertEqual(text, "这是说明文字。")
        self.assertEqual(
            proposal,
            {
                "target_field": "content",
                "title": "改写正文",
                "content": "新的正文内容",
            },
        )

    def test_assistant_finalize_reply_text_uses_confirmation_copy_for_edit_only_reply(self) -> None:
        reply = _assistant_finalize_reply_text(
            "",
            {
                "target_field": "content",
                "title": "改写正文",
                "content": "新的正文内容",
            },
        )

        self.assertEqual(reply, "已生成正文修改建议，请确认是否接受修改。")


if __name__ == "__main__":
    unittest.main()
