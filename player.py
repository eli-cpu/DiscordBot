"""Per-guild audio playback.

``GuildPlayer`` owns a queue of tracks, a voice client, and a background
playback loop that streams cached Opus files to Discord via ffmpeg. A module
level ``PlayerRegistry`` maps guild ids to their player.

Playback loop design:
  * The loop awaits the next track from an ``asyncio.Queue``.
  * It starts playback with ``discord.FFmpegOpusAudio`` (no re-encode) at the
    default volume, or ``FFmpegPCMAudio`` + ``PCMVolumeTransformer`` when the
    volume has been changed (Task 7).
  * An ``asyncio.Event`` is set from the voice client's ``after`` callback to
    signal track completion, so the loop can proceed to the next item.

Loop modes and auto-leave timers are introduced in later tasks; this module is
structured to accommodate them without reshuffling the core loop.
"""

from __future__ import annotations

import asyncio
import enum
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import discord

logger = logging.getLogger(__name__)

# Async callback invoked with a guild id when a player goes idle.
IdleCallback = Callable[[int], Awaitable[None]]

# Default per-track volume (1.0 == unchanged / no PCM transform).
DEFAULT_VOLUME = 1.0


class LoopMode(enum.Enum):
    OFF = "off"
    TRACK = "track"
    QUEUE = "queue"


@dataclass
class PlayableTrack:
    """A track queued for playback."""

    video_id: str
    title: str
    file_path: str
    requested_by: Optional[str] = None


