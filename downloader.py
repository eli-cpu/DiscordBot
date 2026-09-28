"""YouTube resolution and audio download via yt-dlp.

Wraps ``yt_dlp.YoutubeDL`` to:
  * resolve a user query (URL or free-text search) to a canonical video id and
    title without downloading, and
  * download the best audio and post-process it to a ``.opus`` file inside the
    cache directory.

All blocking yt-dlp calls are executed via ``asyncio.to_thread`` so they never
block the event loop.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yt_dlp

# yt-dlp names extracted audio files with the video id as the stem when we use
# the output template below; the ExtractAudio postprocessor rewrites the
# extension to .opus.
_OUTTMPL_STEM = "%(id)s"
_AUDIO_EXT = "opus"

# Heuristic: treat anything that looks like a URL as a direct link; otherwise
# search YouTube.
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


class DownloadError(RuntimeError):
    """Raised when a query cannot be resolved or downloaded."""


@dataclass(frozen=True)
class DownloadResult:
    """Result of a successful resolve+download."""

    video_id: str
    title: str
    file_path: str
    size_bytes: int


@dataclass(frozen=True)
class ResolvedInfo:
    """Cheap metadata resolution (no media download)."""

    video_id: str
    title: str


def is_url(query: str) -> bool:
    """Return True if the query should be treated as a direct URL."""
    return bool(_URL_RE.match(query.strip()))


def build_query(query: str) -> str:
    """Return the yt-dlp target for a query.

    URLs pass through unchanged; free-text becomes a single-result YouTube
    search via the ``ytsearch1:`` prefix.
    """
    q = query.strip()
    if not q:
        raise DownloadError("Empty query.")
    return q if is_url(q) else f"ytsearch1:{q}"


def _base_opts(cache_dir: Path) -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "default_search": "ytsearch",
        "outtmpl": str(Path(cache_dir) / f"{_OUTTMPL_STEM}.%(ext)s"),
    }


def _first_entry(info: dict[str, Any]) -> dict[str, Any]:
    """Return the concrete video info dict from a search or direct result."""
    if info is None:
        raise DownloadError("No result found.")
    entries = info.get("entries")
    if entries is not None:
        entries = [e for e in entries if e]
        if not entries:
            raise DownloadError("No results found for query.")
        return entries[0]
    return info


def _resolve_sync(query: str, cache_dir: Path) -> ResolvedInfo:
    target = build_query(query)
    opts = _base_opts(cache_dir)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=False)
    except yt_dlp.utils.DownloadError as exc:  # type: ignore[attr-defined]
        raise DownloadError(str(exc)) from exc
    entry = _first_entry(info)
    video_id = entry.get("id")
    if not video_id:
        raise DownloadError("Could not determine video id.")
    title = entry.get("title") or video_id
    return ResolvedInfo(video_id=video_id, title=title)


def _download_sync(query: str, cache_dir: Path) -> DownloadResult:
    target = build_query(query)
    opts = _base_opts(cache_dir)
    opts.update(
        {
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": _AUDIO_EXT,
                }
            ],
        }
    )
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=True)
    except yt_dlp.utils.DownloadError as exc:  # type: ignore[attr-defined]
        raise DownloadError(str(exc)) from exc

    entry = _first_entry(info)
    video_id = entry.get("id")
    if not video_id:
        raise DownloadError("Could not determine video id after download.")
    title = entry.get("title") or video_id

    file_path = Path(cache_dir) / f"{video_id}.{_AUDIO_EXT}"
    if not file_path.exists():
        raise DownloadError(
            f"Expected audio file {file_path} was not produced by yt-dlp."
        )
    size_bytes = file_path.stat().st_size
    return DownloadResult(
        video_id=video_id,
        title=title,
        file_path=str(file_path),
        size_bytes=size_bytes,
    )


async def resolve(query: str, cache_dir: Path) -> ResolvedInfo:
    """Resolve a query to (video_id, title) without downloading media."""
    return await asyncio.to_thread(_resolve_sync, query, cache_dir)


async def resolve_and_download(query: str, cache_dir: Path) -> DownloadResult:
    """Download best audio for a query and extract it to a ``.opus`` file."""
    return await asyncio.to_thread(_download_sync, query, cache_dir)
