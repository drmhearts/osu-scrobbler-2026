import json

import pytest

from osu_scrobbler.config import Config
from osu_scrobbler.models import Beatmap, Phase, Telemetry, Track
from osu_scrobbler.playback import PlaybackEngine
from osu_scrobbler.terminal import threshold_prompt, color
from osu_scrobbler.threshold import Threshold, parse_threshold
from osu_scrobbler.watcher import Watcher


@pytest.mark.parametrize('mode,seconds,required', [('half', 30, 60), ('full', 30, 120), ('seconds', 10, 10), ('seconds', 500, 120)])
def test_threshold_exact_boundary_and_custom_ceiling(mode, seconds, required):
    engine = PlaybackEngine(mode, seconds)
    song = Beatmap('map', 'Song', 'Artist', duration=120)
    engine.update(Telemetry(song, Phase.PLAYING, 0), 0, 1700000000)
    engine.bind_track(Track.fallback(song))
    assert engine.next_submission(required - .01, 1700000000 + required - .01) is None
    assert engine.next_submission(required, 1700000000 + required) is not None
    assert engine.next_submission(required + 1, 1700000000 + required + 1) is None


@pytest.mark.parametrize('value', ['nan', 'inf', '-1', '0', 'hello'])
def test_invalid_threshold_is_rejected(value):
    with pytest.raises(ValueError):
        parse_threshold(value)


async def test_timer_prompt_retries_and_saves_while_osu_is_closed(tmp_path):
    answers = iter(['nan', '30'])
    messages = []
    async def missing(*args):
        raise RuntimeError('watcher closed')
    reply = await threshold_prompt(tmp_path, sender=missing, reader=lambda _: next(answers), output=messages.append)
    assert reply == {'threshold': '30s'}
    assert Threshold.load(tmp_path) == Threshold('seconds', 30)
    assert len(messages) == 1
    assert await threshold_prompt(tmp_path, reader=lambda _: '') is None
    assert Threshold.load(tmp_path).seconds == 30


async def test_preferences_persist_without_changing_old_launcher_config(tmp_path):
    config = Config()
    config.save(tmp_path)
    before = (tmp_path / 'config.json').read_bytes()
    watcher = Watcher(tmp_path, config, fixture=True, output=lambda _: None)
    try:
        await watcher.command({'command': 'threshold', 'mode': 'seconds', 'seconds': 12})
        assert watcher.engine.mode == 'seconds' and watcher.engine.seconds == 12
        await watcher.command({'command': 'config', 'action': 'set', 'key': 'auto_export', 'value': True})
        assert watcher.engine.mode == 'seconds'
        assert Threshold.load(tmp_path).seconds == 12
        assert json.loads((tmp_path / 'config.json').read_text())['threshold_mode'] == 'custom'
        assert before != (tmp_path / 'config.json').read_bytes()  # Only the requested export setting changed.
    finally:
        await watcher.musicbrainz.aclose()
        watcher.storage.close()


def test_colors_are_optional_and_disabled_without_a_terminal(monkeypatch):
    import osu_scrobbler.terminal as terminal
    monkeypatch.setattr(terminal.sys, 'stdout', None)
    assert color('Track') == 'Track'
    assert color('Track', enabled=True).endswith('\033[0m')
