import json
from pathlib import Path
import subprocess
import sys

import pytest

from osu_scrobbler.cli import execute, parser
from osu_scrobbler.config import Config, WatchLock
from osu_scrobbler.models import Submission, Track
from osu_scrobbler.storage import Storage


ROOT = Path(__file__).resolve().parents[1]


def test_runnable_fixture_dry_run_emits_two_qualifying_attempts_without_history(tmp_path):
    result = subprocess.run([sys.executable, "-m", "osu_scrobbler", "--data-dir", str(tmp_path),
                             "watch", "--dry-run", "--fixture", str(ROOT / "tests/fixtures/scrobble_demo.json")],
                            text=True, encoding="utf-8", capture_output=True, check=True)
    assert result.stdout.count('DRY RUN — would scrobble:') == 2
    assert 'Demo complete: 2 dry-run scrobbles; none sent.' in result.stdout
    assert "防人想歌" in result.stdout
    assert "Timer: Half song" in result.stdout
    assert '"phase"' not in result.stdout
    assert "recording_id" not in result.stdout
    assert len(result.stdout.splitlines()) <= 10
    with_store = Storage(tmp_path / "history.db")
    assert with_store.history() == []
    with_store.close()


def test_json_output_is_opt_in_and_retains_submission_metadata(tmp_path):
    result = subprocess.run([sys.executable, "-m", "osu_scrobbler", "--data-dir", str(tmp_path), "--json",
                             "watch", "--dry-run", "--fixture", str(ROOT / "tests/fixtures/scrobble_demo.json")],
                            text=True, encoding="utf-8", capture_output=True, check=True)
    events = [json.loads(line) for line in result.stdout.splitlines()]
    previews = [event["DRY RUN - would submit"] for event in events if "DRY RUN - would submit" in event]
    assert len(previews) == 2
    assert previews[0]["metadata"]["title"] == "防人想歌"
    assert previews[0]["metadata"]["album_fallback"] is True
    assert previews[0]["playback_start"] == 1700000002
    assert any(event.get("phase") == "paused" for event in events)


def test_fixture_requires_explicit_dryrun(tmp_path):
    result = subprocess.run([sys.executable, "-m", "osu_scrobbler", "--data-dir", str(tmp_path),
                             "watch", "--fixture", str(ROOT / "tests/fixtures/scrobble_demo.json")],
                            text=True, capture_output=True)
    assert result.returncode == 1
    assert "requires --dry-run" in result.stderr


async def test_offline_export_uses_history_and_recovers_stale_control_file(tmp_path):
    store = Storage(tmp_path / "history.db")
    store.enqueue(Submission("id", "attempt", Track("防人想歌", "Artist", "Album", "Artist"), 100, 200))
    store.mark("id", "accepted", submitted_at=300)
    store.close()
    # Deliberately stale endpoint. A stopped watcher cannot block history access.
    (tmp_path / "control.json").write_text(json.dumps({"port": 1, "token": "stale"}))
    target = tmp_path / "unicode.txt"
    args = parser().parse_args(["--data-dir", str(tmp_path), "export", str(target)])
    assert (await execute(args))["exported"] == 1
    assert (await execute(args))["exported"] == 0
    assert "防人想歌" in target.read_text("utf-8")
    assert (await execute(parser().parse_args(["--data-dir", str(tmp_path), "queue", "list"])))["history"][0]["status"] == "accepted"


@pytest.mark.parametrize("key,value", [("ipc_port", "abc"), ("ipc_port", True), ("threshold_mode", []),
                                      ("export_path", 123), ("musicbrainz_user_agent", None),
                                      ("tosu_url", "ws://example.com/websocket/v2"), ("auto_export", "false")])
def test_invalid_config_is_rejected_before_save(tmp_path, key, value):
    config = Config(**{key: value})
    with pytest.raises(ValueError):
        config.save(tmp_path)
    assert not (tmp_path / "config.json").exists()


def test_single_watcher_lock_is_released_by_owner(tmp_path):
    with WatchLock(tmp_path):
        with pytest.raises(RuntimeError):
            with WatchLock(tmp_path):
                pytest.fail("Second watcher acquired the owner's lock")
    with WatchLock(tmp_path):
        pass


@pytest.mark.parametrize('old_mode', ['custom', 'standard'])
def test_saved_threshold_settings_migrate_to_half_without_commands(tmp_path, old_mode):
    (tmp_path / 'config.json').write_text(json.dumps({'threshold_mode': old_mode}), encoding='utf-8')
    config = Config.load(tmp_path)
    assert config.threshold_mode == 'half'
    config.save(tmp_path)
    assert json.loads((tmp_path / 'config.json').read_text('utf-8'))['threshold_mode'] == 'custom'
    assert Config.load(tmp_path).threshold_mode == 'half'


def test_cached_runtime_default_profile_uses_its_original_data_directory(tmp_path, monkeypatch):
    import osu_scrobbler.config as config_module
    from osu_scrobbler import __version__
    profile = tmp_path / 'original-profile'
    prefix = profile / 'updates/versions' / (__version__ + '-fixture') / 'venv'
    prefix.mkdir(parents=True)
    (prefix.parent / 'ready.json').write_text(json.dumps({'version': __version__,
        'environment': f'versions/{__version__}-fixture/venv', 'digest': 'a' * 64}))
    monkeypatch.setattr(config_module.sys, 'prefix', str(prefix))
    assert config_module.default_data_dir() == profile
