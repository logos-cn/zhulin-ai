from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock
from unittest.mock import patch
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import main
from main import (
    AIModelDiscoveryRequest,
    LoginRequest,
    _sqlite_rebuild_table_with_sql,
    acquire_world_extraction_recovery_lock,
    apply_book_project_archive_payload,
    build_book_project_import_preview,
    chapter_has_extractable_content,
    ChapterUpdateRequest,
    delete_all_characters,
    delete_all_relations,
    ensure_world_extraction_capacity,
    ensure_ai_env_var_permission,
    get_admin_book_memory,
    login,
    list_ai_configs,
    migrate_ai_config_api_keys,
    migrate_world_schema,
    redact_database_url,
    release_world_extraction_recovery_lock,
    resolve_ai_connection_inputs,
    select_book_chapters_for_world_extraction,
    serialize_relation,
    update_chapter,
)
from models import AIConfig, AIModule, AIScope, Base, Book, Chapter, ChapterEpisodicMemory, ChapterNodeType, Character, Relation, RelationEvent, SemanticKnowledgeBase, User, UserRole, UserStatus, WorldExtractionJob, WorldExtractionJobStatus, WorldExtractionSource, WorldConflictStrategy
from security import PASSWORD_TIMING_PADDING_HASH
from secret_storage import decrypt_secret, encrypt_secret, is_encrypted_secret


class MainGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", future=True)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, future=True)
        Base.metadata.create_all(bind=self.engine)
        self.db = self.Session()

        self.author = User(
            username="author",
            password_hash="x",
            role=UserRole.AUTHOR,
            status=UserStatus.ACTIVE,
            is_active=True,
        )
        self.admin = User(
            username="admin",
            password_hash="x",
            role=UserRole.SUPER_ADMIN,
            status=UserStatus.ACTIVE,
            is_active=True,
        )
        self.db.add_all([self.author, self.admin])
        self.db.flush()

        self.config = AIConfig(
            name="测试配置",
            scope=AIScope.USER,
            module=AIModule.CO_WRITING,
            user_id=self.author.id,
            api_format="openai_v1",
            base_url="https://stored.example/v1",
            base_url_env_var="OPENAI_BASE_URL",
            api_key="stored-key",
            api_key_env_var="OPENAI_API_KEY",
            is_enabled=True,
            is_default=False,
        )
        self.db.add(self.config)
        self.system_config = AIConfig(
            name="系统配置",
            scope=AIScope.SYSTEM,
            module=AIModule.OUTLINE_EXPANSION,
            api_format="openai_v1",
            base_url="https://internal.example/v1",
            api_key="system-key",
            model_name="system-model",
            system_prompt_template="secret system prompt",
            extra_headers={"X-Internal": "1"},
            extra_body={"reasoning": "high"},
            notes="internal only",
            is_enabled=True,
            is_default=True,
        )
        self.db.add(self.system_config)
        self.db.commit()
        self.db.refresh(self.config)
        self.db.refresh(self.system_config)

        self.book = Book(
            owner_id=self.author.id,
            title="测试书",
            global_style_prompt="",
        )
        self.db.add(self.book)
        self.db.commit()
        self.db.refresh(self.book)

        self.original_openai_base_url = os.environ.get("OPENAI_BASE_URL")
        self.original_openai_api_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_BASE_URL"] = "https://env.example/v1"
        os.environ["OPENAI_API_KEY"] = "env-key"
        self.lock_dir = tempfile.TemporaryDirectory()
        self.lock_path = Path(self.lock_dir.name) / "world-extraction.lock"
        self.original_lock_handle = main.WORLD_EXTRACTION_RECOVERY_LOCK_HANDLE
        main.WORLD_EXTRACTION_RECOVERY_LOCK_HANDLE = None

    def tearDown(self) -> None:
        release_world_extraction_recovery_lock()
        main.WORLD_EXTRACTION_RECOVERY_LOCK_HANDLE = self.original_lock_handle
        self.lock_dir.cleanup()
        main.LOGIN_ATTEMPTS.clear()
        main.LOGIN_BLOCKED_UNTIL.clear()

        if self.original_openai_base_url is None:
            os.environ.pop("OPENAI_BASE_URL", None)
        else:
            os.environ["OPENAI_BASE_URL"] = self.original_openai_base_url

        if self.original_openai_api_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self.original_openai_api_key

        self.db.close()
        self.engine.dispose()

    def test_redact_database_url_masks_password(self) -> None:
        self.assertEqual(
            redact_database_url("postgresql://writer:secret@db.internal:5432/zhulin"),
            "postgresql://writer:***@db.internal:5432/zhulin",
        )

    def test_non_admin_cannot_set_ai_env_var_bindings(self) -> None:
        with self.assertRaises(HTTPException) as context:
            ensure_ai_env_var_permission(
                self.author,
                {
                    "base_url_env_var": "OPENAI_BASE_URL",
                },
            )

        self.assertEqual(context.exception.status_code, 403)

    def test_model_discovery_uses_stored_credentials_for_non_admin(self) -> None:
        payload = AIModelDiscoveryRequest(config_id=self.config.id, timeout_seconds=30)
        _config, _api_format, base_url, api_key, timeout_seconds = resolve_ai_connection_inputs(
            self.db,
            payload,
            self.author,
        )

        self.assertEqual(base_url, "https://stored.example/v1")
        self.assertEqual(api_key, "stored-key")
        self.assertEqual(timeout_seconds, 30)

    def test_secret_storage_roundtrip(self) -> None:
        encrypted = encrypt_secret("stored-key")

        self.assertTrue(is_encrypted_secret(encrypted))
        self.assertEqual(decrypt_secret(encrypted), "stored-key")

    def test_ai_config_api_key_migration_encrypts_plaintext(self) -> None:
        migrated = migrate_ai_config_api_keys(self.db)
        self.db.refresh(self.config)

        self.assertGreaterEqual(migrated, 1)
        self.assertTrue(is_encrypted_secret(self.config.api_key))
        self.assertEqual(decrypt_secret(self.config.api_key), "stored-key")

    def test_model_discovery_uses_decrypted_stored_credentials(self) -> None:
        self.config.api_key = encrypt_secret("stored-key")
        self.db.add(self.config)
        self.db.commit()
        self.db.refresh(self.config)

        payload = AIModelDiscoveryRequest(config_id=self.config.id, timeout_seconds=30)
        _config, _api_format, _base_url, api_key, _timeout_seconds = resolve_ai_connection_inputs(
            self.db,
            payload,
            self.author,
        )

        self.assertEqual(api_key, "stored-key")

    def test_model_discovery_allows_runtime_env_for_admin(self) -> None:
        payload = AIModelDiscoveryRequest(config_id=self.config.id, timeout_seconds=30)
        _config, _api_format, base_url, api_key, _timeout_seconds = resolve_ai_connection_inputs(
            self.db,
            payload,
            self.admin,
        )

        self.assertEqual(base_url, "https://env.example/v1")
        self.assertEqual(api_key, "env-key")

    def test_model_discovery_rejects_private_base_url_for_non_admin(self) -> None:
        payload = AIModelDiscoveryRequest(base_url="http://127.0.0.1:11434/v1", timeout_seconds=30)

        with self.assertRaises(HTTPException) as context:
            resolve_ai_connection_inputs(self.db, payload, self.author)

        self.assertEqual(context.exception.status_code, 403)
        self.assertIn("不能将接口地址指向本机或内网", str(context.exception.detail))

    def test_model_discovery_allows_private_base_url_for_admin(self) -> None:
        payload = AIModelDiscoveryRequest(base_url="http://127.0.0.1:11434/v1", timeout_seconds=30)

        _config, _api_format, base_url, _api_key, _timeout_seconds = resolve_ai_connection_inputs(
            self.db,
            payload,
            self.admin,
        )

        self.assertEqual(base_url, "http://127.0.0.1:11434/v1")

    def test_update_chapter_schedules_memory_consolidation_after_content_save(self) -> None:
        chapter = Chapter(
            book_id=self.book.id,
            title="第一章",
            content="旧正文",
            sequence_number=1,
        )
        self.db.add(chapter)
        self.db.commit()
        self.db.refresh(chapter)

        with patch("main.schedule_chapter_memory_consolidation") as schedule_mock:
            payload = ChapterUpdateRequest(content="新正文")
            response = update_chapter(
                self.book.id,
                chapter.id,
                payload,
                current_user=self.author,
                db=self.db,
            )

        self.assertEqual(response["content"], "新正文")
        schedule_mock.assert_called_once_with(chapter.id)

    def test_update_chapter_allows_none_content_without_crashing(self) -> None:
        book = Book(id=self.book.id, owner_id=self.author.id, title="测试书", global_style_prompt="")
        chapter = Chapter(
            id=1,
            book_id=self.book.id,
            title="第一章",
            content=None,
            sequence_number=1,
        )
        fake_db = MagicMock()

        with patch("main.get_book_or_404", return_value=book), patch(
            "main.ensure_book_http_access"
        ), patch("main.get_chapter_or_404", return_value=chapter), patch(
            "main.rebuild_chapter_tree"
        ), patch("main.refresh_book_aggregates"), patch(
            "main.serialize_chapter_detail",
            return_value={"id": chapter.id, "content": chapter.content},
        ), patch("main.schedule_chapter_memory_consolidation") as schedule_mock:
            response = update_chapter(
                self.book.id,
                chapter.id,
                ChapterUpdateRequest(title="新标题"),
                current_user=self.author,
                db=fake_db,
            )

        self.assertIsNone(response["content"])
        schedule_mock.assert_not_called()

    def test_login_uses_timing_padding_hash_when_user_missing(self) -> None:
        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))

        with patch("main.verify_password", return_value=False) as verify_mock:
            with self.assertRaises(HTTPException) as context:
                login(
                    LoginRequest(username="ghost", password="bad-password"),
                    request=request,
                    db=self.db,
                )

        self.assertEqual(context.exception.status_code, 401)
        verify_mock.assert_called_once_with("bad-password", PASSWORD_TIMING_PADDING_HASH)

    def test_ensure_world_extraction_capacity_enforces_per_user_limit(self) -> None:
        second_book = Book(owner_id=self.author.id, title="第二本书", global_style_prompt="")
        self.db.add(second_book)
        self.db.flush()
        self.db.add_all(
            [
                WorldExtractionJob(
                    book_id=self.book.id,
                    created_by_id=self.author.id,
                    source_type=WorldExtractionSource.INTERNAL_BOOK,
                    source_name=self.book.title,
                    status=WorldExtractionJobStatus.PENDING,
                    conflict_strategy=WorldConflictStrategy.MERGE,
                    update_world_bible=True,
                ),
                WorldExtractionJob(
                    book_id=second_book.id,
                    created_by_id=self.author.id,
                    source_type=WorldExtractionSource.INTERNAL_BOOK,
                    source_name=second_book.title,
                    status=WorldExtractionJobStatus.RUNNING,
                    conflict_strategy=WorldConflictStrategy.MERGE,
                    update_world_bible=True,
                ),
            ]
        )
        self.db.commit()

        with self.assertRaises(HTTPException) as context:
            ensure_world_extraction_capacity(self.db, self.author, self.book.id)

        self.assertEqual(context.exception.status_code, 429)
        self.assertIn("你当前已有过多后台提取任务", str(context.exception.detail))

    def test_ensure_world_extraction_capacity_enforces_per_book_limit(self) -> None:
        other_user = User(
            username="other",
            password_hash="x",
            role=UserRole.AUTHOR,
            status=UserStatus.ACTIVE,
            is_active=True,
        )
        self.db.add(other_user)
        self.db.flush()
        other_book = Book(owner_id=other_user.id, title="另一本书", global_style_prompt="")
        self.db.add(other_book)
        self.db.flush()
        self.db.add(
            WorldExtractionJob(
                book_id=self.book.id,
                created_by_id=other_user.id,
                source_type=WorldExtractionSource.INTERNAL_BOOK,
                source_name=self.book.title,
                status=WorldExtractionJobStatus.PENDING,
                conflict_strategy=WorldConflictStrategy.MERGE,
                update_world_bible=True,
            )
        )
        self.db.commit()

        with self.assertRaises(HTTPException) as context:
            ensure_world_extraction_capacity(self.db, self.author, self.book.id)

        self.assertEqual(context.exception.status_code, 429)
        self.assertIn("当前书籍已有后台提取任务", str(context.exception.detail))

    def test_sqlite_rebuild_restores_foreign_keys_after_failure(self) -> None:
        self.db.execute(text("PRAGMA foreign_keys=ON"))
        self.db.commit()

        with self.assertRaises(Exception):
            _sqlite_rebuild_table_with_sql(
                self.db,
                table_name="books",
                rebuilt_table_sql='CREATE TABLE "books" (',
            )

        foreign_keys = self.db.execute(text("PRAGMA foreign_keys")).scalar_one()
        self.assertEqual(foreign_keys, 1)

    def test_get_admin_book_memory_returns_style_episodic_and_semantic_memory(self) -> None:
        chapter = Chapter(
            book_id=self.book.id,
            title="第一章",
            content="正文",
            sequence_number=1,
        )
        self.db.add(chapter)
        self.db.flush()
        self.db.add(ChapterEpisodicMemory(chapter_id=chapter.id, summary="第一章核心剧情", involved_characters="周奕,沈昭"))
        self.db.add(SemanticKnowledgeBase(book_id=self.book.id, entity_name="夜巡司", core_fact="夜巡司掌管禁案卷宗。"))
        self.book.extra_data = {
            "derived_style_summary": "冷静克制，动作先行。",
            "derived_style_summary_updated_at": "2026-04-04T02:40:00+00:00",
        }
        self.db.add(self.book)
        self.db.commit()

        response = get_admin_book_memory(self.book.id, current_user=self.admin, db=self.db)

        self.assertEqual(response["book"]["id"], self.book.id)
        self.assertEqual(response["style_anchor"]["source"], "derived_style_summary")
        self.assertEqual(response["style_anchor"]["content"], "冷静克制，动作先行。")
        self.assertEqual(response["episodic_memories"][0]["summary"], "第一章核心剧情")
        self.assertEqual(response["semantic_memories"][0]["entity_name"], "夜巡司")

    def test_list_ai_configs_hides_sensitive_system_fields_for_non_admin(self) -> None:
        response = list_ai_configs(scope=None, module=None, book_id=None, current_user=self.author, db=self.db)
        system_item = next(item for item in response["items"] if item["id"] == self.system_config.id)

        self.assertIsNone(system_item["base_url"])
        self.assertIsNone(system_item["system_prompt_template"])
        self.assertEqual(system_item["extra_headers"], {})
        self.assertEqual(system_item["extra_body"], {})
        self.assertIsNone(system_item["notes"])
        self.assertEqual(system_item["model_name"], "system-model")

    def test_list_ai_configs_keeps_sensitive_system_fields_for_admin(self) -> None:
        response = list_ai_configs(scope=None, module=None, book_id=None, current_user=self.admin, db=self.db)
        system_item = next(item for item in response["items"] if item["id"] == self.system_config.id)

        self.assertEqual(system_item["base_url"], "https://internal.example/v1")
        self.assertEqual(system_item["system_prompt_template"], "secret system prompt")
        self.assertEqual(system_item["extra_headers"], {"X-Internal": "1"})
        self.assertEqual(system_item["extra_body"], {"reasoning": "high"})
        self.assertEqual(system_item["notes"], "internal only")

    def test_recovery_lock_blocks_duplicate_startup_process(self) -> None:
        self.assertTrue(acquire_world_extraction_recovery_lock(self.lock_path))

        duplicate_handle = self.lock_path.open("a+", encoding="utf-8")
        try:
            self.assertFalse(main.WORLD_EXTRACTION_RECOVERY_LOCK_HANDLE is None)
            with self.assertRaises(BlockingIOError):
                main.fcntl.flock(duplicate_handle.fileno(), main.fcntl.LOCK_EX | main.fcntl.LOCK_NB)
        finally:
            duplicate_handle.close()

        release_world_extraction_recovery_lock()
        self.assertTrue(acquire_world_extraction_recovery_lock(self.lock_path))

    def test_delete_all_characters_also_clears_relations(self) -> None:
        source = Character(book_id=self.book.id, name="甲", is_active=True)
        target = Character(book_id=self.book.id, name="乙", is_active=True)
        self.db.add_all([source, target])
        self.db.flush()
        relation = Relation(
            book_id=self.book.id,
            source_character_id=source.id,
            target_character_id=target.id,
            relation_type="朋友",
        )
        self.db.add(relation)
        self.db.commit()

        result = delete_all_characters(self.book.id, self.author, self.db)

        self.assertEqual(result["deleted_character_count"], 2)
        self.assertEqual(result["deleted_relation_count"], 1)
        self.assertEqual(self.db.query(Character).count(), 0)
        self.assertEqual(self.db.query(Relation).count(), 0)

    def test_delete_all_relations_only_removes_relations(self) -> None:
        source = Character(book_id=self.book.id, name="甲", is_active=True)
        target = Character(book_id=self.book.id, name="乙", is_active=True)
        self.db.add_all([source, target])
        self.db.flush()
        self.db.add(
            Relation(
                book_id=self.book.id,
                source_character_id=source.id,
                target_character_id=target.id,
                relation_type="朋友",
            )
        )
        self.db.commit()

        result = delete_all_relations(self.book.id, self.author, self.db)

        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(self.db.query(Character).count(), 2)
        self.assertEqual(self.db.query(Relation).count(), 0)

    def test_chapter_has_extractable_content_only_accepts_body_content(self) -> None:
        outline_only = Chapter(
            book_id=self.book.id,
            title="只有大纲",
            node_type=ChapterNodeType.CHAPTER,
            outline="这是大纲",
            summary="这是摘要",
            content="",
        )
        content_chapter = Chapter(
            book_id=self.book.id,
            title="有正文",
            node_type=ChapterNodeType.CHAPTER,
            outline="这是大纲",
            summary="这是摘要",
            content="这里是真正正文",
        )

        self.assertFalse(chapter_has_extractable_content(outline_only))
        self.assertTrue(chapter_has_extractable_content(content_chapter))

    def test_select_book_chapters_for_world_extraction_ignores_non_body_and_non_chapter_nodes(self) -> None:
        chapter_with_content = Chapter(
            book_id=self.book.id,
            title="第一章",
            node_type=ChapterNodeType.CHAPTER,
            sequence_number=1,
            sort_order=1,
            content="章节正文",
        )
        chapter_outline_only = Chapter(
            book_id=self.book.id,
            title="第二章",
            node_type=ChapterNodeType.CHAPTER,
            sequence_number=2,
            sort_order=2,
            outline="仅大纲",
            summary="仅摘要",
            content="",
        )
        volume_node = Chapter(
            book_id=self.book.id,
            title="第一卷",
            node_type=ChapterNodeType.VOLUME,
            sequence_number=0,
            sort_order=0,
            content="卷说明",
        )
        self.db.add_all([chapter_with_content, chapter_outline_only, volume_node])
        self.db.commit()

        selected, skipped = select_book_chapters_for_world_extraction(
            self.db,
            self.book.id,
            chapter_scope="with_content",
        )

        self.assertEqual([item["chapter_title"] for item in selected], ["第一章"])
        self.assertEqual([item["chapter_title"] for item in skipped], ["第二章"])

    def test_project_archive_preview_marks_conflicts_and_new_items(self) -> None:
        existing_character = Character(
            book_id=self.book.id,
            name="张三",
            description="系统内人物小传",
            is_active=True,
        )
        self.db.add(existing_character)
        self.db.commit()

        preview = build_book_project_import_preview(
            self.db,
            self.book,
            {
                "modules_detected": ["characters"],
                "characters": [
                    {"name": "张三", "biography": "压缩包里更长的人物小传", "card_json": {"importance_level": "major"}},
                    {"name": "李四", "biography": "新的配角", "card_json": {"importance_level": "minor"}},
                ],
            },
        )

        item_map = {item["conflict_id"]: item for item in preview["items"]}
        self.assertEqual(item_map["character:张三"]["status"], "conflict")
        self.assertEqual(item_map["character:李四"]["status"], "new")
        self.assertEqual(preview["counts"]["conflict"], 1)
        self.assertEqual(preview["counts"]["new"], 1)

    def test_project_archive_apply_respects_per_item_decisions(self) -> None:
        existing_character = Character(
            book_id=self.book.id,
            name="张三",
            description="系统内容",
            is_active=True,
        )
        self.db.add(existing_character)
        self.db.commit()

        report = apply_book_project_archive_payload(
            db=self.db,
            book=self.book,
            current_user=self.author,
            archive_payload={
                "modules_detected": ["prompts", "characters"],
                "prompts": {
                    "global_style_prompt": "来自压缩包的新写作要求",
                },
                "characters": [
                    {"name": "张三", "biography": "压缩包想覆盖的人设", "card_json": {"importance_level": "major"}},
                    {"name": "李四", "biography": "新增人物", "card_json": {"importance_level": "minor"}},
                ],
            },
            merge_strategy="smart_merge",
            decisions={
                "prompts:global": "replace_existing",
                "character:张三": "keep_existing",
            },
        )

        self.db.refresh(self.book)
        kept_character = self.db.query(Character).filter(Character.book_id == self.book.id, Character.name == "张三").one()
        created_character = self.db.query(Character).filter(Character.book_id == self.book.id, Character.name == "李四").one()

        self.assertEqual(self.book.global_style_prompt, "来自压缩包的新写作要求")
        self.assertEqual(kept_character.description, "系统内容")
        self.assertEqual(created_character.description, "新增人物")
        self.assertTrue(report["book"]["updated"])
        self.assertEqual(report["characters"]["created"], 1)
        self.assertEqual(report["characters"]["skipped"], 1)

    def test_project_archive_apply_preserves_extended_character_card_fields(self) -> None:
        report = apply_book_project_archive_payload(
            db=self.db,
            book=self.book,
            current_user=self.author,
            archive_payload={
                "modules_detected": ["characters"],
                "characters": [
                    {
                        "name": "周奕",
                        "biography": "山门旧案唯一幸存者。",
                        "age": "二十三岁",
                        "short_term_goal": "找到失踪信使",
                        "long_term_goal": "查清灭门真相",
                        "motivation": "替家人复仇",
                        "personality": "冷静多疑",
                        "appearance": "黑衣瘦高，左眉有疤",
                        "weakness": "旧伤未愈",
                    }
                ],
            },
            merge_strategy="smart_merge",
            decisions={},
        )

        created_character = self.db.query(Character).filter(Character.book_id == self.book.id, Character.name == "周奕").one()

        self.assertEqual(created_character.card_json.get("age"), "二十三岁")
        self.assertEqual(created_character.card_json.get("short_term_goal"), "找到失踪信使")
        self.assertEqual(created_character.card_json.get("long_term_goal"), "查清灭门真相")
        self.assertEqual(created_character.card_json.get("motivation"), "替家人复仇")
        self.assertEqual(created_character.card_json.get("personality"), "冷静多疑")
        self.assertEqual(created_character.card_json.get("appearance"), "黑衣瘦高，左眉有疤")
        self.assertEqual(created_character.card_json.get("weakness"), "旧伤未愈")
        self.assertEqual(report["characters"]["created"], 1)

    def test_serialize_relation_normalizes_relation_fields(self) -> None:
        source = Character(book_id=self.book.id, name="甲", is_active=True)
        target = Character(book_id=self.book.id, name="乙", is_active=True)
        self.db.add_all([source, target])
        self.db.flush()
        relation = Relation(
            book_id=self.book.id,
            source_character_id=source.id,
            target_character_id=target.id,
            relation_type="friend",
            label="mentor student",
            importance_level="core",
        )
        relation.source_character = source
        relation.target_character = target

        payload = serialize_relation(relation)

        self.assertEqual(payload["relation_type"], "affinity")
        self.assertEqual(payload["relation_type_label"], "友好")
        self.assertEqual(payload["label"], "师徒")
        self.assertEqual(payload["importance_level"], "core")
        self.assertEqual(payload["importance_label"], "核心")

    def test_migrate_world_schema_normalizes_legacy_relations_and_creates_events(self) -> None:
        source = Character(book_id=self.book.id, name="甲", is_active=True)
        target = Character(book_id=self.book.id, name="乙", is_active=True)
        self.db.add_all([source, target])
        self.db.flush()
        first = Relation(
            book_id=self.book.id,
            source_character_id=source.id,
            target_character_id=target.id,
            relation_type="朋友",
            label=None,
            description="两人先前关系不错",
        )
        second = Relation(
            book_id=self.book.id,
            source_character_id=source.id,
            target_character_id=target.id,
            relation_type="盟友",
            label="盟友",
            description="后来一起对敌",
        )
        self.db.add_all([first, second])
        self.db.commit()

        stats = migrate_world_schema(self.db)

        relations = self.db.query(Relation).filter(Relation.book_id == self.book.id).all()
        events = self.db.query(RelationEvent).filter(RelationEvent.book_id == self.book.id).all()

        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0].relation_type, "affinity")
        self.assertTrue(relations[0].label in {"朋友", "盟友"})
        self.assertGreaterEqual(stats["merged_relations"], 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].relation_type, "affinity")


if __name__ == "__main__":
    unittest.main()
