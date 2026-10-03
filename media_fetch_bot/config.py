from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    allowed_user_ids: frozenset[int]
    telegram_api_base: str | None
    work_dir: Path
    cache_db: Path


def _parse_user_ids(raw: str) -> frozenset[int]:
    if not raw.strip():
        return frozenset()
    return frozenset(int(item.strip()) for item in raw.split(",") if item.strip())


def load_settings() -> Settings:
    load_dotenv()

    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is not set. Copy .env.example to .env and add your bot token.")

    work_dir = Path(os.getenv("WORK_DIR", "./downloads")).expanduser().resolve()
    cache_db = Path(os.getenv("CACHE_DB", "./media_cache.sqlite3")).expanduser().resolve()
    api_base = os.getenv("TELEGRAM_API_BASE", "").strip() or None

    work_dir.mkdir(parents=True, exist_ok=True)
    cache_db.parent.mkdir(parents=True, exist_ok=True)

    return Settings(
        bot_token=token,
        allowed_user_ids=_parse_user_ids(os.getenv("ALLOWED_USER_IDS", "")),
        telegram_api_base=api_base.rstrip("/") if api_base else None,
        work_dir=work_dir,
        cache_db=cache_db,
    )
