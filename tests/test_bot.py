"""Tests for bot startup checks and slash-command handlers.

Discord objects are faked; the CacheManager and GuildPlayer are mocked so the
command callbacks can be driven without any network or voice connection.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import bot as bot_mod  # noqa: E402
from bot import MissingFFmpegError, ensure_ffmpeg  # noqa: E402
from cache import TrackRecord  # noqa: E402


# --------------------------------------------------------------------------
# ensure_ffmpeg
# --------------------------------------------------------------------------

def test_ensure_ffmpeg_found():
    with mock.patch("bot.shutil.which", return_value="/usr/bin/ffmpeg"):
        assert ensure_ffmpeg() == "/usr/bin/ffmpeg"


def test_ensure_ffmpeg_missing_raises():
    with mock.patch("bot.shutil.which", return_value=None):
        with pytest.raises(MissingFFmpegError):
            ensure_ffmpeg()


# --------------------------------------------------------------------------
# Fakes for interactions / players
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self):
        self.messages: list[tuple[str, bool]] = []
        self.deferred = False

    async def send_message(self, content, ephemeral=False):
        self.messages.append((content, ephemeral))

    async def defer(self, ephemeral=False, thinking=False):
        self.deferred = True


class FakeFollowup:
    def __init__(self):
        self.messages: list[tuple[str, bool]] = []

    async def send(self, content, ephemeral=False):
        self.messages.append((content, ephemeral))


class FakeVoiceState:
    def __init__(self, channel):
        self.channel = channel


class FakeUser:
    def __init__(self, name="tester", channel=None):
        self._name = name
        self.voice = FakeVoiceState(channel) if channel is not None else None

    def __str__(self):
        return self._name


class FakeGuild:
    def __init__(self, gid=1):
        self.id = gid


class FakeInteraction:
    def __init__(self, guild=None, user=None):
        self.guild = guild
        self.user = user
        self.response = FakeResponse()
        self.followup = FakeFollowup()


def _make_bot():
    """Construct a MusicBot without invoking discord.Client networking."""
    cfg = mock.MagicMock()
    cfg.cache_dir = Path("/tmp/cache-test")
    cfg.cache_max_bytes = 4 * 1024**3
    with mock.patch("bot.discord.Client.__init__", return_value=None), \
         mock.patch("bot.CacheManager") as m_cache, \
         mock.patch("bot.app_commands.CommandTree") as m_tree:
        m_tree.return_value = _RealTreeStub()
        b = bot_mod.MusicBot(cfg)
    b.cache = m_cache.return_value
    return b


class _RealTreeStub:
    """A minimal command tree that records callbacks by name."""

    def __init__(self):
        self.commands: dict = {}

    def command(self, name, description=None):
        def deco(fn):
            self.commands[name] = fn
            return fn
        return deco


def _cmd(bot, name):
    return bot.tree.commands[name]


# --------------------------------------------------------------------------
# /play
# --------------------------------------------------------------------------

async def test_play_requires_guild():
    bot = _make_bot()
    inter = FakeInteraction(guild=None, user=FakeUser())
    await _cmd(bot, "play")(inter, "some song")
    assert inter.response.messages
    assert "server" in inter.response.messages[0][0].lower()


async def test_play_requires_voice_channel():
    bot = _make_bot()
    inter = FakeInteraction(guild=FakeGuild(), user=FakeUser(channel=None))
    await _cmd(bot, "play")(inter, "some song")
    assert "voice channel" in inter.response.messages[0][0].lower()


async def test_play_downloads_connects_and_enqueues():
    bot = _make_bot()
    channel = object()
    inter = FakeInteraction(guild=FakeGuild(7), user=FakeUser(channel=channel))

    record = TrackRecord("vid", "Cool Song", "/tmp/vid.opus", 100, 1.0)

    async def fake_get(query):
        return record

    bot.cache.get_or_download = fake_get

    fake_player = mock.MagicMock()
    fake_player.current = None
    fake_player.queue_size.return_value = 1

    async def fake_connect(ch):
        fake_player.connected_to = ch

    fake_player.connect = fake_connect
    bot.players.get_or_create = mock.MagicMock(return_value=fake_player)

    await _cmd(bot, "play")(inter, "cool song")

    assert inter.response.deferred
    fake_player.enqueue.assert_called_once()
    enqueued = fake_player.enqueue.call_args[0][0]
    assert enqueued.video_id == "vid"
    assert enqueued.title == "Cool Song"
    assert fake_player.connected_to is channel
    assert inter.followup.messages
    assert "Now playing" in inter.followup.messages[-1][0]


async def test_play_reports_download_error():
    bot = _make_bot()
    inter = FakeInteraction(guild=FakeGuild(), user=FakeUser(channel=object()))

    async def boom(query):
        raise RuntimeError("nope")

    bot.cache.get_or_download = boom
    await _cmd(bot, "play")(inter, "bad")
    assert inter.response.deferred
    assert "couldn't fetch" in inter.followup.messages[-1][0].lower()


# --------------------------------------------------------------------------
# /skip /stop /queue /nowplaying /leave
# --------------------------------------------------------------------------

async def test_skip_nothing_playing():
    bot = _make_bot()
    inter = FakeInteraction(guild=FakeGuild(), user=FakeUser())
    bot.players.get = mock.MagicMock(return_value=None)
    await _cmd(bot, "skip")(inter)
    assert "nothing is playing" in inter.response.messages[0][0].lower()


async def test_skip_success():
    bot = _make_bot()
    inter = FakeInteraction(guild=FakeGuild(), user=FakeUser())
    player = mock.MagicMock()
    player.skip.return_value = True
    bot.players.get = mock.MagicMock(return_value=player)
    await _cmd(bot, "skip")(inter)
    assert "skipped" in inter.response.messages[0][0].lower()


async def test_stop_clears_queue():
    bot = _make_bot()
    inter = FakeInteraction(guild=FakeGuild(), user=FakeUser())
    player = mock.MagicMock()

    async def fake_stop():
        player.stopped = True

    player.stop = fake_stop
    bot.players.get = mock.MagicMock(return_value=player)
    await _cmd(bot, "stop")(inter)
    assert player.stopped is True
    assert "cleared" in inter.response.messages[0][0].lower()


async def test_queue_lists_current_and_pending():
    bot = _make_bot()
    inter = FakeInteraction(guild=FakeGuild(), user=FakeUser())
    player = mock.MagicMock()
    player.current = mock.MagicMock(title="Playing Now")
    player.queue_snapshot.return_value = [
        mock.MagicMock(title="Track A"),
        mock.MagicMock(title="Track B"),
    ]
    bot.players.get = mock.MagicMock(return_value=player)
    await _cmd(bot, "queue")(inter)
    text = inter.response.messages[0][0]
    assert "Playing Now" in text
    assert "Track A" in text and "Track B" in text


async def test_nowplaying_reports_current():
    bot = _make_bot()
    inter = FakeInteraction(guild=FakeGuild(), user=FakeUser())
    player = mock.MagicMock()
    player.current = mock.MagicMock(title="Current Jam")
    bot.players.get = mock.MagicMock(return_value=player)
    await _cmd(bot, "nowplaying")(inter)
    assert "Current Jam" in inter.response.messages[0][0]


async def test_leave_disconnects():
    bot = _make_bot()
    inter = FakeInteraction(guild=FakeGuild(3), user=FakeUser())
    player = mock.MagicMock()
    player.voice = object()
    bot.players.get = mock.MagicMock(return_value=player)

    async def fake_remove(gid):
        bot.removed = gid

    bot.players.remove = fake_remove
    await _cmd(bot, "leave")(inter)
    assert bot.removed == 3
    assert "left" in inter.response.messages[0][0].lower()


async def test_leave_when_not_connected():
    bot = _make_bot()
    inter = FakeInteraction(guild=FakeGuild(), user=FakeUser())
    bot.players.get = mock.MagicMock(return_value=None)
    await _cmd(bot, "leave")(inter)
    assert "not in a voice channel" in inter.response.messages[0][0].lower()


# --------------------------------------------------------------------------
# Task 7: /pause /resume /volume /loop
# --------------------------------------------------------------------------

async def test_pause_success():
    bot = _make_bot()
    inter = FakeInteraction(guild=FakeGuild(), user=FakeUser())
    player = mock.MagicMock()
    player.pause.return_value = True
    bot.players.get = mock.MagicMock(return_value=player)
    await _cmd(bot, "pause")(inter)
    assert "paused" in inter.response.messages[0][0].lower()


async def test_pause_nothing_playing():
    bot = _make_bot()
    inter = FakeInteraction(guild=FakeGuild(), user=FakeUser())
    player = mock.MagicMock()
    player.pause.return_value = False
    bot.players.get = mock.MagicMock(return_value=player)
    await _cmd(bot, "pause")(inter)
    assert "nothing is playing" in inter.response.messages[0][0].lower()


async def test_resume_success():
    bot = _make_bot()
    inter = FakeInteraction(guild=FakeGuild(), user=FakeUser())
    player = mock.MagicMock()
    player.resume.return_value = True
    bot.players.get = mock.MagicMock(return_value=player)
    await _cmd(bot, "resume")(inter)
    assert "resumed" in inter.response.messages[0][0].lower()


async def test_volume_out_of_range():
    bot = _make_bot()
    inter = FakeInteraction(guild=FakeGuild(), user=FakeUser())
    await _cmd(bot, "volume")(inter, 150)
    assert "between 0 and 100" in inter.response.messages[0][0].lower()


async def test_volume_sets_value():
    bot = _make_bot()
    inter = FakeInteraction(guild=FakeGuild(), user=FakeUser())
    player = mock.MagicMock()
    bot.players.get = mock.MagicMock(return_value=player)
    await _cmd(bot, "volume")(inter, 50)
    player.set_volume.assert_called_once_with(0.5)
    assert "50%" in inter.response.messages[0][0]


async def test_volume_nothing_playing():
    bot = _make_bot()
    inter = FakeInteraction(guild=FakeGuild(), user=FakeUser())
    bot.players.get = mock.MagicMock(return_value=None)
    await _cmd(bot, "volume")(inter, 50)
    assert "nothing is playing" in inter.response.messages[0][0].lower()


async def test_loop_sets_mode():
    from player import LoopMode

    bot = _make_bot()
    inter = FakeInteraction(guild=FakeGuild(), user=FakeUser())
    player = mock.MagicMock()
    bot.players.get = mock.MagicMock(return_value=player)

    choice = mock.MagicMock()
    choice.value = "queue"
    await _cmd(bot, "loop")(inter, choice)
    player.set_loop.assert_called_once_with(LoopMode.QUEUE)
    assert "queue" in inter.response.messages[0][0].lower()


# --------------------------------------------------------------------------
# Task 8: idle + empty-channel auto-leave
# --------------------------------------------------------------------------

class FakeMember:
    def __init__(self, mid, is_bot=False):
        self.id = mid
        self.bot = is_bot


class FakeChannel:
    def __init__(self, members):
        self.members = members


def test_has_human_listeners_true():
    me = FakeMember(100)
    channel = FakeChannel([me, FakeMember(1), FakeMember(2, is_bot=True)])
    assert bot_mod._has_human_listeners(channel, me) is True


def test_has_human_listeners_false_only_bots_and_self():
    me = FakeMember(100)
    channel = FakeChannel([me, FakeMember(2, is_bot=True)])
    assert bot_mod._has_human_listeners(channel, me) is False


def test_has_human_listeners_false_empty():
    me = FakeMember(100)
    assert bot_mod._has_human_listeners(FakeChannel([]), me) is False


async def test_on_player_idle_removes_player():
    bot = _make_bot()
    removed = []

    async def fake_remove(gid):
        removed.append(gid)

    bot.players.remove = fake_remove
    await bot._on_player_idle(99)
    assert removed == [99]


class FakeGuildWithId:
    def __init__(self, gid):
        self.id = gid


class FakeMemberWithGuild(FakeMember):
    def __init__(self, mid, guild, is_bot=False):
        super().__init__(mid, is_bot)
        self.guild = guild


def _patch_user(bot, member):
    """Patch the read-only MusicBot.user property to return ``member``."""
    return mock.patch.object(
        type(bot), "user", new_callable=mock.PropertyMock, return_value=member
    )


async def test_voice_state_update_leaves_when_empty():
    bot = _make_bot()
    guild = FakeGuildWithId(5)
    # Only the bot remains in the channel.
    channel = FakeChannel([FakeMember(100)])
    player = mock.MagicMock()
    player.voice.channel = channel
    bot.players.get = mock.MagicMock(return_value=player)

    removed = []

    async def fake_remove(gid):
        removed.append(gid)

    bot.players.remove = fake_remove

    member = FakeMemberWithGuild(1, guild)  # a human who just left
    with _patch_user(bot, FakeMember(100)):
        await bot.on_voice_state_update(member, mock.MagicMock(), mock.MagicMock())
    assert removed == [5]


async def test_voice_state_update_stays_when_humans_present():
    bot = _make_bot()
    guild = FakeGuildWithId(5)
    channel = FakeChannel([FakeMember(100), FakeMember(1)])  # a human remains
    player = mock.MagicMock()
    player.voice.channel = channel
    bot.players.get = mock.MagicMock(return_value=player)

    removed = []

    async def fake_remove(gid):
        removed.append(gid)

    bot.players.remove = fake_remove

    member = FakeMemberWithGuild(2, guild)
    with _patch_user(bot, FakeMember(100)):
        await bot.on_voice_state_update(member, mock.MagicMock(), mock.MagicMock())
    assert removed == []


async def test_voice_state_update_ignores_self():
    bot = _make_bot()
    guild = FakeGuildWithId(5)
    bot.players.get = mock.MagicMock()

    member = FakeMemberWithGuild(100, guild)  # the bot itself
    with _patch_user(bot, FakeMember(100)):
        await bot.on_voice_state_update(member, mock.MagicMock(), mock.MagicMock())
    # Should return early without even looking up a player.
    bot.players.get.assert_not_called()
