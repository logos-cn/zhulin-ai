from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

import backup_service


class BackupServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root_dir = Path(self.temp_dir.name)
        self.data_dir = self.root_dir / "data"
        self.backup_dir = self.data_dir / "db_backups"
        self.settings_path = self.data_dir / "database_backup_settings.json"
        self.lock_path = self.data_dir / "database_backup_scheduler.lock"
        self.database_path = self.root_dir / "app.sqlite3"
        self.database_url = f"sqlite:///{self.database_path}"

        self.original_root_dir = backup_service.ROOT_DIR
        self.original_data_dir = backup_service.DATA_DIR
        self.original_backup_dir = backup_service.BACKUP_DIR
        self.original_settings_path = backup_service.BACKUP_SETTINGS_PATH
        self.original_lock_path = backup_service.BACKUP_SCHEDULER_LOCK_PATH
        self.original_lock_handle = backup_service._SCHEDULER_LOCK_HANDLE

        backup_service.ROOT_DIR = self.root_dir
        backup_service.DATA_DIR = self.data_dir
        backup_service.BACKUP_DIR = self.backup_dir
        backup_service.BACKUP_SETTINGS_PATH = self.settings_path
        backup_service.BACKUP_SCHEDULER_LOCK_PATH = self.lock_path
        backup_service._SCHEDULER_LOCK_HANDLE = None

        self._build_database()

    def tearDown(self) -> None:
        backup_service.release_backup_scheduler_lock()
        backup_service.ROOT_DIR = self.original_root_dir
        backup_service.DATA_DIR = self.original_data_dir
        backup_service.BACKUP_DIR = self.original_backup_dir
        backup_service.BACKUP_SETTINGS_PATH = self.original_settings_path
        backup_service.BACKUP_SCHEDULER_LOCK_PATH = self.original_lock_path
        backup_service._SCHEDULER_LOCK_HANDLE = self.original_lock_handle
        self.temp_dir.cleanup()

    def _build_database(self) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("create table story (id integer primary key, title text not null)")
            connection.execute("insert into story (title) values (?)", ("竹林测试",))
            connection.commit()

    def test_load_backup_settings_creates_default_file(self) -> None:
        settings = backup_service.load_backup_settings()

        self.assertEqual(settings["enabled"], False)
        self.assertEqual(settings["interval_hours"], backup_service.DEFAULT_INTERVAL_HOURS)
        self.assertEqual(settings["retention_days"], backup_service.DEFAULT_RETENTION_DAYS)
        self.assertTrue(self.settings_path.exists())

    def test_update_backup_settings_persists_configuration(self) -> None:
        settings = backup_service.update_backup_settings(enabled=True, interval_hours=6, retention_days=14)

        reloaded = backup_service.load_backup_settings()
        self.assertEqual(settings["enabled"], True)
        self.assertEqual(reloaded["interval_hours"], 6)
        self.assertEqual(reloaded["retention_days"], 14)

    def test_run_database_backup_now_creates_sqlite_snapshot(self) -> None:
        backup_service.update_backup_settings(enabled=True, interval_hours=12, retention_days=7)

        result = backup_service.run_database_backup_now(self.database_url, reason="manual")

        backup_path = Path(result["backup"]["path"])
        self.assertTrue(backup_path.exists())
        with sqlite3.connect(backup_path) as connection:
            row = connection.execute("select title from story").fetchone()
        self.assertEqual(row[0], "竹林测试")

    def test_run_database_backup_now_prunes_expired_backups(self) -> None:
        backup_service.update_backup_settings(enabled=True, interval_hours=12, retention_days=1)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        expired_path = self.backup_dir / "expired.sqlite3"
        expired_path.write_bytes(b"stale-backup")
        expired_at = (datetime.now(timezone.utc) - timedelta(days=3)).timestamp()
        os.utime(expired_path, (expired_at, expired_at))

        result = backup_service.run_database_backup_now(self.database_url, reason="manual")

        self.assertFalse(expired_path.exists())
        self.assertGreaterEqual(result["deleted_expired_count"], 1)

    def test_get_backup_status_includes_recent_backups(self) -> None:
        backup_service.update_backup_settings(enabled=True, interval_hours=8, retention_days=7)
        backup_service.run_database_backup_now(self.database_url, reason="manual")

        status = backup_service.get_backup_status(self.database_url)

        self.assertEqual(status["database_engine"], "sqlite")
        self.assertEqual(status["supported"], True)
        self.assertTrue(status["recent_backups"])
        self.assertIsNotNone(status["latest_backup"])
        self.assertIsNotNone(status["next_backup_at"])

    def test_resolve_backup_file_path_rejects_invalid_filename(self) -> None:
        with self.assertRaises(ValueError):
            backup_service.resolve_backup_file_path("../bad.sqlite3")

        with self.assertRaises(ValueError):
            backup_service.resolve_backup_file_path("bad.txt")

    def test_restore_database_from_backup_reverts_database_content(self) -> None:
        backup_service.update_backup_settings(enabled=True, interval_hours=8, retention_days=7)
        created = backup_service.run_database_backup_now(self.database_url, reason="manual")
        backup_filename = created["backup"]["filename"]

        with sqlite3.connect(self.database_path) as connection:
            connection.execute("delete from story")
            connection.execute("insert into story (title) values (?)", ("已被修改",))
            connection.commit()

        result = backup_service.restore_database_from_backup(
            self.database_url,
            filename=backup_filename,
            create_safety_backup=True,
        )

        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute("select title from story").fetchone()

        self.assertEqual(row[0], "竹林测试")
        self.assertEqual(result["restored_from"]["filename"], backup_filename)
        self.assertIsNotNone(result["safety_backup"])


if __name__ == "__main__":
    unittest.main()
