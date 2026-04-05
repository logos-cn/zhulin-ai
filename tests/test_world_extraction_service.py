from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from models import (
    Base,
    Book,
    Character,
    Chapter,
    ChapterNodeType,
    Relation,
    RelationEvent,
    User,
    UserRole,
    UserStatus,
    WorldConflictStrategy,
    WorldExtractionJob,
    WorldExtractionJobStatus,
    WorldExtractionSource,
)
from world_extraction_service import (
    DEFAULT_IMPORTED_DOCUMENT_WORKERS,
    ExtractedSegmentPayload,
    ExtractionSegment,
    WorldExtractionCancellationRequested,
    _coalesce_external_blocks,
    _effective_worker_count,
    _select_existing_relation,
    _summarize_world_facts_hierarchically,
    _world_character_prompt,
    _iter_ordered_parallel_extractions,
    _merge_character_payload,
    _parse_boolean,
    _chapter_extraction_signature,
    _postprocess_world_extraction_results,
    _safe_parse_strength,
    apply_segment_world_payload,
    estimate_import_document,
    plan_internal_book_blocks,
    recover_interrupted_world_extraction_jobs,
    resolve_world_extraction_conflict,
    select_job_retry_segments,
)


class WorldExtractionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", future=True)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, future=True)
        Base.metadata.create_all(bind=self.engine)
        self.db = self.Session()

        self.user = User(
            username="tester",
            display_name="Tester",
            password_hash="x",
            role=UserRole.SUPER_ADMIN,
            status=UserStatus.ACTIVE,
            is_active=True,
        )
        self.db.add(self.user)
        self.db.flush()

        self.book = Book(
            owner_id=self.user.id,
            title="测试书",
            global_style_prompt="",
        )
        self.db.add(self.book)
        self.db.commit()
        self.db.refresh(self.user)
        self.db.refresh(self.book)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_merge_character_payload_keeps_earliest_and_latest_appearance(self) -> None:
        existing = Character(
            book_id=self.book.id,
            name="周奕",
            first_appearance_chapter_id=10,
            last_appearance_chapter_id=12,
            is_active=True,
        )

        merged = _merge_character_payload(
            existing,
            {"name": "周奕"},
            strategy=WorldConflictStrategy.MERGE,
            chapter_id=8,
        )

        self.assertEqual(merged["first_appearance_chapter_id"], 8)
        self.assertEqual(merged["last_appearance_chapter_id"], 12)
        self.assertLessEqual(
            merged["first_appearance_chapter_id"],
            merged["last_appearance_chapter_id"],
        )

    def test_merge_character_payload_preserves_extended_card_fields(self) -> None:
        existing = Character(
            book_id=self.book.id,
            name="周奕",
            is_active=True,
            card_json={"age": "二十出头", "short_term_goal": "旧目标"},
        )

        merged = _merge_character_payload(
            existing,
            {
                "name": "周奕",
                "long_term_goal": "查清灭门真相",
                "motivation": "替师门复仇",
                "personality": "冷静谨慎",
                "appearance": "黑衣瘦高，左眉带疤",
                "weakness": "旧伤发作时行动受限",
            },
            strategy=WorldConflictStrategy.MERGE,
            chapter_id=8,
        )

        self.assertEqual(merged["card_json"]["age"], "二十出头")
        self.assertEqual(merged["card_json"]["short_term_goal"], "旧目标")
        self.assertEqual(merged["card_json"]["long_term_goal"], "查清灭门真相")
        self.assertEqual(merged["card_json"]["motivation"], "替师门复仇")
        self.assertEqual(merged["card_json"]["personality"], "冷静谨慎")
        self.assertEqual(merged["card_json"]["appearance"], "黑衣瘦高，左眉带疤")
        self.assertEqual(merged["card_json"]["weakness"], "旧伤发作时行动受限")

    def test_relation_unique_constraint_blocks_duplicate_rows(self) -> None:
        source = Character(book_id=self.book.id, name="甲", is_active=True)
        target = Character(book_id=self.book.id, name="乙", is_active=True)
        self.db.add_all([source, target])
        self.db.flush()
        first = Relation(
            book_id=self.book.id,
            source_character_id=source.id,
            target_character_id=target.id,
            relation_type="夫妻",
            description="第一条",
        )
        second = Relation(
            book_id=self.book.id,
            source_character_id=source.id,
            target_character_id=target.id,
            relation_type="夫妻",
            description="第二条",
        )
        self.db.add_all([first, second])
        with self.assertRaises(Exception):
            self.db.commit()

    def test_parse_boolean_handles_string_false_correctly(self) -> None:
        self.assertTrue(_parse_boolean(True))
        self.assertFalse(_parse_boolean(False))
        self.assertTrue(_parse_boolean("true"))
        self.assertFalse(_parse_boolean("false"))
        self.assertTrue(_parse_boolean(1))
        self.assertFalse(_parse_boolean(0))
        self.assertFalse(_parse_boolean(""))
        self.assertFalse(_parse_boolean(None))

    def test_safe_parse_strength_handles_invalid_and_out_of_range_values(self) -> None:
        self.assertIsNone(_safe_parse_strength("高"))
        self.assertIsNone(_safe_parse_strength(""))
        self.assertIsNone(_safe_parse_strength(None))
        self.assertIsNone(_safe_parse_strength("nan"))
        self.assertEqual(_safe_parse_strength("1.7"), 1.0)
        self.assertEqual(_safe_parse_strength("-0.5"), 0.0)
        self.assertEqual(_safe_parse_strength("0.35"), 0.35)

    def test_effective_worker_count_clamps_to_segment_count(self) -> None:
        self.assertEqual(_effective_worker_count(1000, 8), 8)
        self.assertEqual(_effective_worker_count(DEFAULT_IMPORTED_DOCUMENT_WORKERS, 2), 2)
        self.assertEqual(_effective_worker_count(1000, 0), 1)

    def test_coalesce_external_blocks_merges_short_paragraphs(self) -> None:
        blocks = list(
            _coalesce_external_blocks(
                "原著.txt",
                [
                    "甲" * 400,
                    "乙" * 400,
                    "丙" * 400,
                    "丁" * 400,
                ],
                target_unit_limit=1200,
            )
        )

        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0].label, "原著.txt 第 1-2 段")
        self.assertEqual(blocks[1].label, "原著.txt 第 3-4 段")
        self.assertIn("甲甲甲", blocks[0].text)
        self.assertIn("乙乙乙", blocks[0].text)
        self.assertIn("丙丙丙", blocks[1].text)
        self.assertIn("丁丁丁", blocks[1].text)

    def test_estimate_import_document_returns_token_range(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False) as handle:
            handle.write(("甲" * 400) + "\n\n")
            handle.write(("乙" * 400) + "\n\n")
            handle.write(("丙" * 400) + "\n\n")
            handle.write(("丁" * 400) + "\n")
            temp_path = handle.name

        try:
            estimate = estimate_import_document(
                Path(temp_path),
                source_name="测试原著.txt",
                segment_unit_limit=1200,
                update_world_bible=True,
            )
        finally:
            Path(temp_path).unlink(missing_ok=True)

        self.assertEqual(estimate["raw_block_count"], 4)
        self.assertEqual(estimate["coalesced_block_count"], 2)
        self.assertEqual(estimate["estimated_segment_count"], 2)
        self.assertEqual(estimate["estimated_model_call_count"], 6)
        self.assertGreater(estimate["estimated_total_tokens_low"], 0)
        self.assertGreater(estimate["estimated_total_tokens_high"], estimate["estimated_total_tokens_low"])

    def test_apply_segment_world_payload_deduplicates_character_name_with_internal_spaces(self) -> None:
        existing = Character(
            book_id=self.book.id,
            name="李四",
            is_active=True,
        )
        self.db.add(existing)
        self.db.commit()
        self.db.refresh(existing)

        result = apply_segment_world_payload(
            self.db,
            book=self.book,
            segment=ExtractionSegment(label="第八章", text="李四出现", unit_count=12, chapter_id=8),
            extracted_payload=ExtractedSegmentPayload(
                segment_label="第八章",
                segment_units=12,
                characters=[{"name": "李   四", "description": "更新后的人物描述"}],
                relations=[],
                world_facts=[],
            ),
            conflict_strategy=WorldConflictStrategy.MERGE,
            update_world_bible=False,
        )

        characters = self.db.execute(
            select(Character).where(Character.book_id == self.book.id)
        ).scalars().all()

        self.assertEqual(len(characters), 1)
        self.assertEqual(result["created_character_count"], 0)
        self.assertEqual(result["updated_character_count"], 1)
        self.assertEqual(characters[0].name, "李四")
        self.assertEqual(characters[0].description, "更新后的人物描述")

    def test_apply_segment_world_payload_records_character_timeline_entry(self) -> None:
        chapter = Chapter(
            book_id=self.book.id,
            title="潜入东市",
            sequence_number=8,
            sort_order=8,
            outline="",
            content="",
        )
        self.db.add(chapter)
        self.db.commit()
        self.db.refresh(chapter)

        apply_segment_world_payload(
            self.db,
            book=self.book,
            segment=ExtractionSegment(label="第八章", text="周奕潜入东市", unit_count=12, chapter_id=chapter.id),
            extracted_payload=ExtractedSegmentPayload(
                segment_label="第八章",
                segment_units=12,
                characters=[
                    {
                        "name": "周奕",
                        "description": "谨慎的追查者",
                        "timeline_entries": [
                            {"event": "潜入东市调查", "location": "东市", "status": "潜伏中"}
                        ],
                    }
                ],
                relations=[],
                world_facts=[],
            ),
            conflict_strategy=WorldConflictStrategy.MERGE,
            update_world_bible=False,
        )

        character = self.db.execute(
            select(Character).where(Character.book_id == self.book.id, Character.name == "周奕")
        ).scalar_one()

        self.assertEqual(
            character.card_json.get("timeline_entries"),
            [
                {
                    "chapter_number": 8,
                    "chapter_label": "第8章",
                    "chapter_title": "潜入东市",
                    "event": "潜入东市调查",
                    "location": "东市",
                    "status": "潜伏中",
                }
            ],
        )

    def test_apply_segment_world_payload_records_character_life_statuses(self) -> None:
        apply_segment_world_payload(
            self.db,
            book=self.book,
            segment=ExtractionSegment(label="第八章", text="周奕重伤后仍活着", unit_count=12, chapter_id=8),
            extracted_payload=ExtractedSegmentPayload(
                segment_label="第八章",
                segment_units=12,
                characters=[
                    {
                        "name": "周奕",
                        "description": "谨慎的追查者",
                        "life_statuses": ["活着", "重伤"],
                    }
                ],
                relations=[],
                world_facts=[],
            ),
            conflict_strategy=WorldConflictStrategy.MERGE,
            update_world_bible=False,
        )

        character = self.db.execute(
            select(Character).where(Character.book_id == self.book.id, Character.name == "周奕")
        ).scalar_one()

        self.assertEqual(
            character.card_json.get("life_statuses"),
            ["alive", "serious_injury"],
        )

    def test_apply_segment_world_payload_appends_character_timeline_entries_across_chapters(self) -> None:
        first_chapter = Chapter(
            book_id=self.book.id,
            title="东市盯梢",
            sequence_number=8,
            sort_order=8,
            outline="",
            content="",
        )
        second_chapter = Chapter(
            book_id=self.book.id,
            title="山门潜伏",
            sequence_number=18,
            sort_order=18,
            outline="",
            content="",
        )
        self.db.add_all([first_chapter, second_chapter])
        self.db.commit()
        self.db.refresh(first_chapter)
        self.db.refresh(second_chapter)

        apply_segment_world_payload(
            self.db,
            book=self.book,
            segment=ExtractionSegment(label="第八章", text="周奕在东市", unit_count=12, chapter_id=first_chapter.id),
            extracted_payload=ExtractedSegmentPayload(
                segment_label="第八章",
                segment_units=12,
                characters=[
                    {
                        "name": "周奕",
                        "timeline_entries": [{"event": "在东市盯梢", "location": "东市"}],
                    }
                ],
                relations=[],
                world_facts=[],
            ),
            conflict_strategy=WorldConflictStrategy.MERGE,
            update_world_bible=False,
        )

        apply_segment_world_payload(
            self.db,
            book=self.book,
            segment=ExtractionSegment(label="第十八章", text="周奕潜入山门", unit_count=12, chapter_id=second_chapter.id),
            extracted_payload=ExtractedSegmentPayload(
                segment_label="第十八章",
                segment_units=12,
                characters=[
                    {
                        "name": "周奕",
                        "timeline_entries": [{"event": "潜入山门地牢", "location": "山门地牢", "status": "伪装潜伏"}],
                    }
                ],
                relations=[],
                world_facts=[],
            ),
            conflict_strategy=WorldConflictStrategy.MERGE,
            update_world_bible=False,
        )

        character = self.db.execute(
            select(Character).where(Character.book_id == self.book.id, Character.name == "周奕")
        ).scalar_one()

        self.assertEqual(
            character.card_json.get("timeline_entries"),
            [
                {
                    "chapter_number": 8,
                    "chapter_label": "第8章",
                    "chapter_title": "东市盯梢",
                    "event": "在东市盯梢",
                    "location": "东市",
                },
                {
                    "chapter_number": 18,
                    "chapter_label": "第18章",
                    "chapter_title": "山门潜伏",
                    "event": "潜入山门地牢",
                    "location": "山门地牢",
                    "status": "伪装潜伏",
                },
            ],
        )

    def test_apply_segment_world_payload_manual_review_creates_new_relation_without_failure(self) -> None:
        source = Character(book_id=self.book.id, name="周奕", is_active=True)
        target = Character(book_id=self.book.id, name="母亲", is_active=True)
        self.db.add_all([source, target])
        self.db.commit()
        self.db.refresh(source)
        self.db.refresh(target)

        result = apply_segment_world_payload(
            self.db,
            book=self.book,
            segment=ExtractionSegment(label="第一章", text="周奕与母亲对话", unit_count=42, chapter_id=1),
            extracted_payload=ExtractedSegmentPayload(
                segment_label="第一章",
                segment_units=42,
                characters=[],
                relations=[
                    {
                        "source_name": "周奕",
                        "target_name": "母亲",
                        "relation_type": "亲属",
                        "label": "母子",
                        "description": "周奕与母亲关系亲近",
                        "strength": "0.95",
                        "is_bidirectional": True,
                    }
                ],
                world_facts=[],
            ),
            conflict_strategy=WorldConflictStrategy.MANUAL_REVIEW,
            update_world_bible=False,
        )

        relations = self.db.execute(
            select(Relation).where(Relation.book_id == self.book.id)
        ).scalars().all()

        self.assertEqual(result["created_relation_count"], 1)
        self.assertEqual(result["updated_relation_count"], 0)
        self.assertEqual(result["relation_conflict_count"], 0)
        self.assertEqual(len(result["conflicts"]), 0)
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0].label, "母子")
        self.assertAlmostEqual(relations[0].strength or 0.0, 0.95)
        self.assertTrue(relations[0].is_bidirectional)

    def test_apply_segment_world_payload_compacts_relation_description(self) -> None:
        source = Character(book_id=self.book.id, name="周奕", is_active=True)
        target = Character(book_id=self.book.id, name="林秋", is_active=True)
        self.db.add_all([source, target])
        self.db.commit()
        self.db.refresh(source)
        self.db.refresh(target)

        apply_segment_world_payload(
            self.db,
            book=self.book,
            segment=ExtractionSegment(label="第一章", text="两人一起行动", unit_count=18, chapter_id=1),
            extracted_payload=ExtractedSegmentPayload(
                segment_label="第一章",
                segment_units=18,
                characters=[],
                relations=[
                    {
                        "source_name": "周奕",
                        "target_name": "林秋",
                        "relation_type": "同伴",
                        "label": "临时搭档",
                        "description": "两人目前互相信任，并开始共同调查真相。第二句不应该继续保留，因为关系说明必须保持很短。",
                        "strength": "0.7",
                        "is_bidirectional": True,
                    }
                ],
                world_facts=[],
            ),
            conflict_strategy=WorldConflictStrategy.MERGE,
            update_world_bible=False,
        )

        relation = self.db.execute(
            select(Relation).where(Relation.book_id == self.book.id)
        ).scalar_one()

        self.assertEqual(relation.description, "两人目前互相信任，并开始共同调查真相")
        self.assertLessEqual(len(relation.description or ""), 180)

    def test_apply_segment_world_payload_normalizes_relation_type_and_importance(self) -> None:
        source = Character(book_id=self.book.id, name="周奕", is_active=True)
        target = Character(book_id=self.book.id, name="林秋", is_active=True)
        self.db.add_all([source, target])
        self.db.commit()
        self.db.refresh(source)
        self.db.refresh(target)

        apply_segment_world_payload(
            self.db,
            book=self.book,
            segment=ExtractionSegment(label="第一章", text="两人结盟调查", unit_count=18, chapter_id=1),
            extracted_payload=ExtractedSegmentPayload(
                segment_label="第一章",
                segment_units=18,
                characters=[],
                relations=[
                    {
                        "source_name": "周奕",
                        "target_name": "林秋",
                        "relation_type": "盟友",
                        "label": "盟友",
                        "description": "两人暂时结盟追查真相",
                        "importance_level": "core",
                        "is_bidirectional": True,
                    }
                ],
                world_facts=[],
            ),
            conflict_strategy=WorldConflictStrategy.MERGE,
            update_world_bible=False,
        )

        relation = self.db.execute(select(Relation).where(Relation.book_id == self.book.id)).scalar_one()
        self.assertEqual(relation.relation_type, "affinity")
        self.assertEqual(relation.label, "盟友")
        self.assertEqual(relation.importance_level, "core")

    def test_apply_segment_world_payload_records_relation_event_history(self) -> None:
        source = Character(book_id=self.book.id, name="周奕", is_active=True)
        target = Character(book_id=self.book.id, name="林秋", is_active=True)
        self.db.add_all([source, target])
        self.db.commit()
        self.db.refresh(source)
        self.db.refresh(target)

        apply_segment_world_payload(
            self.db,
            book=self.book,
            segment=ExtractionSegment(label="第一章", text="两人联手", unit_count=18, chapter_id=1),
            extracted_payload=ExtractedSegmentPayload(
                segment_label="第一章",
                segment_units=18,
                characters=[],
                relations=[
                    {
                        "source_name": "周奕",
                        "target_name": "林秋",
                        "relation_type": "affinity",
                        "label": "盟友",
                        "description": "两人暂时联手调查",
                        "event_summary": "在第一章达成合作",
                    }
                ],
                world_facts=[],
            ),
            conflict_strategy=WorldConflictStrategy.MERGE,
            update_world_bible=False,
        )

        apply_segment_world_payload(
            self.db,
            book=self.book,
            segment=ExtractionSegment(label="第三章", text="两人互生猜忌", unit_count=18, chapter_id=3),
            extracted_payload=ExtractedSegmentPayload(
                segment_label="第三章",
                segment_units=18,
                characters=[],
                relations=[
                    {
                        "source_name": "周奕",
                        "target_name": "林秋",
                        "relation_type": "hostility",
                        "label": "猜忌",
                        "description": "两人开始彼此防备",
                        "event_summary": "调查失利后互生猜忌",
                    }
                ],
                world_facts=[],
            ),
            conflict_strategy=WorldConflictStrategy.MERGE,
            update_world_bible=False,
        )

        events = self.db.execute(
            select(RelationEvent).where(RelationEvent.book_id == self.book.id).order_by(RelationEvent.id.asc())
        ).scalars().all()

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].relation_type, "affinity")
        self.assertEqual(events[1].relation_type, "hostility")

    def test_apply_segment_world_payload_returns_touched_entity_ids(self) -> None:
        result = apply_segment_world_payload(
            self.db,
            book=self.book,
            segment=ExtractionSegment(label="第一章", text="周奕与林秋在东市碰头", unit_count=24, chapter_id=1),
            extracted_payload=ExtractedSegmentPayload(
                segment_label="第一章",
                segment_units=24,
                characters=[
                    {"name": "周奕", "description": "追查真相的人"},
                    {"name": "林秋", "description": "与周奕合作的同伴"},
                ],
                relations=[
                    {
                        "source_name": "周奕",
                        "target_name": "林秋",
                        "relation_type": "同伴",
                        "label": "合作查案",
                        "description": "两人暂时联手调查",
                    }
                ],
                world_facts=[],
            ),
            conflict_strategy=WorldConflictStrategy.MERGE,
            update_world_bible=False,
        )

        self.assertEqual(len(result["character_ids"]), 2)
        self.assertEqual(len(result["relation_ids"]), 1)
        self.assertTrue(all(isinstance(item, int) for item in result["character_ids"]))
        self.assertTrue(all(isinstance(item, int) for item in result["relation_ids"]))

    def test_plan_internal_book_blocks_only_uses_title_and_content(self) -> None:
        chapter = Chapter(
            book_id=self.book.id,
            title="第一章 山门夜行",
            node_type=ChapterNodeType.CHAPTER,
            sequence_number=1,
            sort_order=1,
            outline="这里是大纲，不应进入提取文本",
            summary="这里是摘要，也不应进入提取文本",
            content="周奕夜里潜入山门，沿石阶向上。",
        )
        self.db.add(chapter)
        self.db.commit()
        self.db.refresh(chapter)

        job = WorldExtractionJob(
            book_id=self.book.id,
            created_by_id=self.user.id,
            source_type=WorldExtractionSource.INTERNAL_BOOK,
            source_name="软件内全书",
            conflict_strategy=WorldConflictStrategy.MERGE,
            update_world_bible=False,
            chapter_scope="with_content",
            options_json={"skip_unchanged_chapters": False},
        )

        blocks, stats = plan_internal_book_blocks(self.db, job)

        self.assertEqual(len(blocks), 1)
        self.assertEqual(stats["included_chapter_count"], 1)
        self.assertIn("章节标题：第一章 山门夜行", blocks[0].text)
        self.assertIn("章节正文：\n周奕夜里潜入山门，沿石阶向上。", blocks[0].text)
        self.assertNotIn("章节大纲", blocks[0].text)
        self.assertNotIn("章节摘要", blocks[0].text)
        self.assertNotIn("这里是大纲", blocks[0].text)
        self.assertNotIn("这里是摘要", blocks[0].text)

    def test_plan_internal_book_blocks_skips_outline_only_chapter(self) -> None:
        chapter = Chapter(
            book_id=self.book.id,
            title="只有大纲的章节",
            node_type=ChapterNodeType.CHAPTER,
            sequence_number=1,
            sort_order=1,
            outline="只有大纲，没有正文",
            summary="只有摘要，没有正文",
            content="",
        )
        self.db.add(chapter)
        self.db.commit()

        job = WorldExtractionJob(
            book_id=self.book.id,
            created_by_id=self.user.id,
            source_type=WorldExtractionSource.INTERNAL_BOOK,
            source_name="软件内全书",
            conflict_strategy=WorldConflictStrategy.MERGE,
            update_world_bible=False,
            chapter_scope="with_content",
            options_json={"skip_unchanged_chapters": False},
        )

        blocks, stats = plan_internal_book_blocks(self.db, job)

        self.assertEqual(blocks, [])
        self.assertEqual(stats["included_chapter_count"], 0)
        self.assertEqual(stats["skipped_empty_chapter_count"], 1)

    def test_chapter_extraction_signature_ignores_outline_and_summary_changes(self) -> None:
        chapter = Chapter(
            book_id=self.book.id,
            title="第一章",
            node_type=ChapterNodeType.CHAPTER,
            content="正文没有变化",
            outline="旧大纲",
            summary="旧摘要",
        )

        original_signature = _chapter_extraction_signature(chapter)
        chapter.outline = "新大纲"
        chapter.summary = "新摘要"
        updated_signature = _chapter_extraction_signature(chapter)

        self.assertEqual(original_signature, updated_signature)

    def test_postprocess_world_extraction_results_summarizes_entities_and_world_facts(self) -> None:
        source = Character(book_id=self.book.id, name="周奕", description="原始描述", is_active=True)
        target = Character(book_id=self.book.id, name="林秋", description="原始描述", is_active=True)
        self.db.add_all([source, target])
        self.db.flush()
        relation = Relation(
            book_id=self.book.id,
            source_character_id=source.id,
            target_character_id=target.id,
            relation_type="同伴",
            label="临时搭档",
            description="旧描述",
        )
        self.db.add(relation)
        self.book.world_bible = "东市由商会控制。\n夜间实行宵禁。"
        self.db.commit()
        self.db.refresh(source)
        self.db.refresh(target)
        self.db.refresh(relation)
        self.db.refresh(self.book)

        responses = [
            {
                "text": json.dumps(
                    {
                        "characters": [
                            {"id": source.id, "biography": "周奕是追查真相的核心人物，如今仍在东市暗中行动。"},
                            {"id": target.id, "biography": "林秋与周奕协同行动，负责接应与情报支持。"},
                        ]
                    },
                    ensure_ascii=False,
                )
            },
            {
                "text": json.dumps(
                    {
                        "relations": [
                            {
                                "id": relation.id,
                                "relation_type": "friend",
                                "label": "alliance",
                                "description": "两人因共同调查而互相信任。",
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            },
            {
                "text": json.dumps(
                    {
                        "world_facts": [
                            "东市的实际秩序长期受商会把持。",
                            "夜间城中实施严格宵禁。",
                        ]
                    },
                    ensure_ascii=False,
                )
            },
        ]

        with patch(
            "world_extraction_service.ai_service.call_openai_compatible_chat",
            side_effect=responses,
        ):
            stats = _postprocess_world_extraction_results(
                self.db,
                book=self.book,
                summary_config=object(),
                touched_character_ids={source.id, target.id},
                touched_relation_ids={relation.id},
                update_world_bible=True,
            )

        self.db.commit()
        self.db.refresh(source)
        self.db.refresh(target)
        self.db.refresh(relation)
        self.db.refresh(self.book)

        self.assertEqual(stats["characters_summarized"], 2)
        self.assertEqual(stats["relations_summarized"], 1)
        self.assertEqual(stats["world_facts_summarized"], 2)
        self.assertIn("核心人物", source.description or "")
        self.assertIn("协同行动", target.description or "")
        self.assertEqual(relation.relation_type, "同伴")
        self.assertEqual(relation.label, "临时搭档")
        self.assertEqual(relation.description, "两人因共同调查而互相信任")
        self.assertIn("商会把持", self.book.world_bible or "")
        self.assertIn("严格宵禁", self.book.world_bible or "")

    def test_summarize_world_facts_hierarchically_handles_large_input(self) -> None:
        world_facts = [
            "东市由商会控制。",
            "夜间实行宵禁。",
            "皇城在西北。",
            "皇帝病重。",
            "后宫等级森严。",
            "冷宫长期封锁。",
        ]
        responses = [
            {"text": json.dumps({"world_facts": ["东市由商会控制。", "夜间实行宵禁。"]}, ensure_ascii=False)},
            {"text": json.dumps({"world_facts": ["皇城在西北。", "皇帝病重。"]}, ensure_ascii=False)},
            {"text": json.dumps({"world_facts": ["后宫等级森严。", "冷宫长期封锁。"]}, ensure_ascii=False)},
            {"text": json.dumps({"world_facts": ["东市由商会控制。", "后宫等级森严。", "夜间实行宵禁。"]}, ensure_ascii=False)},
        ]
        response_index = {"value": 0}

        def fake_chat(*_args, **_kwargs):
            idx = response_index["value"]
            response_index["value"] += 1
            if idx < len(responses):
                return responses[idx]
            return responses[-1]

        with patch("world_extraction_service.WORLD_FACT_SUMMARY_MAX_INPUT_UNITS", 30), patch(
            "world_extraction_service.WORLD_FACT_SUMMARY_CHUNK_TARGET_UNITS", 16
        ), patch(
            "world_extraction_service.ai_service.call_openai_compatible_chat",
            side_effect=fake_chat,
        ) as mocked_call:
            summarized = _summarize_world_facts_hierarchically(
                self.book,
                summary_config=object(),
                world_facts=world_facts,
            )

        self.assertGreaterEqual(mocked_call.call_count, 4)
        self.assertEqual(
            summarized,
            ["东市由商会控制", "后宫等级森严", "夜间实行宵禁"],
        )

    def test_select_job_retry_segments_prefers_exact_failed_labels(self) -> None:
        job = WorldExtractionJob(
            book_id=self.book.id,
            created_by_id=self.user.id,
            source_type=WorldExtractionSource.INTERNAL_BOOK,
            source_name="测试来源",
            conflict_strategy=WorldConflictStrategy.MERGE,
            update_world_bible=False,
            options_json={
                "retry_failed_only": True,
                "failed_segment_labels": ["第一章（片段 2/2）"],
                "failed_chapter_ids": [1],
            },
        )

        segments = [
            ExtractionSegment(label="第一章（片段 1/2）", text="a", unit_count=10, chapter_id=1),
            ExtractionSegment(label="第一章（片段 2/2）", text="b", unit_count=10, chapter_id=1),
            ExtractionSegment(label="第二章", text="c", unit_count=10, chapter_id=2),
        ]

        selected = select_job_retry_segments(job, segments)

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].label, "第一章（片段 2/2）")

    def test_recover_interrupted_jobs_skips_fresh_heartbeat(self) -> None:
        job = WorldExtractionJob(
            book_id=self.book.id,
            created_by_id=self.user.id,
            source_type=WorldExtractionSource.INTERNAL_BOOK,
            source_name="测试来源",
            status=WorldExtractionJobStatus.RUNNING,
            conflict_strategy=WorldConflictStrategy.MERGE,
            update_world_bible=False,
            message="进行中",
            options_json={"last_heartbeat_at": "2099-01-01T00:00:00+00:00"},
        )
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)

        recovered = recover_interrupted_world_extraction_jobs(self.db)
        self.db.refresh(job)

        self.assertEqual(recovered, 0)
        self.assertEqual(job.status, WorldExtractionJobStatus.RUNNING)
        self.assertIsNone(job.error_message)

    def test_recover_interrupted_jobs_marks_pending_job_without_heartbeat_as_failed(self) -> None:
        job = WorldExtractionJob(
            book_id=self.book.id,
            created_by_id=self.user.id,
            source_type=WorldExtractionSource.INTERNAL_BOOK,
            source_name="测试来源",
            status=WorldExtractionJobStatus.PENDING,
            conflict_strategy=WorldConflictStrategy.MERGE,
            update_world_bible=False,
            message="已进入提取队列。",
            options_json={},
        )
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)

        recovered = recover_interrupted_world_extraction_jobs(self.db)
        self.db.refresh(job)

        self.assertEqual(recovered, 1)
        self.assertEqual(job.status, WorldExtractionJobStatus.FAILED)
        self.assertIn("服务进程中断或重新启动", job.error_message or "")
        self.assertEqual(job.message, "提取队列在开始前因服务进程中断或重新启动而中断。")

    def test_iter_ordered_parallel_extractions_emits_wait_callbacks_for_long_running_segment(self) -> None:
        segment = ExtractionSegment(label="第一章（片段 1/1）", text="测试", unit_count=12, chapter_id=1)
        wait_labels: list[str] = []

        def extractor(current_segment: ExtractionSegment) -> ExtractedSegmentPayload:
            time.sleep(0.12)
            return ExtractedSegmentPayload(
                segment_label=current_segment.label,
                segment_units=current_segment.unit_count,
                characters=[],
                relations=[],
                world_facts=[],
            )

        results = list(
            _iter_ordered_parallel_extractions(
                iter([segment]),
                max_workers=1,
                extractor=extractor,
                wait_timeout_seconds=0.02,
                on_wait=lambda current_segment: wait_labels.append(current_segment.label),
            )
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0].label, segment.label)
        self.assertIsNotNone(results[0][1])
        self.assertIsNone(results[0][2])
        self.assertGreaterEqual(len(wait_labels), 2)
        self.assertTrue(all(label == segment.label for label in wait_labels))

    def test_world_character_prompt_requests_life_statuses_and_timeline_entries(self) -> None:
        segment = ExtractionSegment(label="第八章", text="周奕在东市重伤潜伏", unit_count=12, chapter_id=8)
        prompt = _world_character_prompt(self.book, segment, [])
        user_message = next(item["content"] for item in prompt if item["role"] == "user")

        self.assertIn('"age": "string"', user_message)
        self.assertIn('"short_term_goal": "string"', user_message)
        self.assertIn('"long_term_goal": "string"', user_message)
        self.assertIn('"motivation": "string"', user_message)
        self.assertIn('"personality": "string"', user_message)
        self.assertIn('"appearance": "string"', user_message)
        self.assertIn('"weakness": "string"', user_message)
        self.assertIn('"life_statuses": ["alive | dead | serious_injury | minor_injury | disabled"]', user_message)
        self.assertIn('"timeline_entries": [', user_message)
        self.assertIn("Do not guess life_statuses", user_message)
        self.assertIn("Only fill age, personality, appearance, weakness, motivation", user_message)

    def test_iter_ordered_parallel_extractions_stops_when_cancellation_is_requested(self) -> None:
        segment = ExtractionSegment(label="第一章（片段 1/1）", text="测试", unit_count=12, chapter_id=1)
        wait_count = 0

        def extractor(current_segment: ExtractionSegment) -> ExtractedSegmentPayload:
            time.sleep(0.2)
            return ExtractedSegmentPayload(
                segment_label=current_segment.label,
                segment_units=current_segment.unit_count,
                characters=[],
                relations=[],
                world_facts=[],
            )

        def on_wait(_segment: ExtractionSegment) -> None:
            nonlocal wait_count
            wait_count += 1

        with self.assertRaises(WorldExtractionCancellationRequested):
            list(
                _iter_ordered_parallel_extractions(
                    iter([segment]),
                    max_workers=1,
                    extractor=extractor,
                    wait_timeout_seconds=0.02,
                    on_wait=on_wait,
                    should_stop=lambda: wait_count >= 2,
                )
            )

        self.assertGreaterEqual(wait_count, 2)

    def test_recover_interrupted_jobs_marks_stale_heartbeat_as_failed(self) -> None:
        job = WorldExtractionJob(
            book_id=self.book.id,
            created_by_id=self.user.id,
            source_type=WorldExtractionSource.INTERNAL_BOOK,
            source_name="测试来源",
            status=WorldExtractionJobStatus.RUNNING,
            conflict_strategy=WorldConflictStrategy.MERGE,
            update_world_bible=False,
            message="进行中",
            options_json={"last_heartbeat_at": "2000-01-01T00:00:00+00:00"},
        )
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)

        recovered = recover_interrupted_world_extraction_jobs(self.db)
        self.db.refresh(job)

        self.assertEqual(recovered, 1)
        self.assertEqual(job.status, WorldExtractionJobStatus.FAILED)
        self.assertIn("服务进程中断或重新启动", job.error_message or "")
        self.assertEqual((job.options_json or {}).get("recovery_reason"), "service_process_interrupted")

    def test_resolve_world_extraction_conflict_parses_relation_boolean_and_strength(self) -> None:
        source = Character(book_id=self.book.id, name="甲", is_active=True)
        target = Character(book_id=self.book.id, name="乙", is_active=True)
        self.db.add_all([source, target])
        self.db.flush()

        relation = Relation(
            book_id=self.book.id,
            source_character_id=source.id,
            target_character_id=target.id,
            relation_type="同伴",
            label="旧关系",
            description="旧描述",
            strength=0.9,
            is_bidirectional=True,
        )
        self.db.add(relation)
        self.db.flush()

        job = WorldExtractionJob(
            book_id=self.book.id,
            created_by_id=self.user.id,
            source_type=WorldExtractionSource.INTERNAL_BOOK,
            source_name="测试来源",
            conflict_strategy=WorldConflictStrategy.MANUAL_REVIEW,
            update_world_bible=False,
            result_payload={
                "conflicts": [
                    {
                        "id": f"relation:{relation.id}",
                        "conflict_type": "relation",
                        "target_id": relation.id,
                        "status": "pending",
                        "incoming": {
                            "label": "新关系",
                            "description": "新描述",
                            "strength": "0.4",
                            "is_bidirectional": "false",
                        },
                    }
                ],
                "totals": {
                    "pending_conflict_count": 1,
                    "resolved_conflict_count": 0,
                },
            },
        )
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)

        resolved = resolve_world_extraction_conflict(
            self.db,
            job=job,
            conflict_id=f"relation:{relation.id}",
            decision=WorldConflictStrategy.PREFER_IMPORTED,
        )

        self.db.refresh(relation)
        self.assertEqual(relation.label, "新关系")
        self.assertEqual(relation.description, "新描述")
        self.assertAlmostEqual(relation.strength or 0.0, 0.4)
        self.assertFalse(relation.is_bidirectional)
        self.assertEqual(resolved["status"], "resolved")


if __name__ == "__main__":
    unittest.main()
