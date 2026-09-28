"""Tests for CacheManager.get_or_download (downloader mocked)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cache  # noqa: E402
from downloader import DownloadResult, ResolvedInfo  # noqa: E402


def _write(cache_dir: Path, video_id: str, size: int) -> str:
    p = cache_dir / f"{video_id}.opus"
    p.write_bytes(b"\0" * size)
    return str(p)


async def test_cache_hit_no_download_and_touches(tmp_path):
    mgr = cache.CacheManager(tmp_path, max_bytes=10_000)
    path = _write(tmp_path, "vid1", 100)
    mgr.store.add("vid1", "Song", path, 100, last_accessed=1.0)

    async def fake_resolve(query, cache_dir):
        return ResolvedInfo("vid1", "Song")

    with mock.patch.object(cache.downloader, "resolve", side_effect=fake_resolve), \
         mock.patch.object(cache.downloader, "resolve_and_download") as m_dl:
        rec = await mgr.get_or_download("song")

    m_dl.assert_not_called()
    assert rec.video_id == "vid1"
    assert rec.last_accessed > 1.0  # touched


async def test_cache_miss_downloads_inserts_and_evicts(tmp_path):
    mgr = cache.CacheManager(tmp_path, max_bytes=150)

    # Pre-existing older track that should be evicted after the new download.
    old_path = _write(tmp_path, "old", 100)
    mgr.store.add("old", "Old", old_path, 100, last_accessed=1.0)

    new_path = _write(tmp_path, "new", 100)

    async def fake_resolve(query, cache_dir):
        return ResolvedInfo("new", "New Song")

    async def fake_dl(query, cache_dir):
        return DownloadResult("new", "New Song", new_path, 100)

    with mock.patch.object(cache.downloader, "resolve", side_effect=fake_resolve), \
         mock.patch.object(cache.downloader, "resolve_and_download", side_effect=fake_dl):
        rec = await mgr.get_or_download("new song")

    assert rec.video_id == "new"
    # New track is protected and present; old one evicted to satisfy 150 cap.
    assert mgr.store.get("new") is not None
    assert mgr.store.get("old") is None
    assert Path(new_path).exists()
    assert not Path(old_path).exists()


async def test_concurrent_identical_requests_download_once(tmp_path):
    mgr = cache.CacheManager(tmp_path, max_bytes=10_000)
    new_path = _write(tmp_path, "vid", 100)
    dl_calls = 0

    async def fake_resolve(query, cache_dir):
        await asyncio.sleep(0)  # yield so both tasks interleave before the lock
        return ResolvedInfo("vid", "Song")

    async def fake_dl(query, cache_dir):
        nonlocal dl_calls
        dl_calls += 1
        await asyncio.sleep(0.01)
        return DownloadResult("vid", "Song", new_path, 100)

    with mock.patch.object(cache.downloader, "resolve", side_effect=fake_resolve), \
         mock.patch.object(cache.downloader, "resolve_and_download", side_effect=fake_dl):
        results = await asyncio.gather(
            mgr.get_or_download("song"),
            mgr.get_or_download("song"),
        )

    assert dl_calls == 1
    assert all(r.video_id == "vid" for r in results)
