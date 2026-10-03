from __future__ import annotations

import asyncio
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import yt_dlp


SUPPORTED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}


ProgressCallback = Callable[[float | None, str], None]


@dataclass(frozen=True, slots=True)
class DownloadOption:
    profile: str
    label: str
    size_bytes: int | None
    exact_size: bool = False


@dataclass(frozen=True, slots=True)
class MediaInfo:
    media_id: str
    title: str
    duration: int | None
    webpage_url: str
    thumbnail_url: str | None
    video_options: tuple[DownloadOption, ...]
    audio_options: tuple[DownloadOption, ...]


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


def _format_size(fmt: dict, duration: int | None) -> tuple[int | None, bool]:
    if fmt.get("filesize"):
        return int(fmt["filesize"]), True
    if fmt.get("filesize_approx"):
        return int(fmt["filesize_approx"]), False

    bitrate = fmt.get("tbr") or fmt.get("abr") or fmt.get("vbr")
    if duration and bitrate:
        return int(float(bitrate) * 1000 / 8 * duration), False
    return None, False


def _quality_score(fmt: dict, preferred_ext: str | None = None) -> tuple:
    return (
        1 if preferred_ext and fmt.get("ext") == preferred_ext else 0,
        float(fmt.get("quality") or 0),
        float(fmt.get("fps") or 0),
        float(fmt.get("tbr") or 0),
        float(fmt.get("filesize") or fmt.get("filesize_approx") or 0),
    )


def _pick_best(formats: list[dict], preferred_ext: str | None = None) -> dict | None:
    if not formats:
        return None
    return max(formats, key=lambda fmt: _quality_score(fmt, preferred_ext))


def _sum_sizes(
    first: tuple[int | None, bool],
    second: tuple[int | None, bool],
) -> tuple[int | None, bool]:
    if first[0] is None or second[0] is None:
        return None, False
    return first[0] + second[0], first[1] and second[1]


def _build_video_options(formats: list[dict], duration: int | None) -> tuple[DownloadOption, ...]:
    audio_only = [
        fmt
        for fmt in formats
        if fmt.get("vcodec") == "none" and fmt.get("acodec") not in {None, "none"}
    ]
    best_m4a_audio = _pick_best(audio_only, "m4a") or _pick_best(audio_only)
    audio_size = _format_size(best_m4a_audio, duration) if best_m4a_audio else (None, False)

    heights = sorted(
        {
            int(fmt["height"])
            for fmt in formats
            if fmt.get("height") and fmt.get("vcodec") not in {None, "none"}
        }
    )

    options: list[DownloadOption] = []
    for height in heights:
        exact_height = [
            fmt
            for fmt in formats
            if fmt.get("height") == height and fmt.get("vcodec") not in {None, "none"}
        ]
        video_only = [fmt for fmt in exact_height if fmt.get("acodec") == "none"]
        progressive = [fmt for fmt in exact_height if fmt.get("acodec") not in {None, "none"}]

        best_video = _pick_best(video_only, "mp4") or _pick_best(video_only)
        best_progressive = _pick_best(progressive, "mp4") or _pick_best(progressive)

        if best_video and best_m4a_audio:
            size_bytes, exact = _sum_sizes(
                _format_size(best_video, duration),
                audio_size,
            )
        elif best_progressive:
            size_bytes, exact = _format_size(best_progressive, duration)
        else:
            size_bytes, exact = None, False

        options.append(
            DownloadOption(
                profile=f"video:{height}",
                label=f"{height}p",
                size_bytes=size_bytes,
                exact_size=exact,
            )
        )

    return tuple(options)


def _build_audio_options(formats: list[dict], duration: int | None) -> tuple[DownloadOption, ...]:
    audio_only = [
        fmt
        for fmt in formats
        if fmt.get("vcodec") == "none" and fmt.get("acodec") not in {None, "none"}
    ]
    best_audio = _pick_best(audio_only)
    best_m4a = _pick_best(audio_only, "m4a")
    opus_audio = [fmt for fmt in audio_only if "opus" in str(fmt.get("acodec", "")).lower()]
    best_opus = _pick_best(opus_audio)

    source_size = _format_size(best_audio, duration) if best_audio else (None, False)
    m4a_size = _format_size(best_m4a, duration) if best_m4a else source_size
    opus_size = _format_size(best_opus, duration) if best_opus else source_size

    def mp3_size(kbps: int) -> int | None:
        if not duration:
            return None
        return int(duration * kbps * 1000 / 8 * 1.02)

    return (
        DownloadOption("audio:source", "⭐ Original", source_size[0], source_size[1]),
        DownloadOption("audio:mp3_128", "MP3 128", mp3_size(128), False),
        DownloadOption("audio:mp3_192", "MP3 192", mp3_size(192), False),
        DownloadOption("audio:m4a", "M4A", m4a_size[0], m4a_size[1]),
        DownloadOption("audio:opus", "Opus", opus_size[0], opus_size[1]),
    )


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

    duration = int(info["duration"]) if info.get("duration") is not None else None
    formats = list(info.get("formats") or [])
    video_options = _build_video_options(formats, duration)
    if not video_options:
        raise ValueError("No downloadable video formats were found.")

    thumbnails = info.get("thumbnails") or []
    thumbnail_url = info.get("thumbnail")
    if not thumbnail_url and thumbnails:
        thumbnail_url = thumbnails[-1].get("url")

    return MediaInfo(
        media_id=str(info["id"]),
        title=str(info.get("title") or info["id"]),
        duration=duration,
        webpage_url=str(info.get("webpage_url") or url),
        thumbnail_url=str(thumbnail_url) if thumbnail_url else None,
        video_options=video_options,
        audio_options=_build_audio_options(formats, duration),
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
    return max(files, key=lambda path: path.stat().st_mtime_ns)


def _download_sync(
    url: str,
    profile: str,
    work_dir: Path,
    progress_callback: ProgressCallback | None = None,
) -> DownloadedFile:
    temp_dir = Path(tempfile.mkdtemp(prefix="media-fetch-", dir=work_dir))
    output_template = str(temp_dir / "%(title).160B [%(id)s].%(ext)s")

    def progress_hook(data: dict) -> None:
        if not progress_callback:
            return
        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes") or 0
            percent = (float(downloaded) / float(total) * 100) if total else None
            progress_callback(percent, "downloading")
        elif status == "finished":
            progress_callback(100.0, "processing")

    def postprocessor_hook(data: dict) -> None:
        if progress_callback and data.get("status") == "started":
            progress_callback(100.0, "processing")

    common: dict = {
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 5,
        "fragment_retries": 5,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
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


async def download(
    url: str,
    profile: str,
    work_dir: Path,
    progress_callback: ProgressCallback | None = None,
) -> DownloadedFile:
    return await asyncio.to_thread(
        _download_sync,
        url,
        profile,
        work_dir,
        progress_callback,
    )
