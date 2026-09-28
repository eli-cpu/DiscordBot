"""Discord bot wiring: client, startup checks, and slash commands.

The bot uses discord.py's application (slash) commands. On startup it verifies
that ffmpeg is available (required by both yt-dlp post-processing and voice
playback) and then syncs the command tree.

Core commands (Task 6):
  /play <query>   join the caller's voice channel and queue a track
  /skip           skip the current track
  /stop           stop playback and clear the queue (stays connected)
  /queue          show the current queue
  /nowplaying     show the currently playing track
  /leave          disconnect from voice

Pause/resume/volume/loop (Task 7) and auto-leave (Task 8) are layered on in
later tasks.
"""

from __future__ import annotations

import logging
import shutil

import discord
from discord import app_commands

from cache import CacheManager
from config import Config
from player import LoopMode, PlayableTrack, PlayerRegistry

logger = logging.getLogger(__name__)


class MissingFFmpegError(RuntimeError):
    """Raised at startup when the ffmpeg binary cannot be found on PATH."""


def ensure_ffmpeg() -> str:
    """Return the ffmpeg path or raise :class:`MissingFFmpegError`."""
    path = shutil.which("ffmpeg")
    if not path:
        raise MissingFFmpegError(
            "ffmpeg was not found on PATH. Install it and try again "
            "(the Docker image installs it automatically)."
        )
    return path


class MusicBot(discord.Client):
    """Discord client that owns the cache manager and per-guild players."""

    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        # voice_states is needed to observe members joining/leaving channels
        # (used by auto-leave in Task 8) and to manage voice connections.
        intents.voice_states = True
        super().__init__(intents=intents)

        self.config = config
        self.cache = CacheManager(config.cache_dir, config.cache_max_bytes)
        self.players = PlayerRegistry(
            idle_timeout=config.idle_timeout_seconds,
            on_idle=self._on_player_idle,
        )
        self.tree = app_commands.CommandTree(self)
        _register_commands(self)

    async def _on_player_idle(self, guild_id: int) -> None:
        """Auto-disconnect a guild's player after it has been idle too long."""
        logger.info("Auto-leaving guild %s after idle timeout.", guild_id)
        await self.players.remove(guild_id)

    async def setup_hook(self) -> None:
        # Sync the global command tree. Global commands can take up to an hour
        # to propagate the first time; guild-scoped copies are added on join
        # for faster availability during development is out of scope here.
        await self.tree.sync()
        logger.info("Application commands synced.")

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (id=%s)", self.user, getattr(self.user, "id", "?"))

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Leave when the bot's voice channel has no human members left."""
        # Ignore the bot's own state changes.
        if self.user is not None and member.id == self.user.id:
            return
        guild = member.guild
        if guild is None:
            return
        player = self.players.get(guild.id)
        if player is None or player.voice is None:
            return
        channel = player.voice.channel
        if channel is None:
            return
        if not _has_human_listeners(channel, self.user):
            logger.info("Voice channel in guild %s is empty; leaving.", guild.id)
            await self.players.remove(guild.id)


async def _resolve_voice_channel(
    interaction: discord.Interaction,
) -> discord.VoiceChannel | None:
    """Return the caller's current voice channel, or None if not in one."""
    member = interaction.user
    voice = getattr(member, "voice", None)
    if voice is None or voice.channel is None:
        return None
    return voice.channel


def _has_human_listeners(channel, me) -> bool:
    """Return True if a voice channel has at least one non-bot member.

    The bot itself is excluded so it will leave a channel where only bots
    remain.
    """
    me_id = getattr(me, "id", None)
    for m in getattr(channel, "members", []):
        if getattr(m, "bot", False):
            continue
        if me_id is not None and getattr(m, "id", None) == me_id:
            continue
        return True
    return False


