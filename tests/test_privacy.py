import json
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

from osu_scrobbler.cli import execute, parser
from osu_scrobbler.config import WatchLock
from osu_scrobbler.ipc import CommandServer
from osu_scrobbler.lastfm import Credentials, LastFmClient
from osu_scrobbler.models import Beatmap, Submission, Track
from osu_scrobbler.musicbrainz import MusicBrainzClient
from osu_scrobbler.privacy import clear_cache
from osu_scrobbler.storage import Storage
from osu_scrobbler.tray import TrayApp


async def test_music_requests_do_not_include_game_paths_scores_or_player_identity(tmp_path):
    requests = []
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, json={"recordings": []})))
    mb = MusicBrainzClient(tmp_path / "musicbrainz.db", client=http)
    try:
        await mb.lookup(Beatmap("private-player-map", "Song", "Artist", duration=90,
                               audio_path="C:/Users/Private/audio.mp3"))
        assert len(requests) == 1
        assert set(requests[0].url.params) == {"query", "limit", "fmt"}
        assert "Song" in requests[0].url.params["query"]
        assert "Private" not in str(requests[0].url)
        assert "private-player-map" not in str(requests[0].url)
    finally:
        await mb.aclose()
        await http.aclose()
    sent = []
    def lastfm_handler(request):
        sent.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"scrobbles": {"@attr": {"accepted": "1", "ignored": "0"}, "scrobble": {"ignoredMessage": {"code": "0"}}}})
    http = httpx.AsyncClient(transport=httpx.MockTransport(lastfm_handler))
    lastfm = LastFmClient(Credentials("key", "secret", "session"), http)
    try:
        track = Track("Song", "Artist", "Album", "Artist", 180, details={"playerName": "Private", "score": 999, "path": "private-path"})
        await lastfm.submit(Submission("local-id", "local-attempt", track, 1700000000, 1700000030))
        assert set(sent[0]) == {"method", "artist", "track", "album", "albumArtist", "timestamp", "chosenByUser", "duration", "api_key", "sk", "api_sig", "format"}
        assert "Private" not in json.dumps(sent)
        assert "local-id" not in json.dumps(sent)
    finally:
        await http.aclose()


async def test_cache_is_bounded_expired_entries_removed_and_corrections_keep_only_chosen_fields(tmp_path):
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("No network expected")))
    path = tmp_path / "musicbrainz.db"
    mb = MusicBrainzClient(path, client=http)
    try:
        track = Track("Chosen song", "Artist", "Album", "Artist", details={"playerName": "Private", "recording": {"tags": ["large response"]}})
        mb.set_correction(Beatmap("map", "Song", "Artist"), track)
        value = json.loads(mb._db.execute("SELECT value FROM corrections").fetchone()[0])
        assert value["details"] == {"manual_correction": True}
        for number in range(260):
            mb._put(str(number), {"track": None})
        assert mb._db.execute("SELECT COUNT(*) FROM cache").fetchone()[0] == 256
        mb._db.execute("INSERT INTO cache VALUES ('expired','{}',0)")
        mb._db.execute("UPDATE corrections SET value=?", (json.dumps(track.to_dict()),))
        mb._db.execute("PRAGMA user_version=0")
        mb._db.commit()
    finally:
        await mb.aclose()
    mb = MusicBrainzClient(path, client=http)
    try:
        assert mb._db.execute("SELECT COUNT(*) FROM cache WHERE key='expired'").fetchone()[0] == 0
        correction = await mb.lookup(Beatmap("different difficulty", "Song", "Artist"))
        assert correction.track.title == "Chosen song"
        assert correction.track.details == {"manual_correction": True}
        mb._db.execute("UPDATE cache SET expires=0")
        mb._db.commit()
        assert mb._get("anything") is None
        assert mb._db.execute("SELECT COUNT(*) FROM cache").fetchone()[0] == 0
    finally:
        await mb.aclose()
        await http.aclose()


async def test_clear_cache_preserves_history_corrections_exports_and_unrelated_files(tmp_path):
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("No network expected")))
    mb = MusicBrainzClient(tmp_path / "musicbrainz.db", client=http)
    mb._put("lookup", {"track": None})
    mb.set_correction(Beatmap("map", "Song", "Artist"), Track("Song", "Artist", "Album", "Artist"))
    await mb.aclose()
    store = Storage(tmp_path / "history.db")
    store.enqueue(Submission("id", "attempt", Track("Song", "Artist", "Album", "Artist"), 1700000000, 1700000030))
    store.close()
    for name in ("activity.log", "activity.log.1", "activity.log.2", "unrelated.log", "scrobbles.txt"):
        (tmp_path / name).write_text("keep unless disposable", "utf-8")
    for filename in ("watch.lock", "tray.lock"):
        with WatchLock(tmp_path, filename):
            with pytest.raises(RuntimeError, match="Exit the tray"):
                clear_cache(tmp_path)
            assert (tmp_path / "activity.log").exists()
    response = await execute(parser().parse_args(["--data-dir", str(tmp_path), "privacy", "--clear-cache"]))
    assert "Cleared" in response["message"]
    mb = MusicBrainzClient(tmp_path / "musicbrainz.db", client=http)
    try:
        assert mb._db.execute("SELECT COUNT(*) FROM cache").fetchone()[0] == 0
        assert (await mb.lookup(Beatmap("map", "Song", "Artist"))).track.title == "Song"
        store = Storage(tmp_path / "history.db")
        assert store.pending()[0].id == "id"
        store.close()
        assert not list(tmp_path.glob("activity.log*"))
        assert (tmp_path / "unrelated.log").read_text() == "keep unless disposable"
        assert (tmp_path / "scrobbles.txt").exists()
    finally:
        await mb.aclose()
        await http.aclose()


async def test_uninstall_removes_only_own_startup_and_stops_only_own_tray(tmp_path, monkeypatch):
    import osu_scrobbler.tray as tray_module
    removed = []
    stopped = []
    monkeypatch.setattr(tray_module, "remove_startup", lambda directory: removed.append(directory))
    app = TrayApp(tmp_path, SimpleNamespace(stop=lambda: stopped.append(True)))
    server = CommandServer(tmp_path, 0, app.handle_command, control_name="tray-control.json")
    await server.start()
    try:
        response = await execute(parser().parse_args(["--data-dir", str(tmp_path), "tray", "--uninstall"]))
        assert "stopped" in response["message"]
        assert removed == [tmp_path] and stopped == [True]
    finally:
        await server.close()
