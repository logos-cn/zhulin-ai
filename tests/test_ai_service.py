from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import AIConfig, AIScope, Book, Chapter, Character, Relation
from ai_service import (
    _clean_character_name,
    _merge_appearance_chapter_ids,
    _parse_booleanish,
    _previous_chapter_payloads,
    _previous_chapters_heading,
    _select_existing_relation,
    _select_related_characters,
    _serialize_character,
    _safe_parse_relation_strength,
    _world_character_prompt,
    ResolvedAIConfig,
    build_prompt_context,
    normalize_relation_description,
    run_generation,
    run_world_extraction,
    store_latest_ai_draft_text,
)
from models import (
    AIModule,
    AuthorGoldenCorpus,
    Base,
    ChapterEpisodicMemory,
    ChapterNodeType,
    SemanticKnowledgeBase,
    User,
    UserRole,
    UserStatus,
)


class AIServiceHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", future=True)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, future=True)
        Base.metadata.create_all(bind=self.engine)
        self.db = self.Session()
        self.user = User(username="tester", password_hash="x", role=UserRole.AUTHOR, status=UserStatus.ACTIVE, is_active=True)
        self.db.add(self.user)
        self.db.flush()
        self.book = Book(owner_id=self.user.id, title="测试书", global_style_prompt="")
        self.db.add(self.book)
        self.db.commit()
        self.db.refresh(self.user)
        self.db.refresh(self.book)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_clean_character_name_collapses_internal_whitespace(self) -> None:
        self.assertEqual(_clean_character_name("  李   四  "), "李 四")
        self.assertEqual(_clean_character_name(""), "")

    def test_merge_appearance_chapter_ids_keeps_earliest_and_latest(self) -> None:
        existing = Character(
            book_id=1,
            name="周奕",
            first_appearance_chapter_id=10,
            last_appearance_chapter_id=12,
            is_active=True,
        )

        first_appearance, last_appearance = _merge_appearance_chapter_ids(existing, 8)

        self.assertEqual(first_appearance, 8)
        self.assertEqual(last_appearance, 12)
        self.assertLessEqual(first_appearance, last_appearance)

    def test_parse_booleanish_handles_string_false(self) -> None:
        self.assertTrue(_parse_booleanish(True))
        self.assertFalse(_parse_booleanish(False))
        self.assertTrue(_parse_booleanish("true"))
        self.assertFalse(_parse_booleanish("false"))
        self.assertTrue(_parse_booleanish(1))
        self.assertFalse(_parse_booleanish(0))
        self.assertFalse(_parse_booleanish(""))
        self.assertFalse(_parse_booleanish(None))

    def test_safe_parse_relation_strength_handles_invalid_and_out_of_range_values(self) -> None:
        self.assertIsNone(_safe_parse_relation_strength("高"))
        self.assertIsNone(_safe_parse_relation_strength(""))
        self.assertIsNone(_safe_parse_relation_strength(None))
        self.assertIsNone(_safe_parse_relation_strength("nan"))
        self.assertEqual(_safe_parse_relation_strength("1.4"), 1.0)
        self.assertEqual(_safe_parse_relation_strength("-0.2"), 0.0)
        self.assertEqual(_safe_parse_relation_strength("0.35"), 0.35)

    def test_normalize_relation_description_prefers_first_sentence(self) -> None:
        value = "两人目前互相信任，并开始共同调查真相。第二句不应该继续保留，因为说明会过长。"
        self.assertEqual(
            normalize_relation_description(value, max_chars=24),
            "两人目前互相信任，并开始共同调查真相",
        )

    def test_select_related_characters_filters_future_timeline_entries(self) -> None:
        current_chapter = Chapter(id=20, book_id=1, title="第二十章", sequence_number=20)
        character = Character(
            id=7,
            book_id=1,
            name="周奕",
            description="谨慎的追查者。",
            is_active=True,
            card_json={
                "timeline_entries": [
                    {"chapter_number": 8, "event": "进入东市查线索", "location": "东市"},
                    {"chapter_number": 24, "event": "潜入山门地牢", "location": "山门地牢"},
                ]
            },
        )

        payload = _select_related_characters([character], {}, current_chapter, 5)

        self.assertEqual(len(payload), 1)
        self.assertEqual(
            payload[0]["timeline_entries"],
            [{"chapter_number": 8, "chapter_label": "第8章", "event": "进入东市查线索", "location": "东市"}],
        )
        self.assertEqual(payload[0]["current_location"], "东市")
        self.assertEqual(payload[0]["current_focus"], "进入东市查线索")

    def test_select_related_characters_does_not_exclude_stale_last_appearance(self) -> None:
        current_chapter = Chapter(id=20, book_id=1, title="第二十章", sequence_number=20)
        stale = Character(
            id=8,
            book_id=1,
            name="甄嬛",
            description="重要人物。",
            last_appearance_chapter_id=10,
            is_active=True,
            aliases=["熹贵妃"],
        )

        payload = _select_related_characters([stale], {}, current_chapter, 5)

        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["name"], "甄嬛")

    def test_previous_chapter_payloads_ignore_none_content(self) -> None:
        current_chapter = Chapter(id=3, book_id=self.book.id, title="第三章", sequence_number=3)
        chapters = [
            Chapter(id=1, book_id=self.book.id, title="第一章", sequence_number=1, content=None, node_type=ChapterNodeType.CHAPTER),
            Chapter(id=2, book_id=self.book.id, title="第二章", sequence_number=2, content="可用正文", node_type=ChapterNodeType.CHAPTER),
            current_chapter,
        ]

        payloads = _previous_chapter_payloads(chapters, current_chapter, limit=3)

        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["title"], "第二章")
        self.assertEqual(payloads[0]["content"], "可用正文")

    def test_serialize_character_includes_extended_card_fields(self) -> None:
        character = Character(
            id=7,
            book_id=1,
            name="周奕",
            description="谨慎的追查者。",
            is_active=True,
            card_json={
                "age": "二十三岁",
                "short_term_goal": "找到失踪信使",
                "long_term_goal": "查清灭门真相",
                "motivation": "替家人复仇",
                "personality": "冷静多疑",
                "appearance": "常穿黑色短打，左眉有疤",
                "weakness": "旧伤未愈，难以久战",
            },
        )

        payload = _serialize_character(character)

        self.assertEqual(payload["age"], "二十三岁")
        self.assertEqual(payload["short_term_goal"], "找到失踪信使")
        self.assertEqual(payload["long_term_goal"], "查清灭门真相")
        self.assertEqual(payload["motivation"], "替家人复仇")
        self.assertEqual(payload["personality"], "冷静多疑")
        self.assertEqual(payload["appearance"], "常穿黑色短打，左眉有疤")
        self.assertEqual(payload["weakness"], "旧伤未愈，难以久战")

    def test_world_character_prompt_requests_extended_card_fields(self) -> None:
        book = Book(id=1, owner_id=1, title="测试书", world_bible="门派林立")
        chapter = Chapter(id=1, book_id=1, title="第一章", content="周奕负伤潜入东市追查旧案。")

        prompt = _world_character_prompt(book, chapter, [])
        user_message = next(item["content"] for item in prompt if item["role"] == "user")

        self.assertIn('"age": "string"', user_message)
        self.assertIn('"short_term_goal": "string"', user_message)
        self.assertIn('"long_term_goal": "string"', user_message)
        self.assertIn('"motivation": "string"', user_message)
        self.assertIn('"personality": "string"', user_message)
        self.assertIn('"appearance": "string"', user_message)
        self.assertIn('"weakness": "string"', user_message)
        self.assertIn('"life_statuses": ["alive | dead | serious_injury | minor_injury | disabled"]', user_message)
        self.assertIn("Only fill age, personality, appearance, weakness, motivation", user_message)

    def test_build_prompt_context_keeps_extended_character_fields_in_related_characters(self) -> None:
        chapter = Chapter(
            book_id=self.book.id,
            title="第一章",
            sequence_number=1,
            content="周奕在东市追查旧案。",
        )
        character = Character(
            book_id=self.book.id,
            name="周奕",
            description="山门旧案唯一幸存者。",
            is_active=True,
            card_json={
                "age": "二十三岁",
                "short_term_goal": "找到失踪信使",
                "long_term_goal": "查清灭门真相",
                "motivation": "替家人复仇",
                "personality": "冷静多疑",
                "appearance": "黑衣瘦高，左眉有疤",
                "weakness": "旧伤未愈",
            },
        )
        self.db.add_all([chapter, character])
        self.db.commit()
        self.db.refresh(chapter)

        context = build_prompt_context(
            self.db,
            book=self.book,
            chapter=chapter,
            current_user=self.user,
            module=AIModule.CO_WRITING,
            target_field="content",
            apply_mode="append",
            user_prompt="继续写",
            previous_chapters=0,
            character_limit=5,
            target_units=1200,
        )

        related = context.related_characters[0]
        self.assertEqual(related["age"], "二十三岁")
        self.assertEqual(related["short_term_goal"], "找到失踪信使")
        self.assertEqual(related["long_term_goal"], "查清灭门真相")
        self.assertEqual(related["motivation"], "替家人复仇")
        self.assertEqual(related["personality"], "冷静多疑")
        self.assertEqual(related["appearance"], "黑衣瘦高，左眉有疤")
        self.assertEqual(related["weakness"], "旧伤未愈")

    def test_select_existing_relation_returns_matching_row(self) -> None:
        source = Character(book_id=self.book.id, name="甲", is_active=True)
        target = Character(book_id=self.book.id, name="乙", is_active=True)
        self.db.add_all([source, target])
        self.db.flush()
        relation = Relation(
            book_id=self.book.id,
            source_character_id=source.id,
            target_character_id=target.id,
            relation_type="夫妻",
            description="已成婚",
        )
        self.db.add(relation)
        self.db.commit()

        existing = _select_existing_relation(
            self.db,
            book_id=self.book.id,
            source_character_id=source.id,
            target_character_id=target.id,
            relation_type="夫妻",
        )

        self.assertIsNotNone(existing)
        self.assertEqual(existing.id, relation.id)

    def test_store_latest_ai_draft_text_persists_in_extra_data(self) -> None:
        chapter = Chapter(
            book_id=self.book.id,
            title="第一章",
            sequence_number=1,
            content="正文",
        )
        self.db.add(chapter)
        self.db.commit()
        self.db.refresh(chapter)

        store_latest_ai_draft_text(self.db, chapter, "这是最新 AI 草稿")
        self.db.refresh(chapter)

        self.assertEqual(chapter.extra_data["last_ai_draft_text"], "这是最新 AI 草稿")

    def test_build_prompt_context_uses_deepseek_memory_prompt_for_new_chapter_content(self) -> None:
        previous_chapters = [
            Chapter(book_id=self.book.id, title="第一章", sequence_number=1, content="第一章正文"),
            Chapter(book_id=self.book.id, title="第二章", sequence_number=2, content="第二章正文"),
            Chapter(book_id=self.book.id, title="第三章", sequence_number=3, content="第三章正文"),
            Chapter(book_id=self.book.id, title="第四章", sequence_number=4, content="第四章结尾是城门前的夜雨。"),
        ]
        current_chapter = Chapter(
            book_id=self.book.id,
            title="第五章",
            sequence_number=5,
            outline="周奕潜入夜巡司档案库，发现夜巡司统领其实是旧案知情人。",
            content="",
        )
        self.db.add_all(previous_chapters + [current_chapter])
        self.db.flush()
        self.db.add_all(
            [
                ChapterEpisodicMemory(chapter_id=previous_chapters[0].id, summary="第一章摘要", involved_characters="周奕"),
                ChapterEpisodicMemory(chapter_id=previous_chapters[1].id, summary="第二章摘要", involved_characters="周奕,沈昭"),
                ChapterEpisodicMemory(chapter_id=previous_chapters[2].id, summary="第三章摘要", involved_characters="周奕"),
                SemanticKnowledgeBase(
                    book_id=self.book.id,
                    entity_name="夜巡司",
                    core_fact="夜巡司统领曾参与掩盖山门旧案真相。",
                ),
                AuthorGoldenCorpus(
                    book_id=self.book.id,
                    content="文风要求：冷峻克制，重动作细节与心理停顿。",
                ),
                Character(
                    book_id=self.book.id,
                    name="周奕",
                    description="山门旧案幸存者。",
                    role_label="主角",
                    is_active=True,
                    card_json={
                        "personality": "冷静多疑",
                        "short_term_goal": "潜入夜巡司查案",
                    },
                ),
                Character(
                    book_id=self.book.id,
                    name="沈昭",
                    description="夜巡司暗线。",
                    role_label="盟友",
                    is_active=True,
                    card_json={
                        "personality": "谨慎克制",
                        "timeline_entries": [
                            {"chapter_number": 4, "event": "在城门前接应周奕", "location": "城门"}
                        ],
                    },
                ),
                Character(
                    book_id=self.book.id,
                    name="王五",
                    description="路人甲。",
                    role_label="路人",
                    is_active=True,
                    card_json={
                        "personality": "普通",
                    },
                ),
            ]
        )
        self.book.global_style_prompt = "最终写作要求：叙事冷静，动作描写克制。"
        self.db.add(self.book)
        self.db.commit()
        self.db.refresh(current_chapter)

        config = ResolvedAIConfig(
            id=1,
            name="DeepSeek",
            module=AIModule.OUTLINE_EXPANSION,
            source="database",
            scope="system",
            provider_name="deepseek",
            api_format="openai_v1",
            base_url="https://api.deepseek.com/v1",
            api_key="test-key",
            model_name="deepseek-chat",
            timeout_seconds=120,
            temperature=None,
            top_p=None,
            max_tokens=None,
            reasoning_effort=None,
            system_prompt_template=None,
            extra_headers={},
            extra_body={},
        )

        context = build_prompt_context(
            self.db,
            book=self.book,
            chapter=current_chapter,
            current_user=self.user,
            module=AIModule.OUTLINE_EXPANSION,
            target_field="content",
            apply_mode="replace",
            user_prompt="扩写这一章",
            previous_chapters=3,
            character_limit=5,
            target_units=2000,
            config=config,
        )

        self.assertIn("<system_instructions>", context.user_prompt)
        self.assertIn("<style_anchor>", context.user_prompt)
        self.assertIn("最终写作要求：叙事冷静，动作描写克制。", context.user_prompt)
        self.assertNotIn("文风要求：冷峻克制，重动作细节与心理停顿。", context.user_prompt)
        self.assertIn("第一章摘要", context.user_prompt)
        self.assertIn("第二章摘要", context.user_prompt)
        self.assertIn("第三章摘要", context.user_prompt)
        self.assertIn("第四章结尾是城门前的夜雨。", context.user_prompt)
        self.assertIn("夜巡司统领曾参与掩盖山门旧案真相。", context.user_prompt)
        self.assertIn("<character_cards>", context.user_prompt)
        self.assertIn("周奕", context.user_prompt)
        self.assertIn("沈昭", context.user_prompt)
        self.assertNotIn("王五", context.user_prompt)
        self.assertTrue(context.context_sections["deepseek_memory_mode"])

    def test_build_prompt_context_keeps_deepseek_memory_prompt_when_content_exists_and_override_present(self) -> None:
        previous_chapters = [
            Chapter(book_id=self.book.id, title="第一章", sequence_number=1, content="第一章正文"),
            Chapter(book_id=self.book.id, title="第二章", sequence_number=2, content="第二章正文"),
            Chapter(book_id=self.book.id, title="第三章", sequence_number=3, content="第三章正文"),
            Chapter(book_id=self.book.id, title="第四章", sequence_number=4, content="第四章结尾"),
        ]
        current_chapter = Chapter(
            book_id=self.book.id,
            title="第五章",
            sequence_number=5,
            outline="周奕在夜巡司内库继续追查旧案。",
            content="这里已经有旧正文。",
        )
        self.db.add_all(previous_chapters + [current_chapter])
        self.db.flush()
        self.db.add_all(
            [
                ChapterEpisodicMemory(chapter_id=previous_chapters[0].id, summary="第一章摘要", involved_characters="周奕"),
                ChapterEpisodicMemory(chapter_id=previous_chapters[1].id, summary="第二章摘要", involved_characters="周奕"),
                ChapterEpisodicMemory(chapter_id=previous_chapters[2].id, summary="第三章摘要", involved_characters="周奕"),
                SemanticKnowledgeBase(
                    book_id=self.book.id,
                    entity_name="夜巡司",
                    core_fact="夜巡司内库夜间只开一炷香。",
                ),
                Character(
                    book_id=self.book.id,
                    name="周奕",
                    description="追查旧案的人。",
                    role_label="主角",
                    is_active=True,
                    card_json={
                        "personality": "冷静",
                    },
                ),
            ]
        )
        self.book.global_style_prompt = "最终写作要求：节奏稳，细节准。"
        self.db.add(self.book)
        self.db.commit()
        self.db.refresh(current_chapter)

        config = ResolvedAIConfig(
            id=1,
            name="DeepSeek",
            module=AIModule.OUTLINE_EXPANSION,
            source="database",
            scope="system",
            provider_name="deepseek",
            api_format="openai_v1",
            base_url="https://api.deepseek.com/v1",
            api_key="test-key",
            model_name="deepseek-chat",
            timeout_seconds=120,
            temperature=None,
            top_p=None,
            max_tokens=None,
            reasoning_effort=None,
            system_prompt_template=None,
            extra_headers={},
            extra_body={},
        )

        context = build_prompt_context(
            self.db,
            book=self.book,
            chapter=current_chapter,
            current_user=self.user,
            module=AIModule.OUTLINE_EXPANSION,
            target_field="content",
            apply_mode="replace",
            user_prompt="扩写这一章",
            previous_chapters=3,
            character_limit=5,
            target_units=2000,
            config=config,
            system_prompt_override="补充要求：突出夜色压迫感。",
        )

        self.assertIn("<episodic_memory>", context.user_prompt)
        self.assertIn("夜巡司内库夜间只开一炷香。", context.user_prompt)
        self.assertIn("周奕", context.user_prompt)
        self.assertIn("补充要求：突出夜色压迫感。", context.system_prompt)
        self.assertTrue(context.context_sections["deepseek_memory_mode"])

    def test_build_prompt_context_preserves_core_system_rules_when_override_present(self) -> None:
        chapter = Chapter(
            book_id=self.book.id,
            title="第一章",
            sequence_number=1,
            content="周奕已经潜入东市。",
        )
        self.db.add(chapter)
        self.book.global_style_prompt = "文风要求：冷静克制。"
        self.db.add(self.book)
        self.db.commit()
        self.db.refresh(chapter)

        config = ResolvedAIConfig(
            id=1,
            name="测试配置",
            module=AIModule.CO_WRITING,
            source="database",
            scope="system",
            provider_name="openai",
            api_format="openai_v1",
            base_url="https://example.com/v1",
            api_key="test-key",
            model_name="test-model",
            timeout_seconds=120,
            temperature=None,
            top_p=None,
            max_tokens=None,
            reasoning_effort=None,
            system_prompt_template="固定系统模板。",
            extra_headers={},
            extra_body={},
        )

        context = build_prompt_context(
            self.db,
            book=self.book,
            chapter=chapter,
            current_user=self.user,
            module=AIModule.CO_WRITING,
            target_field="content",
            apply_mode="append",
            user_prompt="继续写",
            previous_chapters=0,
            character_limit=5,
            target_units=1200,
            config=config,
            system_prompt_override="补充要求：突出压迫感。",
        )

        self.assertIn("固定系统模板。", context.system_prompt)
        self.assertIn("[续写硬规则]", context.system_prompt)
        self.assertIn("[系统文风]", context.system_prompt)
        self.assertIn("补充要求：突出压迫感。", context.system_prompt)

    def test_run_world_extraction_handles_existing_none_timeline_entries(self) -> None:
        chapter = Chapter(
            book_id=self.book.id,
            title="第一章",
            sequence_number=1,
            content="周奕在东市潜伏。",
        )
        existing = Character(
            book_id=self.book.id,
            name="周奕",
            description="旧人物卡",
            is_active=True,
            card_json={"timeline_entries": None},
        )
        self.db.add_all([chapter, existing])
        self.db.commit()
        self.db.refresh(chapter)
        self.db.refresh(existing)

        resolved_config = ResolvedAIConfig(
            id=1,
            name="测试配置",
            module=AIModule.CHARACTER_EXTRACTION,
            source="database",
            scope="system",
            provider_name="openai",
            api_format="openai_v1",
            base_url="https://example.com/v1",
            api_key="test-key",
            model_name="test-model",
            timeout_seconds=120,
            temperature=None,
            top_p=None,
            max_tokens=None,
            reasoning_effort=None,
            system_prompt_template=None,
            extra_headers={},
            extra_body={},
        )

        chat_side_effect = [
            {
                "text": '{"characters":[{"name":"周奕","timeline_entries":[{"chapter_number":1,"event":"在东市潜伏","location":"东市"}]}],"world_facts":[]}',
                "request_body": {},
                "url": "https://example.com/v1/chat/completions",
                "raw_response": {},
            },
            {
                "text": '{"relations":[]}',
                "request_body": {},
                "url": "https://example.com/v1/chat/completions",
                "raw_response": {},
            },
        ]

        with patch("ai_service.resolve_ai_config_with_fallback", side_effect=[resolved_config, resolved_config]), patch(
            "ai_service.call_openai_compatible_chat",
            side_effect=chat_side_effect,
        ):
            result = run_world_extraction(
                self.db,
                book=self.book,
                chapter=chapter,
                current_user=self.user,
                dry_run=True,
                update_world_bible=False,
            )

        self.assertEqual(len(result["characters"]), 1)
        self.assertEqual(
            result["characters"][0]["card_json"]["timeline_entries"],
            [
                {
                    "chapter_number": 1,
                    "chapter_label": "第1章",
                    "chapter_title": "第一章",
                    "event": "在东市潜伏",
                    "location": "东市",
                }
            ],
        )

    def test_build_prompt_context_memory_mode_scores_before_character_limit(self) -> None:
        previous_chapters = [
            Chapter(book_id=self.book.id, title="第一章", sequence_number=1, content="第一章中甄嬛命王钦传话。"),
            Chapter(book_id=self.book.id, title="第二章", sequence_number=2, content="第二章中弘历与青樱对话。"),
            Chapter(book_id=self.book.id, title="第三章", sequence_number=3, content="第三章中甄嬛再度施压。"),
            Chapter(book_id=self.book.id, title="第四章", sequence_number=4, content="第四章结尾甄嬛与弘历在殿中对峙。"),
        ]
        current_chapter = Chapter(
            book_id=self.book.id,
            title="第五章",
            sequence_number=5,
            outline="甄嬛要求弘历给乌拉那拉氏一个说法，青樱在旁边试探。",
            content="",
        )
        self.db.add_all(previous_chapters + [current_chapter])
        self.db.flush()
        filler_characters = [
            Character(book_id=self.book.id, name="阿甲", description="无关人物", is_active=True),
            Character(book_id=self.book.id, name="阿乙", description="无关人物", is_active=True),
            Character(book_id=self.book.id, name="阿丙", description="无关人物", is_active=True),
        ]
        relevant_characters = [
            Character(
                book_id=self.book.id,
                name="爱新觉罗·弘历",
                aliases=["弘历"],
                description="主角。",
                last_appearance_chapter_id=2,
                is_active=True,
            ),
            Character(
                book_id=self.book.id,
                name="乌拉那拉·青樱",
                aliases=["青樱"],
                description="关键人物。",
                last_appearance_chapter_id=2,
                is_active=True,
            ),
            Character(
                book_id=self.book.id,
                name="甄嬛",
                aliases=["熹贵妃"],
                description="关键人物。",
                last_appearance_chapter_id=2,
                is_active=True,
            ),
        ]
        self.db.add_all(filler_characters + relevant_characters)
        self.book.global_style_prompt = "最终写作要求：准。"
        self.db.add(self.book)
        self.db.commit()
        self.db.refresh(current_chapter)

        config = ResolvedAIConfig(
            id=1,
            name="DeepSeek",
            module=AIModule.OUTLINE_EXPANSION,
            source="database",
            scope="system",
            provider_name="deepseek",
            api_format="openai_v1",
            base_url="https://api.deepseek.com/v1",
            api_key="test-key",
            model_name="deepseek-chat",
            timeout_seconds=120,
            temperature=None,
            top_p=None,
            max_tokens=None,
            reasoning_effort=None,
            system_prompt_template=None,
            extra_headers={},
            extra_body={},
        )

        context = build_prompt_context(
            self.db,
            book=self.book,
            chapter=current_chapter,
            current_user=self.user,
            module=AIModule.OUTLINE_EXPANSION,
            target_field="content",
            apply_mode="replace",
            user_prompt="扩写这一章",
            previous_chapters=1,
            character_limit=2,
            target_units=2000,
            config=config,
        )

        self.assertIn("甄嬛", context.user_prompt)
        self.assertIn("弘历", context.user_prompt)
        self.assertNotIn("阿甲", context.user_prompt)

    def test_run_generation_dry_run_falls_back_for_co_writing_when_outline_config_exists(self) -> None:
        chapter = Chapter(
            book_id=self.book.id,
            title="第一章",
            sequence_number=1,
            content="周奕在东市追查旧案。",
        )
        config = AIConfig(
            name="大纲模型",
            scope=AIScope.SYSTEM,
            module=AIModule.OUTLINE_EXPANSION,
            api_format="openai_v1",
            base_url="https://example.com/v1",
            api_key="test-key",
            model_name="test-model",
            system_prompt_template="这是大纲模块的固定模板。",
            is_enabled=True,
            is_default=True,
        )
        self.db.add_all([chapter, config])
        self.db.commit()
        self.db.refresh(chapter)

        result = run_generation(
            self.db,
            book=self.book,
            chapter=chapter,
            current_user=self.user,
            module=AIModule.CO_WRITING,
            user_prompt="继续写",
            target_field="content",
            apply_mode="append",
            target_units=1200,
            previous_chapters=0,
            character_limit=5,
            system_prompt_override=None,
            chunk_size=1200,
            use_reasoner_planning=False,
            dry_run=True,
            store_snapshot=False,
            apply_result=False,
        )

        self.assertEqual(result["ai_config"]["model_name"], "test-model")
        self.assertEqual(result["ai_config_fallback_from"], AIModule.OUTLINE_EXPANSION.value)
        self.assertEqual(result["ai_config"]["module"], AIModule.CO_WRITING.value)
        self.assertNotIn("这是大纲模块的固定模板。", result["context_preview"]["system_prompt"])

    def test_previous_chapters_heading_reflects_requested_and_actual_count(self) -> None:
        self.assertEqual(_previous_chapters_heading(1, 1), "前 1 章原文")
        self.assertEqual(_previous_chapters_heading(5, 5), "前 1-5 章原文")
        self.assertEqual(_previous_chapters_heading(5, 3), "前 1-3 章原文")


if __name__ == "__main__":
    unittest.main()
