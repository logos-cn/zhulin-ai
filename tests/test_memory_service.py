from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from memory_service import build_deepseek_memory_prompt, get_derived_style_summary, resolve_style_anchor, retrieve_dynamic_context
from models import (
    AuthorGoldenCorpus,
    Base,
    Book,
    Chapter,
    ChapterEpisodicMemory,
    SemanticKnowledgeBase,
    User,
    UserRole,
    UserStatus,
)


class MemoryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", future=True)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, future=True)
        Base.metadata.create_all(bind=self.engine)
        self.db = self.Session()

        self.user = User(
            username="memory-user",
            password_hash="x",
            role=UserRole.AUTHOR,
            status=UserStatus.ACTIVE,
            is_active=True,
        )
        self.db.add(self.user)
        self.db.flush()
        self.book = Book(
            owner_id=self.user.id,
            title="记忆测试书",
            global_style_prompt="全局文风：简洁冷静。",
        )
        self.db.add(self.book)
        self.db.flush()

        chapter_1 = Chapter(book_id=self.book.id, title="第一章", sequence_number=1, content="第一章正文")
        chapter_2 = Chapter(book_id=self.book.id, title="第二章", sequence_number=2, content="第二章正文")
        chapter_3 = Chapter(book_id=self.book.id, title="第三章", sequence_number=3, content="第三章正文")
        chapter_4 = Chapter(book_id=self.book.id, title="第四章", sequence_number=4, content="第四章正文结尾")
        self.db.add_all([chapter_1, chapter_2, chapter_3, chapter_4])
        self.db.flush()
        self.db.add_all(
            [
                ChapterEpisodicMemory(chapter_id=chapter_1.id, summary="第一章摘要", involved_characters="甲"),
                ChapterEpisodicMemory(chapter_id=chapter_2.id, summary="第二章摘要", involved_characters="甲,乙"),
                ChapterEpisodicMemory(chapter_id=chapter_3.id, summary="第三章摘要", involved_characters="乙"),
                SemanticKnowledgeBase(
                    book_id=self.book.id,
                    entity_name="夜巡司",
                    core_fact="夜巡司负责封存禁案卷宗。",
                ),
                SemanticKnowledgeBase(
                    book_id=self.book.id,
                    entity_name="白塔",
                    core_fact="白塔每逢朔夜才会开启。",
                ),
            ]
        )
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_retrieve_dynamic_context_uses_summary_window_and_previous_full_content(self) -> None:
        context = retrieve_dynamic_context(
            self.db,
            book_id=self.book.id,
            current_outline="周奕潜入夜巡司调查禁案。",
            current_chapter_seq=5,
        )

        self.assertEqual(context.recent_summaries, ["第一章摘要", "第二章摘要", "第三章摘要"])
        self.assertEqual(context.immediate_context, "第四章正文结尾")
        self.assertEqual(context.semantic_rules, ["夜巡司负责封存禁案卷宗。"])

    def test_retrieve_dynamic_context_falls_back_to_sort_order_when_sequence_number_missing(self) -> None:
        chapters = self.db.query(Chapter).filter(Chapter.book_id == self.book.id).order_by(Chapter.id.asc()).all()
        for chapter in chapters:
            chapter.sequence_number = None
            self.db.add(chapter)
        self.db.commit()

        context = retrieve_dynamic_context(
            self.db,
            book_id=self.book.id,
            current_outline="周奕潜入夜巡司调查禁案。",
            current_chapter_seq=5,
        )

        self.assertEqual(context.recent_summaries, ["第一章摘要", "第二章摘要", "第三章摘要"])
        self.assertEqual(context.immediate_context, "第四章正文结尾")
        self.assertEqual(context.semantic_rules, ["夜巡司负责封存禁案卷宗。"])

    def test_resolve_style_anchor_always_uses_book_prompt(self) -> None:
        self.assertEqual(resolve_style_anchor(self.db, self.book), "全局文风：简洁冷静。")

        corpus = AuthorGoldenCorpus(
            book_id=self.book.id,
            content="范本文风：长句克制，动作优先。",
        )
        self.db.add(corpus)
        self.db.commit()

        self.assertEqual(resolve_style_anchor(self.db, self.book), "全局文风：简洁冷静。")

    def test_get_derived_style_summary_reads_book_extra_data(self) -> None:
        self.book.extra_data = {
            "derived_style_summary": "短句推进，动作描写优先。",
            "derived_style_summary_updated_at": "2026-04-04T02:40:00+00:00",
        }
        self.db.add(self.book)
        self.db.commit()

        content, updated_at = get_derived_style_summary(self.book)

        self.assertEqual(content, "短句推进，动作描写优先。")
        self.assertEqual(updated_at, "2026-04-04T02:40:00+00:00")

    def test_build_deepseek_memory_prompt_keeps_xml_structure(self) -> None:
        prompt = build_deepseek_memory_prompt(
            style_anchor="范本",
            recent_summaries=["摘要一", "摘要二"],
            immediate_context="上一章正文",
            semantic_rules=["规则一", "规则二"],
            character_cards=[
                {
                    "name": "周奕",
                    "role_label": "调查者",
                    "biography": "山门旧案幸存者。",
                    "description": "山门旧案幸存者。",
                    "personality": "冷静多疑",
                }
            ],
            current_outline="当前大纲",
        )

        self.assertIn("<system_instructions>", prompt)
        self.assertIn("<style_anchor>", prompt)
        self.assertIn("<character_cards>", prompt)
        self.assertIn("<recent_summaries>", prompt)
        self.assertIn("<immediate_context>", prompt)
        self.assertIn("<semantic_rules>", prompt)
        self.assertIn("<current_task>", prompt)
        self.assertIn("周奕", prompt)
        self.assertIn("摘要一", prompt)
        self.assertIn("规则二", prompt)
        self.assertEqual(prompt.count("山门旧案幸存者。"), 1)


if __name__ == "__main__":
    unittest.main()
