"""Tests for GuildPlayer queue/controls and PlayerRegistry (voice mocked)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import player as player_mod  # noqa: E402
from player import GuildPlayer, LoopMode, PlayableTrack, PlayerRegistry  # noqa: E402


def _track(vid: str) -> PlayableTrack:
    return PlayableTrack(video_id=vid, title=f"T{vid}", file_path=f"/tmp/{vid}.opus")


class FakeVoice:
    """Minimal stand-in for discord.VoiceClient driving the playback loop.

    ``play`` records the track and immediately schedules the ``after`` callback
    so the loop advances, letting us assert play order deterministically.
    """

    def __init__(self):
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None
        self.channel = object()
        self._connected = True
        self._playing = False
        self._paused = False
        self.played: list[str] = []
        self._after = None

    def is_connected(self):
        return self._connected

    def is_playing(self):
        return self._playing

    def is_paused(self):
        return self._paused

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def play(self, source, after=None):
        self._playing = True
        # source.stem carries the file path; record something identifiable.
        self.played.append(getattr(source, "_test_id", "?"))
        self._after = after
        # Finish "instantly" on the loop.
        self.loop.call_soon(self._finish)

    def _finish(self):
        self._playing = False
        if self._after is not None:
            cb, self._after = self._after, None
            cb(None)

    def stop(self):
        self._playing = False
        if self._after is not None:
            cb, self._after = self._after, None
            cb(None)

    async def disconnect(self):
        self._connected = False


def test_enqueue_and_snapshot():
    gp = GuildPlayer(guild_id=1)
    gp.enqueue(_track("a"))
    gp.enqueue(_track("b"))
    assert gp.queue_size() == 2
    ids = [t.video_id for t in gp.queue_snapshot()]
    assert ids == ["a", "b"]


def test_default_state():
    gp = GuildPlayer(guild_id=1)
    assert gp.loop_mode is LoopMode.OFF
    assert gp.volume == player_mod.DEFAULT_VOLUME
    assert gp.current is None
    assert not gp.is_playing()
    assert not gp.is_paused()


async def test_playback_loop_plays_in_order():
    gp = GuildPlayer(guild_id=1)
    fake = FakeVoice()
    gp.voice = fake

    # Tag each source with its track id so FakeVoice can record order.
    def make_source(track):
        src = mock.MagicMock()
        src._test_id = track.video_id
        return src

    with mock.patch.object(gp, "_make_source", side_effect=make_source):
        gp.enqueue(_track("a"))
        gp.enqueue(_track("b"))
        gp.enqueue(_track("c"))
        gp._ensure_play_task()
        # Let the loop drain the queue.
        for _ in range(50):
            await asyncio.sleep(0)
            if fake.played == ["a", "b", "c"]:
                break

    assert fake.played == ["a", "b", "c"]
    await gp.disconnect()


async def test_skip_returns_true_when_playing():
    gp = GuildPlayer(guild_id=1)
    fake = FakeVoice()
    fake._playing = True
    gp.voice = fake
    assert gp.skip() is True


def test_skip_returns_false_when_idle():
    gp = GuildPlayer(guild_id=1)
    fake = FakeVoice()
    fake._playing = False
    gp.voice = fake
    assert gp.skip() is False


async def test_stop_drains_queue():
    gp = GuildPlayer(guild_id=1)
    gp.enqueue(_track("a"))
    gp.enqueue(_track("b"))
    gp.voice = FakeVoice()
    await gp.stop()
    assert gp.queue_size() == 0


async def test_disconnect_tears_down():
    gp = GuildPlayer(guild_id=1)
    fake = FakeVoice()
    gp.voice = fake
    gp._ensure_play_task()
    await gp.disconnect()
    assert gp.voice is None
    assert not fake.is_connected()


async def test_registry_get_or_create_and_remove():
    reg = PlayerRegistry()
    p1 = reg.get_or_create(5)
    p2 = reg.get_or_create(5)
    assert p1 is p2
    assert reg.get(5) is p1
    await reg.remove(5)
    assert reg.get(5) is None


# ---- Task 7: pause / resume / volume / loop ---------------------------------


def test_pause_and_resume():
    gp = GuildPlayer(guild_id=1)
    fake = FakeVoice()
    fake._playing = True
    gp.voice = fake

    assert gp.pause() is True
    assert fake.is_paused() is True
    # Not "playing" anymore, so pause again is a no-op.
    fake._playing = False
    assert gp.pause() is False

    assert gp.resume() is True
    assert fake.is_paused() is False
    assert gp.resume() is False  # nothing paused now


def test_set_volume_updates_value_and_live_source():
    gp = GuildPlayer(guild_id=1)
    # No live source: just records the value.
    gp.set_volume(0.5)
    assert gp.volume == 0.5

    # Live PCMVolumeTransformer source gets updated in place.
    live = mock.MagicMock(spec=player_mod.discord.PCMVolumeTransformer)
    live.volume = 0.5
    gp._current_source = live
    gp.set_volume(0.25)
    assert gp.volume == 0.25
    assert live.volume == 0.25


def test_set_loop():
    gp = GuildPlayer(guild_id=1)
    gp.set_loop(LoopMode.QUEUE)
    assert gp.loop_mode is LoopMode.QUEUE
    gp.set_loop(LoopMode.OFF)
    assert gp.loop_mode is LoopMode.OFF


def test_skip_marks_interrupted():
    gp = GuildPlayer(guild_id=1)
    fake = FakeVoice()
    fake._playing = True
    gp.voice = fake
    gp._interrupted = False
    assert gp.skip() is True
    assert gp._interrupted is True


async def test_stop_disables_loop():
    gp = GuildPlayer(guild_id=1)
    gp.set_loop(LoopMode.TRACK)
    gp.voice = FakeVoice()
    await gp.stop()
    assert gp.loop_mode is LoopMode.OFF
    assert gp._interrupted is True


async def test_loop_track_replays_then_stops():
    gp = GuildPlayer(guild_id=1)
    fake = FakeVoice()
    gp.voice = fake
    gp.set_loop(LoopMode.TRACK)

    plays = 0

    def make_source(track):
        nonlocal plays
        plays += 1
        # After 3 replays, turn looping off so the loop can advance/idle.
        if plays >= 3:
            gp.loop_mode = LoopMode.OFF
        src = mock.MagicMock()
        src._test_id = track.video_id
        return src

    with mock.patch.object(gp, "_make_source", side_effect=make_source):
        gp.enqueue(_track("a"))
        gp._ensure_play_task()
        for _ in range(100):
            await asyncio.sleep(0)
            if plays >= 3 and gp.current is None:
                break

    # Same track played at least 3 times (looped in place).
    assert fake.played[:3] == ["a", "a", "a"]
    await gp.disconnect()


async def test_loop_queue_reappends_track():
    gp = GuildPlayer(guild_id=1)
    fake = FakeVoice()
    gp.voice = fake
    gp.set_loop(LoopMode.QUEUE)

    plays = 0

    def make_source(track):
        nonlocal plays
        plays += 1
        if plays >= 2:
            gp.loop_mode = LoopMode.OFF
        src = mock.MagicMock()
        src._test_id = track.video_id
        return src

    with mock.patch.object(gp, "_make_source", side_effect=make_source):
        gp.enqueue(_track("a"))
        gp._ensure_play_task()
        for _ in range(100):
            await asyncio.sleep(0)
            if plays >= 2:
                break

    # Track "a" was re-appended after finishing and played again.
    assert fake.played[:2] == ["a", "a"]
    await gp.disconnect()


# ---- Task 8: idle auto-disconnect -------------------------------------------


async def test_idle_timeout_fires_callback():
    fired: list[int] = []

    async def on_idle(guild_id):
        fired.append(guild_id)

    gp = GuildPlayer(guild_id=42, idle_timeout=0.01, on_idle=on_idle)
    gp.voice = FakeVoice()
    gp._ensure_play_task()

    # Queue stays empty; the loop should time out and fire on_idle.
    for _ in range(200):
        await asyncio.sleep(0)
        if fired:
            break
        await asyncio.sleep(0.005)

    assert fired == [42]
    await gp.disconnect()


async def test_no_idle_when_track_arrives():
    fired: list[int] = []

    async def on_idle(guild_id):
        fired.append(guild_id)

    gp = GuildPlayer(guild_id=7, idle_timeout=0.05, on_idle=on_idle)
    fake = FakeVoice()
    gp.voice = fake

    def make_source(track):
        src = mock.MagicMock()
        src._test_id = track.video_id
        return src

    with mock.patch.object(gp, "_make_source", side_effect=make_source):
        gp.enqueue(_track("a"))
        gp._ensure_play_task()
        for _ in range(50):
            await asyncio.sleep(0)
            if fake.played == ["a"]:
                break

    assert fake.played == ["a"]
    assert fired == []  # played a track before the idle window elapsed
    await gp.disconnect()


async def test_registry_forwards_idle_config():
    async def on_idle(guild_id):
        pass

    reg = PlayerRegistry(idle_timeout=1.5, on_idle=on_idle)
    p = reg.get_or_create(1)
    assert p.idle_timeout == 1.5
    assert p._on_idle is on_idle
