from __future__ import annotations

import asyncio
import html
import logging
import secrets
from dataclasses import dataclass

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .cache import TelegramFileCache
from .config import Settings, load_settings
from .downloader import MediaInfo, download, extract_info, is_supported_url


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("media-fetch-bot")

router = Router()
settings: Settings
cache: TelegramFileCache
sessions: dict[str, "MediaSession"] = {}


@dataclass(slots=True)
class MediaSession:
    user_id: int
    info: MediaInfo


def allowed(user_id: int) -> bool:
    return not settings.allowed_user_ids or user_id in settings.allowed_user_ids


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


def main_keyboard(token: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🎵 Audio", callback_data=f"kind:{token}:audio")
    builder.button(text="🎬 Video", callback_data=f"kind:{token}:video")
    builder.adjust(2)
    return builder.as_markup()


def audio_keyboard(token: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⭐ Original", callback_data=f"get:{token}:audio:source")
    builder.button(text="MP3 128", callback_data=f"get:{token}:audio:mp3_128")
    builder.button(text="MP3 192", callback_data=f"get:{token}:audio:mp3_192")
    builder.button(text="M4A", callback_data=f"get:{token}:audio:m4a")
    builder.button(text="Opus", callback_data=f"get:{token}:audio:opus")
    builder.button(text="← Back", callback_data=f"back:{token}")
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup()


def video_keyboard(token: str, heights: tuple[int, ...]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    displayed = heights[-10:]
    for height in reversed(displayed):
        builder.button(text=f"{height}p", callback_data=f"get:{token}:video:{height}")
    builder.button(text="← Back", callback_data=f"back:{token}")
    builder.adjust(2)
    return builder.as_markup()


def get_session(token: str, user_id: int) -> MediaSession | None:
    session = sessions.get(token)
    if session and session.user_id == user_id:
        return session
    return None


@router.message(CommandStart())
async def start(message: Message) -> None:
    if not message.from_user:
        return
    if not allowed(message.from_user.id):
        await message.answer(
            f"⛔ This bot is private. Your Telegram user ID: <code>{message.from_user.id}</code>"
        )
        return
    await message.answer(
        "Send me a YouTube link. I will let you choose audio/video and the exact available quality."
    )


@router.message(Command("id"))
async def show_id(message: Message) -> None:
    if message.from_user:
        await message.answer(f"Your Telegram user ID: <code>{message.from_user.id}</code>")


@router.message(F.text)
async def handle_url(message: Message) -> None:
    if not message.from_user or not message.text:
        return
    if not allowed(message.from_user.id):
        await message.answer("⛔ Access denied.")
        return

    url = message.text.strip()
    if not is_supported_url(url):
        await message.answer("Send a valid YouTube / youtu.be link.")
        return

    status = await message.answer("🔎 Reading video information…")
    try:
        info = await extract_info(url)
    except Exception as exc:
        log.exception("Could not extract media info")
        await status.edit_text(
            f"❌ Could not read this video:\n<code>{html.escape(str(exc))}</code>"
        )
        return

    token = secrets.token_urlsafe(6)
    sessions[token] = MediaSession(user_id=message.from_user.id, info=info)

    await status.edit_text(
        f"<b>{html.escape(info.title)}</b>\n"
        f"⏱ {format_duration(info.duration)}\n\n"
        "What do you want to download?",
        reply_markup=main_keyboard(token),
    )


@router.callback_query(F.data.startswith("kind:"))
async def choose_kind(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.data or not callback.message:
        return
    _, token, kind = callback.data.split(":", 2)
    session = get_session(token, callback.from_user.id)
    if not session:
        await callback.answer("This menu expired. Send the link again.", show_alert=True)
        return

    if kind == "audio":
        await callback.message.edit_reply_markup(reply_markup=audio_keyboard(token))
    else:
        await callback.message.edit_reply_markup(
            reply_markup=video_keyboard(token, session.info.heights)
        )
    await callback.answer()


@router.callback_query(F.data.startswith("back:"))
async def go_back(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.data or not callback.message:
        return
    token = callback.data.split(":", 1)[1]
    if not get_session(token, callback.from_user.id):
        await callback.answer("This menu expired. Send the link again.", show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=main_keyboard(token))
    await callback.answer()


@router.callback_query(F.data.startswith("get:"))
async def download_media(callback: CallbackQuery, bot: Bot) -> None:
    if not callback.from_user or not callback.data or not callback.message:
        return

    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Invalid option.", show_alert=True)
        return

    _, token, kind, option = parts
    session = get_session(token, callback.from_user.id)
    if not session:
        await callback.answer("This menu expired. Send the link again.", show_alert=True)
        return

    profile = f"{kind}:{option}"
    await callback.answer("Starting…")

    cached = await asyncio.to_thread(cache.get, session.info.media_id, profile)
    if cached:
        try:
            await bot.send_document(
                chat_id=callback.message.chat.id,
                document=cached.file_id,
                caption=f"⚡ Cached • {html.escape(session.info.title)}",
            )
            return
        except Exception:
            log.exception("Cached file_id failed; downloading again")

    status = await callback.message.answer("⬇️ Downloading…")
    downloaded = None
    try:
        downloaded = await download(
            session.info.webpage_url,
            profile,
            settings.work_dir,
        )
        size_mb = downloaded.path.stat().st_size / (1024 * 1024)
        await status.edit_text(f"📤 Uploading… ({size_mb:.1f} MB)")

        if not settings.telegram_api_base and size_mb > 49:
            raise RuntimeError(
                "The file is larger than the standard Telegram Bot API upload limit. "
                "Configure TELEGRAM_API_BASE to use a local Bot API server (up to 2 GB)."
            )

        sent = await bot.send_document(
            chat_id=callback.message.chat.id,
            document=FSInputFile(downloaded.path),
            caption=html.escape(session.info.title),
            request_timeout=60 * 30,
        )

        if sent.document:
            await asyncio.to_thread(
                cache.put,
                session.info.media_id,
                profile,
                sent.document.file_id,
                sent.document.file_name,
            )

        await status.delete()
    except Exception as exc:
        log.exception("Download/upload failed")
        await status.edit_text(f"❌ Failed:\n<code>{html.escape(str(exc))}</code>")
    finally:
        if downloaded:
            await asyncio.to_thread(downloaded.cleanup)


def create_bot() -> Bot:
    default = DefaultBotProperties(parse_mode=ParseMode.HTML)
    if settings.telegram_api_base:
        api = TelegramAPIServer.from_base(settings.telegram_api_base, is_local=True)
        session = AiohttpSession(api=api)
        return Bot(token=settings.bot_token, session=session, default=default)
    return Bot(token=settings.bot_token, default=default)


async def main() -> None:
    global settings, cache
    settings = load_settings()
    cache = TelegramFileCache(settings.cache_db)

    if not settings.allowed_user_ids:
        log.warning("ALLOWED_USER_IDS is empty: anyone who finds the bot can use it.")

    bot = create_bot()
    dp = Dispatcher()
    dp.include_router(router)

    log.info("Media Fetch Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
