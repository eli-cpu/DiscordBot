# Discord Music Bot

A Discord bot that plays YouTube audio in voice channels via slash commands.
Tracks are downloaded once, transcoded to Opus, and cached on disk with SQLite
metadata and a size-capped LRU policy (default 4 GB), so repeat plays are
instant and storage stays bounded.

## Features

- Slash commands for full playback control
- Plays from a search term or a YouTube URL
- Downloads to Opus and caches; least-recently-used tracks are evicted once the
  cache exceeds its size cap
- Auto-joins the caller's voice channel on `/play`
- Auto-leaves when the queue has been idle too long, or when no humans remain in
  the channel
- Queue, skip, pause/resume, volume, and loop (off / track / queue) modes
- Ships as a Docker image that installs ffmpeg and everything else and runs
  automatically

## Commands

| Command | Description |
| --- | --- |
| `/play <query>` | Join your voice channel and queue a song (search term or URL) |
| `/skip` | Skip the current track |
| `/stop` | Stop playback and clear the queue |
| `/queue` | Show the current queue |
| `/nowplaying` | Show the current track |
| `/pause` | Pause playback |
| `/resume` | Resume playback |
| `/volume <0-100>` | Set the playback volume |
| `/loop <off\|track\|queue>` | Set the loop mode |
| `/leave` | Disconnect the bot from voice |

## Configuration

Configuration is read from environment variables (a local `.env` file is loaded
automatically when present). Copy `.env.example` to `.env` and fill it in:

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | yes | — | Your Discord bot token |
| `CACHE_DIR` | no | `./cache` | Where cached audio and the SQLite DB live |
| `CACHE_MAX_BYTES` | no | `4294967296` (4 GB) | Cache size ceiling in bytes |
| `IDLE_TIMEOUT_SECONDS` | no | `300` | Idle time before auto-disconnect |

## Discord application setup

1. Create an application and bot at the
   [Discord Developer Portal](https://discord.com/developers/applications).
2. Copy the bot token into `DISCORD_TOKEN`.
3. Invite the bot with the `bot` and `applications.commands` scopes, and grant
   it permission to connect and speak in voice channels.

Slash commands are synced globally on startup; they can take up to an hour to
appear the first time.

## Running locally

Requires Python 3.12+ and ffmpeg on your `PATH`.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then edit .env and set DISCORD_TOKEN
.venv/bin/python main.py
```

The bot verifies ffmpeg is installed at startup and exits with a clear message
if it is missing.

### Running the tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## Running with Docker

The image installs ffmpeg, libopus, and the Python dependencies, then runs the
bot. The cache is stored on a named volume so it persists across restarts.

Using Docker Compose (recommended):

```bash
cp .env.example .env   # set DISCORD_TOKEN
docker compose up --build -d
docker compose logs -f
```

Or with plain Docker:

```bash
docker build -t discord-music-bot .
docker run -d --name music-bot \
  --env-file .env \
  -e CACHE_DIR=/data/cache \
  -v music-cache:/data/cache \
  discord-music-bot
```

The `.env` file is never baked into the image; it is passed in at runtime.

## Security note

Never commit your real bot token. `.env` is gitignored. If a token has been
exposed, rotate it in the Discord Developer Portal.

## Project layout

| File | Responsibility |
| --- | --- |
| `main.py` | Entrypoint: load config, verify ffmpeg, run the bot |
| `bot.py` | Discord client, startup checks, slash commands, auto-leave |
| `player.py` | Per-guild playback queue and voice loop |
| `cache.py` | SQLite metadata store and LRU cache manager |
| `downloader.py` | yt-dlp wrapper: resolve and download to Opus |
| `config.py` | Environment-based configuration |
