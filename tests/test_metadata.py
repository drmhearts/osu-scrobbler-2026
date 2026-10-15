import asyncio
from dataclasses import replace
import json

import httpx
import pytest

from osu_scrobbler import musicbrainz
from osu_scrobbler.discogs import DiscogsClient
from osu_scrobbler.metadata import MetadataClient
from osu_scrobbler.models import Beatmap, Track
from osu_scrobbler.musicbrainz import MusicBrainzClient
from osu_scrobbler.config import Config


RECORDING = "ed6af3e6-0717-4ec4-87c3-e30754019f6a"
RELEASE = "48b14517-5acb-4ab5-807c-d9d61e537a4b"
OTHER = "9adc612c-5d0a-42e5-ae23-5a37d9a1cf0d"


def mb_release(identifier=RELEASE, kind="Single", title="Song", **kwargs):
    return {"id": identifier, "title": title, "date": "2020", "status": "Official",
            "artist-credit": [{"name": "Artist"}], "release-group": {"id": identifier, "primary-type": kind}, **kwargs}


def recording(releases=None):
    return {"id": RECORDING, "title": "Song", "length": 180000, "score": 100,
            "artist-credit": [{"name": "Artist"}], "releases": releases or []}


def discogs_release(kind="EP", title="Real EP", artist="Artist"):
    return {"id": 123, "title": title, "artists": [{"name": artist}], "year": 2020,
            "formats": [{"descriptions": [kind]}], "tracklist": [
                {"type_": "track", "title": "Intro", "position": "1", "duration": "1:00"},
                {"type_": "track", "title": "Song", "artists": [{"name": "Artist"}], "position": "2", "duration": "3:00"}]}


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    # Real transports are mocked, but preserve scheduling/cancellation behavior.
    sleep = asyncio.sleep
    async def fast_sleep(seconds):
        await sleep(0)
    monkeypatch.setattr(musicbrainz.asyncio, "sleep", fast_sleep)


def clients(tmp_path, mb_handler, dc_handler, *, token="test-token"):
    mb_http = httpx.AsyncClient(transport=httpx.MockTransport(mb_handler))
    dc_http = httpx.AsyncClient(transport=httpx.MockTransport(dc_handler))
    mb = MusicBrainzClient(tmp_path / "musicbrainz.db", client=mb_http)
    dc = DiscogsClient(token=token, client=dc_http, cache_get=mb._get, cache_put=mb._put)
    return MetadataClient(musicbrainz=mb, discogs=dc), mb_http, dc_http


def mb_handler(releases=None, *, browsed=None, failure=False, tracklist=None):
    def handle(request):
        if failure:
            return httpx.Response(503)
        path = request.url.path
        if "/release/" in path:
            rid = path.rsplit("/", 1)[1]
            data = next((r for r in (browsed or []) + (releases or []) if r["id"] == rid), {})
            if tracklist is not None:
                data = {**data, "media": tracklist}
        elif path.endswith("/release"):
            assert request.url.params["recording"] == RECORDING
            data = {"releases": browsed or [], "release-count": len(browsed or [])}
        elif path.endswith("/recording"):
            data = {"recordings": [recording(releases)]}
        else:
            data = recording(releases)
        return httpx.Response(200, json=data)
    return handle


def dc_handler(data=None, *, status=200):
    def handle(request):
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, json={"results": [{"id": 123, "title": "Artist - Real release", "format": ["EP"]}]} if "search" in request.url.path else (data or discogs_release()))
    return handle


@pytest.mark.parametrize("kind", ["EP", "Compilation", "Album", "Soundtrack", "Mixtape"])
async def test_discogs_multi_track_release_recovers_musicbrainz_single(tmp_path, kind):
    client, h1, h2 = clients(tmp_path, mb_handler([mb_release()]), dc_handler(discogs_release(kind)))
    try:
        result = await client.lookup(Beatmap("map", "Song", "Artist", duration=180))
        track = result.track
        assert track.album == "Real EP" and track.release_type == kind
        assert track.recording_id == RECORDING  # independent title/artist/duration agreement
        assert track.release_id is None  # never pass a Discogs ID as a MB release
        assert track.track_number == "2" and track.track_count == 2
        assert track.identifiers["discogs_release"] == "123"
        assert len(result.candidates) >= 2
    finally:
        await client.aclose()
        await h1.aclose()
        await h2.aclose()


async def test_browse_finds_ep_missing_from_embedded_recording_releases(tmp_path):
    ep = mb_release(OTHER, "EP", "Hidden EP", media=[{"track-count": 5}])
    client, h1, h2 = clients(tmp_path, mb_handler([mb_release()], browsed=[ep]), dc_handler(status=503), token="")
    try:
        result = await client.lookup(Beatmap("map", "Song", "Artist", duration=180))
        assert result.track.album == "Hidden EP"
        assert result.track.release_type == "EP"
        assert result.track.track_count == 5
    finally:
        await client.aclose()
        await h1.aclose()
        await h2.aclose()


async def test_tracklist_mismatch_rejects_direct_release_hint(tmp_path):
    client, h1, h2 = clients(tmp_path, mb_handler([], browsed=[], tracklist=[{"tracks": [{"recording": {"id": OTHER}}]}]), dc_handler(status=503), token="")
    try:
        result = await client.lookup(Beatmap("map", "Song", "Artist", release_id=RELEASE))
        assert result.track.album_fallback and result.track.release_type == "Unknown"
        assert "Rejected" in " ".join(result.track.details["lookup_warnings"])
    finally:
        await client.aclose()
        await h1.aclose()
        await h2.aclose()


