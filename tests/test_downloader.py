"""Tests for downloader query handling and result parsing (yt-dlp mocked)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import downloader  # noqa: E402
from downloader import DownloadError  # noqa: E402


def test_is_url():
    assert downloader.is_url("https://youtube.com/watch?v=abc")
    assert downloader.is_url("HTTP://x.y")
    assert not downloader.is_url("never gonna give you up")
    assert not downloader.is_url("youtube.com/watch")  # no scheme


def test_build_query_url_passthrough():
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert downloader.build_query(url) == url


def test_build_query_search_prefix():
    assert downloader.build_query("rick astley") == "ytsearch1:rick astley"


def test_build_query_empty_raises():
    with pytest.raises(DownloadError):
        downloader.build_query("   ")


def test_first_entry_from_search():
    info = {"entries": [{"id": "vid1", "title": "One"}, {"id": "vid2"}]}
    entry = downloader._first_entry(info)
    assert entry["id"] == "vid1"


def test_first_entry_direct():
    info = {"id": "vid9", "title": "Direct"}
    assert downloader._first_entry(info)["id"] == "vid9"


def test_first_entry_empty_raises():
    with pytest.raises(DownloadError):
        downloader._first_entry({"entries": []})
    with pytest.raises(DownloadError):
        downloader._first_entry(None)


def _fake_ydl(info):
    """Build a context-manager mock whose extract_info returns ``info``."""
    inst = mock.MagicMock()
    inst.extract_info.return_value = info
    cm = mock.MagicMock()
    cm.__enter__.return_value = inst
    cm.__exit__.return_value = False
    return cm, inst


async def test_resolve_returns_metadata(tmp_path):
    cm, inst = _fake_ydl({"entries": [{"id": "abc123", "title": "My Song"}]})
    with mock.patch.object(downloader.yt_dlp, "YoutubeDL", return_value=cm):
        res = await downloader.resolve("my song", tmp_path)
    assert res.video_id == "abc123"
    assert res.title == "My Song"
    # Called with download disabled.
    _, kwargs = inst.extract_info.call_args
    assert kwargs.get("download") is False


async def test_download_returns_result_and_checks_file(tmp_path):
    # Simulate the .opus file yt-dlp would have produced.
    (tmp_path / "abc123.opus").write_bytes(b"\0" * 42)
    cm, _ = _fake_ydl({"id": "abc123", "title": "My Song"})
    with mock.patch.object(downloader.yt_dlp, "YoutubeDL", return_value=cm):
        res = await downloader.resolve_and_download(
            "https://youtu.be/abc123", tmp_path
        )
    assert res.video_id == "abc123"
    assert res.title == "My Song"
    assert res.file_path == str(tmp_path / "abc123.opus")
    assert res.size_bytes == 42


async def test_download_missing_file_raises(tmp_path):
    cm, _ = _fake_ydl({"id": "nofile", "title": "Ghost"})
    with mock.patch.object(downloader.yt_dlp, "YoutubeDL", return_value=cm):
        with pytest.raises(DownloadError):
            await downloader.resolve_and_download("ghost", tmp_path)
