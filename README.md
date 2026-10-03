# Media Fetch Bot

Telegram bot that downloads YouTube media with yt-dlp and sends the finished file back to the chat.

## Features

- YouTube / youtu.be / YouTube Music links
- Deletes the submitted URL message after accepting it
- One editable media panel instead of creating a new message for every menu/status screen
- Video thumbnail, title, duration and a compact clickable YouTube link
- Audio or Video selection
- Video buttons generated from resolutions actually available for the video
- Estimated or exact size shown next to audio/video options when yt-dlp exposes enough metadata
- Download percentage and progress bar
- Audio: original, MP3 128, MP3 192, M4A, Opus
- ffmpeg merge/conversion through yt-dlp
- Temporary files are deleted after processing
- SQLite cache stores Telegram file_id values, so the same media/profile can be resent without downloading and uploading it again
- Optional Telegram user-ID allowlist
- Optional local Telegram Bot API endpoint for large uploads

## Requirements

- Python 3.11+
- ffmpeg
- Telegram bot token from BotFather

Arch Linux:

    sudo pacman -S python ffmpeg

## Install

    git clone https://github.com/sqwiziiy/Media-Fetch-Bot.git
    cd Media-Fetch-Bot
    python -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env

Edit .env:

    BOT_TOKEN=your_bot_token_here
    ALLOWED_USER_IDS=your_numeric_telegram_user_id

If you do not know your Telegram numeric user ID, leave ALLOWED_USER_IDS empty for the first launch, start the bot, send /id, put the returned ID into .env, then restart.

Run:

    python main.py

## Usage

1. Send a YouTube URL.
2. The original URL message is removed.
3. The bot shows the video's thumbnail and information.
4. Choose Audio or Video.
5. Choose a format or quality; the button also shows the expected file size.
6. The same panel shows download progress.
7. The finished file is sent to the chat.
8. Temporary media is removed from disk.

## Large files

Telegram's hosted Bot API limits new bot uploads to about 50 MB, but this does not make those media qualities unavailable. The bot still allows and downloads selections up to 2000 MB. If the finished file is too large for the currently configured hosted Bot API, the panel reports that delivery is unavailable for now; temporary-link delivery is planned.

A self-hosted Telegram Bot API server can upload files up to 2000 MB. Set:

    TELEGRAM_API_BASE=http://127.0.0.1:8081

Only options estimated above 2000 MB are marked with ❌ and blocked.

A temporary-link delivery mode for files that cannot be sent directly to Telegram is planned, but is not implemented yet.

When switching an existing bot from Telegram's hosted Bot API to a local Bot API server, log the bot out from the hosted API first.

## Environment

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| BOT_TOKEN | yes | - | Telegram bot token |
| ALLOWED_USER_IDS | recommended | empty | Comma-separated numeric Telegram user IDs; empty allows everyone |
| TELEGRAM_API_BASE | no | Telegram hosted API | Local Bot API base URL |
| WORK_DIR | no | ./downloads | Temporary download directory |
| CACHE_DB | no | ./media_cache.sqlite3 | SQLite file_id cache |

Use this project only for media you are allowed to download.