async def test_one_source_failure_does_not_hide_other_confident_release(tmp_path):
    client, h1, h2 = clients(tmp_path, mb_handler(failure=True), dc_handler(discogs_release("Compilation", artist="Various")))
    try:
        result = await client.lookup(Beatmap("map", "Song", "Artist", duration=180))
        assert result.track.release_type == "Compilation"
        assert "incomplete" in result.track.details["lookup_warnings"][0]
        assert result.track.recording_id is None
    finally:
        await client.aclose()
        await h1.aclose()
        await h2.aclose()


async def test_sources_agree_and_preserve_primary_secondary_types(tmp_path):
    release = mb_release(kind="Album", title="Real EP", **{"release-group": {"id": RELEASE, "primary-type": "Album", "secondary-types": ["Compilation"]}})
    client, h1, h2 = clients(tmp_path, mb_handler([release]), dc_handler(discogs_release("Compilation")))
    try:
        result = await client.lookup(Beatmap("map", "Song", "Artist", duration=180))
        assert result.track.details["sources"] == ["Discogs", "MusicBrainz"]
        assert result.track.release_type == "Compilation"
        assert result.track.details["source_metadata"]["MusicBrainz"]["type"] == "Album"
        assert result.track.details["source_metadata"]["MusicBrainz"]["secondary_types"] == ["Compilation"]
        assert result.track.identifiers["discogs_release"] == "123"
    finally:
        await client.aclose()
        await h1.aclose()
        await h2.aclose()


async def test_recording_ambiguity_not_resolved_by_title_only_across_sources(tmp_path):
    data = recording()
    client, h1, h2 = clients(tmp_path, lambda _: httpx.Response(200, json={"recordings": [data, {**data, "id": OTHER}]}), dc_handler())
    try:
        result = await client.lookup(Beatmap("map", "Song", "Artist", duration=180))
        assert result.track is None
        assert "Ambiguous" in result.reason
    finally:
        await client.aclose()
        await h1.aclose()
        await h2.aclose()


async def test_lookup_cache_varies_with_album_hint_and_manual_correction_wins(tmp_path):
    calls = []
    handle = mb_handler([mb_release(kind="Album", title="Original")])
    def recording_requests(request):
        calls.append(request)
        return handle(request)
    client, h1, h2 = clients(tmp_path, recording_requests, dc_handler(), token="")
    try:
        bm = Beatmap("map", "Song", "Artist", duration=180)
        await client.lookup(bm)
        count = len(calls)
        await client.lookup(bm)
        assert len(calls) == count
        await client.lookup(replace(bm, album="Different"))
        assert len(calls) > count
        correction = Track("Manual", "Artist", "Chosen EP", "Artist", release_type="EP")
        client.set_correction(bm, correction)
        result = await client.lookup(bm)
        assert result.track.title == "Manual" and result.track.release_type == "EP"
        assert (await client.info(result.track)).album == "Chosen EP"
    finally:
        await client.aclose()
        await h1.aclose()
        await h2.aclose()


async def test_requests_coalesce_and_correction_wins_during_network_lookup(tmp_path):
    entered, proceed = asyncio.Event(), asyncio.Event()
    calls = []
    async def delayed(request):
        calls.append(request)
        entered.set()
        await proceed.wait()
        return httpx.Response(200, json={"recordings": []})
    client, h1, h2 = clients(tmp_path, delayed, dc_handler(), token="")
    try:
        bm = Beatmap("map", "Song", "Artist")
        a = asyncio.create_task(client.lookup(bm))
        b = asyncio.create_task(client.lookup(bm))
        await entered.wait()
        client.set_correction(bm, Track("Manual", "Artist", "Chosen", "Artist"))
        proceed.set()
        results = await asyncio.gather(a, b)
        assert len(calls) == 1
        assert all(r.track.title == "Manual" for r in results)
    finally:
        await client.aclose()
        await h1.aclose()
        await h2.aclose()


def test_source_preferences_preserve_old_launcher_config_shape(tmp_path):
    config = Config(discogs_enabled=False)
    config.save(tmp_path)
    assert "discogs_enabled" not in json.loads((tmp_path / "config.json").read_text("utf-8"))
    assert Config.load(tmp_path).discogs_enabled is False


def test_old_tracks_load_with_unknown_release_type():
    track = Track.from_dict({"title": "Song", "artist": "Artist", "album": "Song", "album_artist": "Artist", "album_fallback": True})
    assert track.release_type == "Unknown" and not track.identifiers


@pytest.mark.parametrize("hint", [{"recording_id": RECORDING}, {"isrc": "TEST12345678"}])
def test_exact_identifier_beats_stale_title_and_artist(hint):
    data = {**recording(), "isrcs": ["TEST12345678"]}
    track, safe = musicbrainz._rank(Beatmap("map", "Old title", "Old artist", **hint), data)
    assert safe and track.confidence == 1.0
    assert track.details["identifier_match"] is True


async def test_known_recording_identifier_cannot_be_overridden_by_unrelated_discogs_release(tmp_path):
    client, h1, h2 = clients(tmp_path, mb_handler([mb_release(kind="Album", title="Canonical album")]),
                            dc_handler({**discogs_release(), "tracklist": [{"title": "Old title", "type_": "track", "duration": "3:00"}]}))
    try:
        result = await client.lookup(Beatmap("map", "Old title", "Artist", duration=180, recording_id=RECORDING))
        assert result.track.album == "Canonical album" and result.track.recording_id == RECORDING
    finally:
        await client.aclose()
        await h1.aclose()
        await h2.aclose()