def _register_commands(bot: "MusicBot") -> None:
    tree = bot.tree

    @tree.command(name="play", description="Play a song from YouTube (search or URL).")
    @app_commands.describe(query="A search term or a YouTube URL")
    async def play(interaction: discord.Interaction, query: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        channel = await _resolve_voice_channel(interaction)
        if channel is None:
            await interaction.response.send_message(
                "Join a voice channel first, then run /play.", ephemeral=True
            )
            return

        # Downloading can take a while; defer to avoid the 3s interaction timeout.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            record = await bot.cache.get_or_download(query)
        except Exception as exc:  # noqa: BLE001 - surface a friendly message
            logger.exception("Failed to fetch %r", query)
            await interaction.followup.send(
                f"Couldn't fetch that track: {exc}", ephemeral=True
            )
            return

        player = bot.players.get_or_create(interaction.guild.id)
        try:
            await player.connect(channel)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to join voice channel")
            await interaction.followup.send(
                f"Couldn't join your voice channel: {exc}", ephemeral=True
            )
            return

        player.enqueue(
            PlayableTrack(
                video_id=record.video_id,
                title=record.title,
                file_path=record.file_path,
                requested_by=str(interaction.user),
            )
        )

        position = player.queue_size()
        if player.current is None and position <= 1:
            msg = f"Now playing: **{record.title}**"
        else:
            msg = f"Queued **{record.title}** (position {position})."
        await interaction.followup.send(msg, ephemeral=True)

    @tree.command(name="skip", description="Skip the current track.")
    async def skip(interaction: discord.Interaction) -> None:
        player = _player_for(bot, interaction)
        if player is None or not player.skip():
            await interaction.response.send_message(
                "Nothing is playing.", ephemeral=True
            )
            return
        await interaction.response.send_message("Skipped.", ephemeral=True)

    @tree.command(name="stop", description="Stop playback and clear the queue.")
    async def stop(interaction: discord.Interaction) -> None:
        player = _player_for(bot, interaction)
        if player is None:
            await interaction.response.send_message(
                "Nothing is playing.", ephemeral=True
            )
            return
        await player.stop()
        await interaction.response.send_message(
            "Stopped and cleared the queue.", ephemeral=True
        )

    @tree.command(name="queue", description="Show the current queue.")
    async def queue(interaction: discord.Interaction) -> None:
        player = _player_for(bot, interaction)
        if player is None:
            await interaction.response.send_message(
                "The queue is empty.", ephemeral=True
            )
            return

        lines: list[str] = []
        if player.current is not None:
            lines.append(f"**Now playing:** {player.current.title}")
        pending = player.queue_snapshot()
        if pending:
            lines.append("**Up next:**")
            for i, track in enumerate(pending, start=1):
                lines.append(f"{i}. {track.title}")
        if not lines:
            await interaction.response.send_message(
                "The queue is empty.", ephemeral=True
            )
            return
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @tree.command(name="nowplaying", description="Show the current track.")
    async def nowplaying(interaction: discord.Interaction) -> None:
        player = _player_for(bot, interaction)
        if player is None or player.current is None:
            await interaction.response.send_message(
                "Nothing is playing.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"Now playing: **{player.current.title}**", ephemeral=True
        )

    @tree.command(name="pause", description="Pause the current track.")
    async def pause(interaction: discord.Interaction) -> None:
        player = _player_for(bot, interaction)
        if player is None or not player.pause():
            await interaction.response.send_message(
                "Nothing is playing.", ephemeral=True
            )
            return
        await interaction.response.send_message("Paused.", ephemeral=True)

    @tree.command(name="resume", description="Resume the paused track.")
    async def resume(interaction: discord.Interaction) -> None:
        player = _player_for(bot, interaction)
        if player is None or not player.resume():
            await interaction.response.send_message(
                "Nothing is paused.", ephemeral=True
            )
            return
        await interaction.response.send_message("Resumed.", ephemeral=True)

    @tree.command(name="volume", description="Set the playback volume (0-100).")
    @app_commands.describe(level="Volume percentage from 0 to 100")
    async def volume(interaction: discord.Interaction, level: int) -> None:
        if not 0 <= level <= 100:
            await interaction.response.send_message(
                "Volume must be between 0 and 100.", ephemeral=True
            )
            return
        player = _player_for(bot, interaction)
        if player is None:
            await interaction.response.send_message(
                "Nothing is playing.", ephemeral=True
            )
            return
        player.set_volume(level / 100.0)
        await interaction.response.send_message(
            f"Volume set to {level}%.", ephemeral=True
        )

    @tree.command(name="loop", description="Set the loop mode.")
    @app_commands.describe(mode="off, track, or queue")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="off", value="off"),
            app_commands.Choice(name="track", value="track"),
            app_commands.Choice(name="queue", value="queue"),
        ]
    )
    async def loop(
        interaction: discord.Interaction, mode: app_commands.Choice[str]
    ) -> None:
        player = _player_for(bot, interaction)
        if player is None:
            await interaction.response.send_message(
                "Nothing is playing.", ephemeral=True
            )
            return
        player.set_loop(LoopMode(mode.value))
        await interaction.response.send_message(
            f"Loop mode set to **{mode.value}**.", ephemeral=True
        )

    @tree.command(name="leave", description="Disconnect the bot from voice.")
    async def leave(interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return
        player = bot.players.get(interaction.guild.id)
        if player is None or player.voice is None:
            await interaction.response.send_message(
                "I'm not in a voice channel.", ephemeral=True
            )
            return
        await bot.players.remove(interaction.guild.id)
        await interaction.response.send_message("Left the voice channel.", ephemeral=True)


def _player_for(bot: "MusicBot", interaction: discord.Interaction):
    """Return the guild's player if one exists, else None."""
    if interaction.guild is None:
        return None
    return bot.players.get(interaction.guild.id)
