"""Tests for the SQLite metadata store and LRU eviction."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cache import MetadataStore  # noqa: E402


def _make_file(cache_dir: Path, name: str, size: int) -> Path:
    p = cache_dir / name
    p.write_bytes(b"\0" * size)
    return p


def test_add_get_and_total_size(tmp_path):
    store = MetadataStore(tmp_path)
    f = _make_file(tmp_path, "a.opus", 100)
    store.add("vid_a", "Title A", str(f), 100, last_accessed=1.0)

    rec = store.get("vid_a")
    assert rec is not None
    assert rec.title == "Title A"
    assert rec.size_bytes == 100
    assert store.total_size() == 100
    assert store.get("missing") is None


def test_touch_updates_ordering(tmp_path):
    store = MetadataStore(tmp_path)
    store.add("a", "A", str(_make_file(tmp_path, "a.opus", 10)), 10, last_accessed=1.0)
    store.add("b", "B", str(_make_file(tmp_path, "b.opus", 10)), 10, last_accessed=2.0)

    # 'a' is oldest. Touch it so 'b' becomes the eviction candidate.
    store.touch("a", when=3.0)

    evicted = store.evict_until_under(10)
    assert evicted == ["b"]
    assert store.get("a") is not None
    assert store.get("b") is None


def test_evict_until_under_removes_files(tmp_path):
    store = MetadataStore(tmp_path)
    fa = _make_file(tmp_path, "a.opus", 100)
    fb = _make_file(tmp_path, "b.opus", 100)
    fc = _make_file(tmp_path, "c.opus", 100)
    store.add("a", "A", str(fa), 100, last_accessed=1.0)
    store.add("b", "B", str(fb), 100, last_accessed=2.0)
    store.add("c", "C", str(fc), 100, last_accessed=3.0)

    # Limit 150 -> must drop to <=150. Evicts oldest (a), then b (200->100).
    evicted = store.evict_until_under(150)
    assert evicted == ["a", "b"]
    assert not fa.exists()
    assert not fb.exists()
    assert fc.exists()
    assert store.total_size() == 100


def test_evict_noop_when_under_limit(tmp_path):
    store = MetadataStore(tmp_path)
    store.add("a", "A", str(_make_file(tmp_path, "a.opus", 50)), 50, last_accessed=1.0)
    assert store.evict_until_under(1000) == []
    assert store.get("a") is not None


def test_evict_protects_video(tmp_path):
    store = MetadataStore(tmp_path)
    store.add("a", "A", str(_make_file(tmp_path, "a.opus", 100)), 100, last_accessed=1.0)
    store.add("b", "B", str(_make_file(tmp_path, "b.opus", 100)), 100, last_accessed=2.0)

    # 'a' is oldest but protected; eviction must skip it and take 'b'.
    evicted = store.evict_until_under(100, protect="a")
    assert evicted == ["b"]
    assert store.get("a") is not None
    assert store.get("b") is None


def test_remove_deletes_row_and_file(tmp_path):
    store = MetadataStore(tmp_path)
    f = _make_file(tmp_path, "a.opus", 10)
    store.add("a", "A", str(f), 10)
    store.remove("a")
    assert store.get("a") is None
    assert not f.exists()


def test_persistence_across_instances(tmp_path):
    store = MetadataStore(tmp_path)
    store.add("a", "A", str(_make_file(tmp_path, "a.opus", 10)), 10)
    # New instance over the same dir should see the row.
    store2 = MetadataStore(tmp_path)
    assert store2.get("a") is not None
