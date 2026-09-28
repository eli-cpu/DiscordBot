"""Configuration loading for the Discord music bot.

Reads settings from environment variables (populated from a ``.env`` file via
python-dotenv when present) and exposes them through a small immutable
``Config`` dataclass.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Default cache ceiling: 4 GB.
DEFAULT_CACHE_MAX_BYTES = 4 * 1024**3
# Default idle timeout before auto-disconnect when the queue is empty.
DEFAULT_IDLE_TIMEOUT_SECONDS = 300


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    """Runtime configuration for the bot."""

    token: str
    cache_dir: Path
    cache_max_bytes: int
    idle_timeout_seconds: int


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be a positive integer, got {value}")
    return value


def load(*, load_env: bool = True) -> Config:
    """Load and validate configuration from the environment.

    Args:
        load_env: When True, load variables from a local ``.env`` file first.

    Returns:
        A populated :class:`Config`.

    Raises:
        ConfigError: If ``DISCORD_TOKEN`` is missing or a numeric var is invalid.
    """

    if load_env:
        load_dotenv()

    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise ConfigError(
            "DISCORD_TOKEN is not set. Add it to your environment or a .env file."
        )

    cache_dir = Path(os.environ.get("CACHE_DIR", "./cache")).expanduser()
    cache_max_bytes = _get_int("CACHE_MAX_BYTES", DEFAULT_CACHE_MAX_BYTES)
    idle_timeout_seconds = _get_int(
        "IDLE_TIMEOUT_SECONDS", DEFAULT_IDLE_TIMEOUT_SECONDS
    )

    return Config(
        token=token,
        cache_dir=cache_dir,
        cache_max_bytes=cache_max_bytes,
        idle_timeout_seconds=idle_timeout_seconds,
    )
