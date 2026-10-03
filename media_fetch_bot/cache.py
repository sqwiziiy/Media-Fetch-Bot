from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CacheEntry:
    file_id: str
    file_name: str | None


class TelegramFileCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _init_db(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_files (
                    media_id TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    file_name TEXT,
                    PRIMARY KEY (media_id, profile)
                )
                """
            )

    def get(self, media_id: str, profile: str) -> CacheEntry | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT file_id, file_name FROM telegram_files WHERE media_id = ? AND profile = ?",
                (media_id, profile),
            ).fetchone()
        return CacheEntry(*row) if row else None

    def put(self, media_id: str, profile: str, file_id: str, file_name: str | None) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO telegram_files(media_id, profile, file_id, file_name)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(media_id, profile) DO UPDATE SET
                    file_id = excluded.file_id,
                    file_name = excluded.file_name
                """,
                (media_id, profile, file_id, file_name),
            )
