"""On-disk audio cache with SQLite metadata and LRU eviction.

Downloaded Opus files live in the cache directory; a SQLite database in the
same directory tracks metadata (video id, title, path, size, last-accessed
time). The cache is capped at a byte limit and evicts least-recently-used
tracks when the limit is exceeded.

This module contains the storage/metadata layer (Task 2). The higher-level
``get_or_download`` orchestration and the downloader wiring are added in a
later task.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

import downloader

DB_FILENAME = "cache.db"


@dataclass(frozen=True)
class TrackRecord:
    """Metadata for a single cached track."""

    video_id: str
    title: str
    file_path: str
    size_bytes: int
    last_accessed: float


class MetadataStore:
    """SQLite-backed store for cached track metadata.

    The store owns only the metadata and the audio files it references; it does
    not download anything. All timestamps are POSIX seconds (float).
    """

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.cache_dir / DB_FILENAME
        self.init_db()

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_db(self) -> None:
        """Create the tracks table if it does not already exist."""
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tracks (
                    video_id      TEXT PRIMARY KEY,
                    title         TEXT NOT NULL,
                    file_path     TEXT NOT NULL,
                    size_bytes    INTEGER NOT NULL,
                    last_accessed REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_last_accessed "
                "ON tracks (last_accessed)"
            )

    def get(self, video_id: str) -> Optional[TrackRecord]:
        """Return the record for ``video_id`` or None if not cached."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT video_id, title, file_path, size_bytes, last_accessed "
                "FROM tracks WHERE video_id = ?",
                (video_id,),
            ).fetchone()
        return _row_to_record(row) if row else None

    def add(
        self,
        video_id: str,
        title: str,
        file_path: str,
        size_bytes: int,
        last_accessed: Optional[float] = None,
    ) -> TrackRecord:
        """Insert or replace a track record and return it."""
        ts = time.time() if last_accessed is None else last_accessed
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tracks "
                "(video_id, title, file_path, size_bytes, last_accessed) "
                "VALUES (?, ?, ?, ?, ?)",
                (video_id, title, file_path, size_bytes, ts),
            )
        return TrackRecord(video_id, title, file_path, size_bytes, ts)

    def touch(self, video_id: str, when: Optional[float] = None) -> None:
        """Update the last-accessed timestamp for a track to now."""
        ts = time.time() if when is None else when
        with self._connect() as conn:
            conn.execute(
                "UPDATE tracks SET last_accessed = ? WHERE video_id = ?",
                (ts, video_id),
            )

    def total_size(self) -> int:
        """Return the total size in bytes of all cached tracks."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) AS total FROM tracks"
            ).fetchone()
        return int(row["total"])

    def remove(self, video_id: str) -> None:
        """Delete a track's DB row and its file (best effort)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT file_path FROM tracks WHERE video_id = ?",
                (video_id,),
            ).fetchone()
            if row is not None:
                _unlink_quietly(Path(row["file_path"]))
                conn.execute("DELETE FROM tracks WHERE video_id = ?", (video_id,))

    def evict_until_under(self, limit_bytes: int, *, protect: str = "") -> list[str]:
        """Evict least-recently-used tracks until total size <= limit_bytes.

        Args:
            limit_bytes: Target ceiling for total cache size.
            protect: Optional video_id that must never be evicted (e.g. the
                track that was just downloaded and is about to play).

        Returns:
            The list of evicted video ids, oldest first.
        """
        evicted: list[str] = []
        with self._connect() as conn:
            total = int(
                conn.execute(
                    "SELECT COALESCE(SUM(size_bytes), 0) AS total FROM tracks"
                ).fetchone()["total"]
            )
            if total <= limit_bytes:
                return evicted

            rows = conn.execute(
                "SELECT video_id, file_path, size_bytes FROM tracks "
                "ORDER BY last_accessed ASC"
            ).fetchall()

            for row in rows:
                if total <= limit_bytes:
                    break
                if row["video_id"] == protect:
                    continue
                _unlink_quietly(Path(row["file_path"]))
                conn.execute(
                    "DELETE FROM tracks WHERE video_id = ?", (row["video_id"],)
                )
                total -= int(row["size_bytes"])
                evicted.append(row["video_id"])
        return evicted


class CacheManager:
    """High-level cache orchestration over a :class:`MetadataStore`.

    Resolves a user query to a video id, serves cached audio when present, and
    otherwise downloads it, records metadata, and enforces the byte limit via
    LRU eviction. A per-video-id asyncio lock ensures concurrent requests for
    the same track download only once.
    """

    def __init__(self, cache_dir: Path, max_bytes: int) -> None:
        self.cache_dir = Path(cache_dir)
        self.max_bytes = max_bytes
        self.store = MetadataStore(self.cache_dir)
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _lock_for(self, video_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(video_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[video_id] = lock
            return lock

    async def get_or_download(self, query: str) -> TrackRecord:
        """Return a cached track for ``query``, downloading it if needed.

        The returned track's file is guaranteed to exist on disk and is
        protected from eviction during this call.
        """
        resolved = await downloader.resolve(query, self.cache_dir)
        video_id = resolved.video_id

        lock = await self._lock_for(video_id)
        async with lock:
            existing = self.store.get(video_id)
            if existing is not None and Path(existing.file_path).exists():
                self.store.touch(video_id)
                return self.store.get(video_id)  # refreshed timestamp

            # Cache miss (or file vanished): (re)download.
            result = await downloader.resolve_and_download(query, self.cache_dir)
            record = self.store.add(
                video_id=result.video_id,
                title=result.title,
                file_path=result.file_path,
                size_bytes=result.size_bytes,
            )
            # Enforce the size cap, but never evict the track we just fetched.
            self.store.evict_until_under(self.max_bytes, protect=result.video_id)
            return record


def _row_to_record(row: sqlite3.Row) -> TrackRecord:
    return TrackRecord(
        video_id=row["video_id"],
        title=row["title"],
        file_path=row["file_path"],
        size_bytes=int(row["size_bytes"]),
        last_accessed=float(row["last_accessed"]),
    )


def _unlink_quietly(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError, OSError):
        path.unlink()
