from __future__ import annotations

import asyncio
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp


SUPPORTED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}


@dataclass(frozen=True, slots=True)
class MediaInfo:
    media_id: str
    title: str
    duration: int | None
    webpage_url: str
    heights: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class DownloadedFile:
    path: Path
    temp_dir: Path

    def cleanup(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)


def is_supported_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and (parsed.hostname or "").lower() in SUPPORTED_HOSTS


def _extract_info_sync(url: str) -> MediaInfo:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info or info.get("_type") == "playlist":
        raise ValueError("Send a link to a single video, not a playlist.")

    heights = sorted(
        {
            int(fmt["height"])
            for fmt in info.get("formats", [])
            if fmt.get("height") and fmt.get("vcodec") not in {None, "none"}
        }
    )

    if not heights:
        raise ValueError("No downloadable video formats were found.")

    return MediaInfo(
        media_id=str(info["id"]),
        title=str(info.get("title") or info["id"]),
        duration=int(info["duration"]) if info.get("duration") is not None else None,
        webpage_url=str(info.get("webpage_url") or url),
        heights=tuple(heights),
    )


async def extract_info(url: str) -> MediaInfo:
    return await asyncio.to_thread(_extract_info_sync, url)


def _pick_output_file(temp_dir: Path) -> Path:
    files = [
        path
        for path in temp_dir.iterdir()
        if path.is_file() and path.suffix not in {".part", ".ytdl"}
    ]
    if not files:
        raise RuntimeError("yt-dlp finished, but no output file was created.")
    return max(files, key=lambda p: p.stat().st_mtime_ns)


def _download_sync(url: str, profile: str, work_dir: Path) -> DownloadedFile:
    temp_dir = Path(tempfile.mkdtemp(prefix="media-fetch-", dir=work_dir))
    output_template = str(temp_dir / "%(title).160B [%(id)s].%(ext)s")

    common: dict = {
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 5,
        "fragment_retries": 5,
    }

    if profile.startswith("video:"):
        height = int(profile.split(":", 1)[1])
        common.update(
            {
                "format": (
                    f"bestvideo[height={height}][ext=mp4]+bestaudio[ext=m4a]/"
                    f"bestvideo[height={height}]+bestaudio/"
                    f"best[height={height}]"
                ),
                "merge_output_format": "mp4",
            }
        )
    elif profile == "audio:source":
        common["format"] = "bestaudio/best"
    elif profile.startswith("audio:mp3_"):
        bitrate = profile.removeprefix("audio:mp3_")
        common.update(
            {
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": bitrate,
                    }
                ],
            }
        )
    elif profile == "audio:m4a":
        common.update(
            {
                "format": "bestaudio[ext=m4a]/bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "m4a",
                    }
                ],
            }
        )
    elif profile == "audio:opus":
        common.update(
            {
                "format": "bestaudio[acodec*=opus]/bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "opus",
                    }
                ],
            }
        )
    else:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise ValueError(f"Unknown download profile: {profile}")

    try:
        with yt_dlp.YoutubeDL(common) as ydl:
            ydl.download([url])
        return DownloadedFile(path=_pick_output_file(temp_dir), temp_dir=temp_dir)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


async def download(url: str, profile: str, work_dir: Path) -> DownloadedFile:
    return await asyncio.to_thread(_download_sync, url, profile, work_dir)
