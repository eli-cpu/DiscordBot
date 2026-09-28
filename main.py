"""Entrypoint: load config, verify ffmpeg, and run the bot."""

from __future__ import annotations

import logging
import sys

from bot import MissingFFmpegError, MusicBot, ensure_ffmpeg
from config import ConfigError, load

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("main")


def main() -> int:
    try:
        config = load()
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        return 1

    try:
        ffmpeg_path = ensure_ffmpeg()
    except MissingFFmpegError as exc:
        logger.error("%s", exc)
        return 1
    logger.info("Using ffmpeg at %s", ffmpeg_path)

    bot = MusicBot(config)
    bot.run(config.token, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
