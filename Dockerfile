# Discord music bot image.
#
# Installs ffmpeg (audio extraction + voice playback) and libopus (Opus
# encoding for Discord voice), then the Python dependencies, and finally runs
# the bot. The audio cache lives on a mounted volume at /data/cache so it
# survives container restarts.

FROM python:3.12-slim

# System dependencies:
#   ffmpeg   - used by yt-dlp for audio extraction and by discord.py for playback
#   libopus0 - Opus codec runtime required for Discord voice
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libopus0 \
    && rm -rf /var/lib/apt/lists/*

# Don't buffer stdout/stderr so logs show up immediately in `docker logs`.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CACHE_DIR=/data/cache

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application source.
COPY main.py bot.py player.py cache.py downloader.py config.py ./

# Run as a non-root user and give it ownership of the cache volume path.
RUN useradd --create-home --uid 10001 botuser \
    && mkdir -p /data/cache \
    && chown -R botuser:botuser /data
USER botuser

# The cache directory is a volume so downloaded audio persists across restarts.
VOLUME ["/data/cache"]

CMD ["python", "main.py"]