class GuildPlayer:
    """Manages the queue and playback for a single guild."""

    def __init__(
        self,
        guild_id: int,
        idle_timeout: Optional[float] = None,
        on_idle: Optional["IdleCallback"] = None,
    ) -> None:
        self.guild_id = guild_id
        # Seconds to wait with an empty queue before auto-disconnecting. None
        # disables the idle timer (used in tests that drive the loop manually).
        self.idle_timeout = idle_timeout
        # Async callback invoked with the guild id when the idle timer expires.
        self._on_idle = on_idle
        self._queue: asyncio.Queue[PlayableTrack] = asyncio.Queue()
        self.voice: Optional[discord.VoiceClient] = None
        self.current: Optional[PlayableTrack] = None
        self.volume: float = DEFAULT_VOLUME
        self.loop_mode: LoopMode = LoopMode.OFF

        self._play_task: Optional[asyncio.Task] = None
        self._track_done = asyncio.Event()
        self._closed = False

        # The live audio source for the current track (used to change volume in
        # place when it is a PCMVolumeTransformer).
        self._current_source: Optional[discord.AudioSource] = None
        # Set when the current track is skipped/stopped so the loop does NOT
        # re-enqueue it under an active loop mode.
        self._interrupted = False

    # ---- Connection -----------------------------------------------------

    async def connect(self, channel: discord.VoiceChannel) -> None:
        """Connect to (or move to) the given voice channel."""
        if self.voice is not None and self.voice.is_connected():
            if self.voice.channel != channel:
                await self.voice.move_to(channel)
        else:
            self.voice = await channel.connect()
        self._ensure_play_task()

    def _ensure_play_task(self) -> None:
        if self._play_task is None or self._play_task.done():
            self._closed = False
            self._play_task = asyncio.create_task(self._run())

    # ---- Queue ----------------------------------------------------------

    def enqueue(self, track: PlayableTrack) -> None:
        """Add a track to the end of the queue."""
        self._queue.put_nowait(track)

    def queue_snapshot(self) -> list[PlayableTrack]:
        """Return a list copy of the pending queue (excludes current)."""
        return list(self._queue._queue)  # type: ignore[attr-defined]

    def queue_size(self) -> int:
        return self._queue.qsize()

    # ---- Controls -------------------------------------------------------

    def skip(self) -> bool:
        """Stop the current track so the loop advances. Returns True if playing.

        A skip counts as an interruption, so a track being looped (TRACK mode)
        is not replayed; the loop moves on to the next queued item instead.
        """
        if self.voice is not None and (
            self.voice.is_playing() or self.voice.is_paused()
        ):
            self._interrupted = True
            self.voice.stop()
            return True
        return False

    def pause(self) -> bool:
        """Pause playback. Returns True if something was actually paused."""
        if self.voice is not None and self.voice.is_playing():
            self.voice.pause()
            return True
        return False

    def resume(self) -> bool:
        """Resume playback. Returns True if something was actually resumed."""
        if self.voice is not None and self.voice.is_paused():
            self.voice.resume()
            return True
        return False

    def set_volume(self, volume: float) -> None:
        """Set the playback volume (1.0 == original level).

        Applies immediately to the current track when it is playing through a
        ``PCMVolumeTransformer``; otherwise the change takes effect on the next
        track (and switches the source type via :meth:`_make_source`).
        """
        self.volume = volume
        source = self._current_source
        if isinstance(source, discord.PCMVolumeTransformer):
            source.volume = volume

    def set_loop(self, mode: LoopMode) -> None:
        """Set the loop mode (OFF / TRACK / QUEUE)."""
        self.loop_mode = mode

    def is_playing(self) -> bool:
        return self.voice is not None and self.voice.is_playing()

    def is_paused(self) -> bool:
        return self.voice is not None and self.voice.is_paused()

    async def stop(self) -> None:
        """Clear the queue and stop playback (stays connected).

        Turns off any loop mode and marks the current track interrupted so it
        is not re-enqueued.
        """
        self.loop_mode = LoopMode.OFF
        self._interrupted = True
        self._drain_queue()
        if self.voice is not None and (
            self.voice.is_playing() or self.voice.is_paused()
        ):
            self.voice.stop()

    def _drain_queue(self) -> None:
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - race guard
                break

    async def disconnect(self) -> None:
        """Tear down: clear queue, stop playback, cancel loop, disconnect."""
        self._closed = True
        self._drain_queue()
        if self.voice is not None:
            if self.voice.is_playing() or self.voice.is_paused():
                self.voice.stop()
            if self.voice.is_connected():
                await self.voice.disconnect()
        self.voice = None
        self.current = None
        if self._play_task is not None and not self._play_task.done():
            self._play_task.cancel()

    # ---- Playback loop --------------------------------------------------

    def _make_source(self, track: PlayableTrack) -> discord.AudioSource:
        """Build an audio source for a track honoring the current volume."""
        if self.volume == DEFAULT_VOLUME:
            return discord.FFmpegOpusAudio(track.file_path)
        pcm = discord.FFmpegPCMAudio(track.file_path)
        return discord.PCMVolumeTransformer(pcm, volume=self.volume)

    def _after_playback(self, error: Optional[Exception]) -> None:
        if error is not None:  # pragma: no cover - logged from voice thread
            logger.error("Playback error in guild %s: %s", self.guild_id, error)
        # Called from the voice thread; hop back to the loop thread safely.
        loop = getattr(self.voice, "loop", None)
        if loop is not None:
            loop.call_soon_threadsafe(self._track_done.set)
        else:  # pragma: no cover - fallback when loop not attached (tests)
            self._track_done.set()

    async def _run(self) -> None:
        """Background loop: play queued tracks until closed.

        Loop-mode handling after a track finishes on its own (i.e. not skipped
        or stopped):
          * TRACK: replay the same track.
          * QUEUE: append the track back to the end of the queue.
          * OFF:   move on to the next queued track.
        """
        try:
            while not self._closed:
                try:
                    track = await self._next_track()
                except asyncio.TimeoutError:
                    # Idle for too long with an empty queue: auto-disconnect.
                    await self._handle_idle()
                    break
                if self._closed:
                    break
                self.current = track

                # Replay in place while TRACK-looping and not interrupted.
                while not self._closed:
                    self._interrupted = False
                    await self._play_one(track)
                    if (
                        self._closed
                        or self._interrupted
                        or self.loop_mode is not LoopMode.TRACK
                    ):
                        break

                # Queue-loop: re-append the finished track (unless interrupted).
                if (
                    not self._closed
                    and not self._interrupted
                    and self.loop_mode is LoopMode.QUEUE
                ):
                    self._queue.put_nowait(track)

                self.current = None
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception:  # pragma: no cover - defensive
            logger.exception("Playback loop crashed for guild %s", self.guild_id)

    async def _next_track(self) -> PlayableTrack:
        """Await the next queued track, honoring the idle timeout.

        Raises ``asyncio.TimeoutError`` if the queue stays empty past
        ``idle_timeout`` (when a timeout is configured).
        """
        if self.idle_timeout is None:
            return await self._queue.get()
        return await asyncio.wait_for(self._queue.get(), timeout=self.idle_timeout)

    async def _handle_idle(self) -> None:
        """Fire the idle callback (if any) to trigger auto-disconnect."""
        logger.info("Guild %s idle; auto-disconnecting.", self.guild_id)
        if self._on_idle is not None:
            await self._on_idle(self.guild_id)

    async def _play_one(self, track: PlayableTrack) -> None:
        if self.voice is None or not self.voice.is_connected():
            return
        self._track_done.clear()
        source = self._make_source(track)
        self._current_source = source
        self.voice.play(source, after=self._after_playback)
        await self._track_done.wait()
        self._current_source = None


class PlayerRegistry:
    """Registry of guild id -> GuildPlayer.

    New players inherit the registry's idle timeout and idle callback so the
    bot can auto-disconnect them after a period with an empty queue.
    """

    def __init__(
        self,
        idle_timeout: Optional[float] = None,
        on_idle: Optional[IdleCallback] = None,
    ) -> None:
        self._players: dict[int, GuildPlayer] = {}
        self.idle_timeout = idle_timeout
        self._on_idle = on_idle

    def get(self, guild_id: int) -> Optional[GuildPlayer]:
        return self._players.get(guild_id)

    def get_or_create(self, guild_id: int) -> GuildPlayer:
        player = self._players.get(guild_id)
        if player is None:
            player = GuildPlayer(
                guild_id,
                idle_timeout=self.idle_timeout,
                on_idle=self._on_idle,
            )
            self._players[guild_id] = player
        return player

    async def remove(self, guild_id: int) -> None:
        player = self._players.pop(guild_id, None)
        if player is not None:
            await player.disconnect()
