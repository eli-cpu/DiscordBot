"""Tests for config loading."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for key in (
        "DISCORD_TOKEN",
        "CACHE_DIR",
        "CACHE_MAX_BYTES",
        "IDLE_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)


def test_defaults(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "abc")
    cfg = config.load(load_env=False)
    assert cfg.token == "abc"
    assert cfg.cache_dir == Path("./cache")
    assert cfg.cache_max_bytes == config.DEFAULT_CACHE_MAX_BYTES
    assert cfg.idle_timeout_seconds == config.DEFAULT_IDLE_TIMEOUT_SECONDS


def test_overrides(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "tok")
    monkeypatch.setenv("CACHE_DIR", "/tmp/mycache")
    monkeypatch.setenv("CACHE_MAX_BYTES", "2048")
    monkeypatch.setenv("IDLE_TIMEOUT_SECONDS", "60")
    cfg = config.load(load_env=False)
    assert cfg.cache_dir == Path("/tmp/mycache")
    assert cfg.cache_max_bytes == 2048
    assert cfg.idle_timeout_seconds == 60


def test_missing_token_raises(monkeypatch):
    with pytest.raises(config.ConfigError):
        config.load(load_env=False)


def test_blank_token_raises(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "   ")
    with pytest.raises(config.ConfigError):
        config.load(load_env=False)


def test_invalid_int_raises(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "tok")
    monkeypatch.setenv("CACHE_MAX_BYTES", "notanumber")
    with pytest.raises(config.ConfigError):
        config.load(load_env=False)


def test_nonpositive_int_raises(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "tok")
    monkeypatch.setenv("IDLE_TIMEOUT_SECONDS", "0")
    with pytest.raises(config.ConfigError):
        config.load(load_env=False)
