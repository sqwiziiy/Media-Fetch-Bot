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
from .downloader import DownloadOption, MediaInfo, download, extract_info, is_supported_url


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("media-fetch-bot")

HOSTED_UPLOAD_LIMIT = 49 * 1024 * 1024
LOCAL_UPLOAD_LIMIT = 2000 * 1024 * 1024

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


def format_size(size_bytes: int | None, exact: bool = True) -> str:
    if size_bytes is None:
        return "? MB"
    prefix = "" if exact else "~"
    mib = size_bytes / (1024 * 1024)
    if mib >= 1024:
        return f"{prefix}{mib / 1024:.2f} GB"
    if mib >= 100:
        return f"{prefix}{mib:.0f} MB"
    return f"{prefix}{mib:.1f} MB"


def panel_caption(info: MediaInfo, body: str) -> str:
    title = html.escape(info.title)
    url = html.escape(info.webpage_url, quote=True)
    return (
        f"<b>{title}</b>\n"
        f"⏱ {format_duration(info.duration)}\n"
        f'🔗 <a href="{url}">Open on YouTube</a>\n\n'
        f"{body}"
    )


def progress_bar(percent: float | None) -> str:
    if percent is None:
        return "▱▱▱▱▱▱▱▱▱▱"
    value = max(0.0, min(100.0, percent))
    filled = min(10, int(value // 10))
    return "▰" * filled + "▱" * (10 - filled)


def main_keyboard(token: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🎵 Audio", callback_data=f"kind:{token}:audio")
    builder.button(text="🎬 Video", callback_data=f"kind:{token}:video")
    builder.adjust(2)
    return builder.as_markup()


def option_block_reason(option: DownloadOption) -> str | None:
    if option.size_bytes is not None and option.size_bytes > LOCAL_UPLOAD_LIMIT:
        return "too_large"
    if (
        not settings.telegram_api_base
        and option.size_bytes is not None
        and option.size_bytes > HOSTED_UPLOAD_LIMIT
    ):
        return "hosted_limit"
    return None


def option_button_text(option: DownloadOption) -> str:
    size = format_size(option.size_bytes, option.exact_size)
    reason = option_block_reason(option)
    suffix = " ❌" if reason == "too_large" else " 🚧" if reason == "hosted_limit" else ""
    return f"{option.label} • {size}{suffix}"


def options_keyboard(token: str, options: tuple[DownloadOption, ...]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for option in options:
        reason = option_block_reason(option)
        callback_data = (
            f"no:{token}:{reason}"
            if reason
            else f"get:{token}:{option.profile}"
        )
        builder.button(text=option_button_text(option), callback_data=callback_data)
    builder.button(text="← Back", callback_data=f"back:{token}")
    builder.adjust(1)
    return builder.as_markup()


def video_keyboard(token: str, info: MediaInfo) -> InlineKeyboardMarkup:
    return options_keyboard(token, tuple(reversed(info.video_options[-10:])))


def audio_keyboard(token: str, info: MediaInfo) -> InlineKeyboardMarkup:
    return options_keyboard(token, info.audio_options)


def get_session(token: str, user_id: int) -> MediaSession | None:
    session = sessions.get(token)
    if session and session.user_id == user_id:
        return session
    return None


def get_option(info: MediaInfo, profile: str) -> DownloadOption | None:
    for option in (*info.video_options, *info.audio_options):
        if option.profile == profile:
            return option
    return None


async def edit_panel(
    message: Message,
    caption: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        if message.photo:
            await message.edit_caption(caption=caption, reply_markup=reply_markup)
        else:
            await message.edit_text(caption, reply_markup=reply_markup)
    except Exception as exc:
        log.debug("Panel edit skipped: %s", exc)


async def send_media_panel(bot: Bot, chat_id: int, info: MediaInfo, token: str) -> Message:
    caption = panel_caption(info, "What do you want to download?")
    if info.thumbnail_url:
        try:
            return await bot.send_photo(
                chat_id=chat_id,
                photo=info.thumbnail_url,
                caption=caption,
                reply_markup=main_keyboard(token),
            )
        except Exception:
            log.exception("Could not send thumbnail; falling back to a text panel")

    return await bot.send_message(
        chat_id=chat_id,
        text=caption,
        reply_markup=main_keyboard(token),
    )


async def download_with_progress(
    panel: Message,
    info: MediaInfo,
    profile: str,
):
    queue: asyncio.Queue[tuple[float | None, str]] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def report(percent: float | None, phase: str) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (percent, phase))

    task = asyncio.create_task(
        download(
            info.webpage_url,
            profile,
            settings.work_dir,
            progress_callback=report,
        )
    )

    last_percent = -10
    last_phase = ""
    last_edit_at = 0.0

    while not task.done() or not queue.empty():
        try:
            percent, phase = await asyncio.wait_for(queue.get(), timeout=0.35)
            while not queue.empty():
                percent, phase = queue.get_nowait()
        except TimeoutError:
            continue

        now = loop.time()
        if phase == "downloading":
            shown = int(percent) if percent is not None else None
            should_edit = (
                shown is None
                or shown >= 100
                or shown - last_percent >= 3
                or now - last_edit_at >= 1.5
            )
            if not should_edit:
                continue

            if shown is not None:
                last_percent = shown
                percentage_text = f"{shown}%"
            else:
                percentage_text = "…"

            await edit_panel(
                panel,
                panel_caption(
                    info,
                    f"⬇️ <b>Downloading… {percentage_text}</b>\n"
                    f"<code>{progress_bar(percent)}</code>",
                ),
            )
            last_phase = phase
            last_edit_at = now

        elif phase == "processing" and last_phase != "processing":
            await edit_panel(
                panel,
                panel_caption(info, "🔧 <b>Processing media…</b>"),
            )
            last_phase = phase
            last_edit_at = now

    return await task


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
async def handle_url(message: Message, bot: Bot) -> None:
    if not message.from_user or not message.text:
        return
    if not allowed(message.from_user.id):
        await message.answer("⛔ Access denied.")
        return

    url = message.text.strip()
    if not is_supported_url(url):
        await message.answer("Send a valid YouTube / youtu.be link.")
        return

    try:
        await message.delete()
    except Exception:
        log.debug("Could not delete the original URL message")

    try:
        info = await extract_info(url)
    except Exception as exc:
        log.exception("Could not extract media info")
        await bot.send_message(
            chat_id=message.chat.id,
            text=f"❌ Could not read this video:\n<code>{html.escape(str(exc))}</code>",
        )
        return

    token = secrets.token_urlsafe(6)
    sessions[token] = MediaSession(user_id=message.from_user.id, info=info)
    await send_media_panel(bot, message.chat.id, info, token)


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
        await edit_panel(
            callback.message,
            panel_caption(session.info, "🎵 <b>Choose audio format:</b>"),
            audio_keyboard(token, session.info),
        )
    else:
        await edit_panel(
            callback.message,
            panel_caption(session.info, "🎬 <b>Choose video quality:</b>"),
            video_keyboard(token, session.info),
        )

    await callback.answer()


@router.callback_query(F.data.startswith("back:"))
async def go_back(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.data or not callback.message:
        return

    token = callback.data.split(":", 1)[1]
    session = get_session(token, callback.from_user.id)
    if not session:
        await callback.answer("This menu expired. Send the link again.", show_alert=True)
        return

    await edit_panel(
        callback.message,
        panel_caption(session.info, "What do you want to download?"),
        main_keyboard(token),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("no:"))
async def unavailable_option(callback: CallbackQuery) -> None:
    if not callback.data:
        return

    _, token, reason = callback.data.split(":", 2)
    if not callback.from_user or not get_session(token, callback.from_user.id):
        await callback.answer("This menu expired. Send the link again.", show_alert=True)
        return

    if reason == "too_large":
        text = "❌ Estimated file size is over 2000 MB, so this option is unavailable."
    else:
        text = (
            "🚧 This file is estimated to exceed the standard Telegram Bot API upload limit. "
            "Temporary download links are not implemented yet."
        )
    await callback.answer(text, show_alert=True)


@router.callback_query(F.data.startswith("get:"))
async def download_media(callback: CallbackQuery, bot: Bot) -> None:
    if not callback.from_user or not callback.data or not callback.message:
        return

    parts = callback.data.split(":", 3)
    if len(parts) != 4:
        await callback.answer("Invalid option.", show_alert=True)
        return

    _, token, kind, option_name = parts
    profile = f"{kind}:{option_name}"
    session = get_session(token, callback.from_user.id)
    if not session:
        await callback.answer("This menu expired. Send the link again.", show_alert=True)
        return

    option = get_option(session.info, profile)
    if not option:
        await callback.answer("This option is no longer available.", show_alert=True)
        return

    cached = await asyncio.to_thread(cache.get, session.info.media_id, profile)
    if cached:
        await callback.answer("Cached")
        try:
            await edit_panel(
                callback.message,
                panel_caption(session.info, "⚡ <b>Sending cached file…</b>"),
            )
            await bot.send_document(
                chat_id=callback.message.chat.id,
                document=cached.file_id,
                caption=html.escape(session.info.title),
            )
            await edit_panel(
                callback.message,
                panel_caption(session.info, "✅ <b>Sent from Telegram cache.</b>"),
                main_keyboard(token),
            )
            return
        except Exception:
            log.exception("Cached file_id failed; downloading again")
    else:
        reason = option_block_reason(option)
        if reason:
            if reason == "too_large":
                text = "❌ This option is estimated to be over 2000 MB."
            else:
                text = (
                    "🚧 This option is estimated to exceed the current Telegram upload limit. "
                    "Temporary download links will be added later."
                )
            await callback.answer(text, show_alert=True)
            return
        await callback.answer("Starting…")

    downloaded = None
    try:
        await edit_panel(
            callback.message,
            panel_caption(
                session.info,
                "⬇️ <b>Downloading… 0%</b>\n<code>▱▱▱▱▱▱▱▱▱▱</code>",
            ),
        )

        downloaded = await download_with_progress(
            callback.message,
            session.info,
            profile,
        )

        size_bytes = downloaded.path.stat().st_size
        size_text = format_size(size_bytes)

        if size_bytes > LOCAL_UPLOAD_LIMIT:
            await edit_panel(
                callback.message,
                panel_caption(
                    session.info,
                    f"❌ <b>Unavailable.</b>\nThe finished file is {size_text}, over the 2000 MB limit.",
                ),
                main_keyboard(token),
            )
            return

        if not settings.telegram_api_base and size_bytes > HOSTED_UPLOAD_LIMIT:
            await edit_panel(
                callback.message,
                panel_caption(
                    session.info,
                    f"🚧 <b>Direct delivery is not available yet.</b>\n"
                    f"The finished file is {size_text}. Temporary download links will be added later.",
                ),
                main_keyboard(token),
            )
            return

        await edit_panel(
            callback.message,
            panel_caption(
                session.info,
                f"📤 <b>Sending to Telegram…</b>\n📦 {size_text}",
            ),
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

        await edit_panel(
            callback.message,
            panel_caption(
                session.info,
                f"✅ <b>Done.</b>\n📦 {size_text}",
            ),
            main_keyboard(token),
        )

    except Exception as exc:
        log.exception("Download/upload failed")
        await edit_panel(
            callback.message,
            panel_caption(
                session.info,
                f"❌ <b>Failed:</b>\n<code>{html.escape(str(exc))}</code>",
            ),
            main_keyboard(token),
        )
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
