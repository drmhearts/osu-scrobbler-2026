import asyncio
from dataclasses import replace
import json
import time

import httpx
import pytest

from osu_scrobbler.metadata import MetadataClient
from osu_scrobbler.musicbrainz import MusicBrainzClient, _rank
from osu_scrobbler.models import Beatmap, LookupResult, Track
from osu_scrobbler.lastfm_metadata import LastfmMetadataClient
from osu_scrobbler.releases import reconcile
from osu_scrobbler.config import Config
from osu_scrobbler.diagnostics import ResolutionLog


def track(source, title="Release", *, artist="Artist", album_artist="Artist", confidence=0.99, kind="Album", identifier="id"):
    return Track("Song", artist, title, album_artist, 180, confidence=confidence, verified=True, release_type=kind,
                 track_count=8, release_id=identifier if source == "MusicBrainz" else None,
                 identifiers={source: identifier}, details={"source": source, "membership_verified": True})


@pytest.mark.parametrize("kind", ["EP", "Compilation", "Single", "Soundtrack"])
def test_lastfm_consensus_keeps_known_release_classification(kind):
    bm = Beatmap("map", "Song", "Artist", duration=180)
    known = track("MusicBrainz", kind=kind, confidence=0.94)
    lastfm = track("Last.fm", kind="Unknown")
    winner, _ = reconcile(bm, [known, lastfm])
    assert winner.release_type == kind
    assert winner.details["sources"] == ["Last.fm", "MusicBrainz"]


def test_consensus_is_scored_before_selection_and_deduplicated_per_provider():
    bm = Beatmap("map", "Song", "Artist")
    mb = track("MusicBrainz", "Correct", confidence=0.94)
    lf = track("Last.fm", "Correct", confidence=0.94)
    dc = track("Discogs", "Different", confidence=0.99)
    for choices in [[dc, mb, lf], [lf, dc, mb], [mb, lf, dc]]:
        winner, _ = reconcile(bm, choices)
        assert winner.album == "Correct"
        assert winner.details["release_evidence"]["agreeing_providers"] == 1
    duplicate = replace(mb, release_id="other")
    winner, _ = reconcile(bm, [mb, duplicate, lf])
    assert winner.details["release_evidence"]["agreeing_providers"] == 1


def test_conflicting_artist_or_duration_does_not_form_consensus():
    bm = Beatmap("map", "Song", "Artist")
    good = track("MusicBrainz")
    wrong_artist = track("Last.fm", artist="Other Artist")
    wrong_duration = replace(track("Discogs"), duration=600)
    winner, _ = reconcile(bm, [good, wrong_artist, wrong_duration])
    assert winner.details["sources"] == ["MusicBrainz"]


def test_single_vs_later_album_deterministic_and_explicit_release_hint_wins():
    single = replace(track("MusicBrainz", "Song", kind="Single", identifier="single"), release_date="2000")
    album = replace(track("Last.fm", "Album"), release_date="2002")
    bm = Beatmap("map", "Song", "Artist")
    winner, _ = reconcile(bm, [single, album])
    assert winner.album == "Album"
    winner, _ = reconcile(replace(bm, release_id="single"), [album, single])
    assert winner.release_type == "Single"


def test_producer_vocalist_collaboration_uses_authoritative_credit():
    wanted = Beatmap("map", "Song", "Producer feat. Singer", duration=180)
    candidate, safe = _rank(wanted, {"title": "Song", "length": 180000,
                                   "artist-credit": [{"name": "Producer & Singer"}], "score": 100})
    assert safe and candidate.artist == "Producer & Singer"
    assert candidate.album_artist == ""  # No release credit was supplied.


class Provider:
    api_enabled = True
    authenticated = False
    def __init__(self, found=(), *, hang=False):
        self.found, self.hang, self.calls = list(found), hang, []
        self.cancelled = False
    async def candidates(self, beatmap, **kwargs):
        self.calls.append(kwargs)
        if self.hang:
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True
        return self.found, []
    async def aclose(self):
        pass


async def test_slow_provider_is_cancelled_after_fast_verified_match(tmp_path):
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"recordings": []})))
    mb = MusicBrainzClient(tmp_path / "cache.db", client=http)
    async def lookup(beatmap):
        return LookupResult(track("MusicBrainz"))
    mb.lookup = lookup
    slow = Provider(hang=True)
    lf = Provider()
    client = MetadataClient(musicbrainz=mb, discogs=slow, lastfm=lf)
    try:
        result = await asyncio.wait_for(client.lookup(Beatmap("map", "Song", "Artist")), 0.5)
        assert result.track.album == "Release" and slow.cancelled
        assert slow.calls and lf.calls
    finally:
        await client.aclose()
        await http.aclose()


async def test_fast_provider_chain_settles_before_early_cancellation(tmp_path):
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"recordings": []})))
    mb = MusicBrainzClient(tmp_path / "cache.db", client=http)
    async def lookup(beatmap):
        return LookupResult(track("MusicBrainz"))
    mb.lookup = lookup
    class Chained(Provider):
        async def candidates(self, beatmap, **kwargs):
            for _ in range(12):
                await asyncio.sleep(0)
            return [track("Discogs")], []
    client = MetadataClient(musicbrainz=mb, discogs=Chained(), lastfm=False)
    try:
        result = await client.lookup(Beatmap("map", "Song", "Artist"))
        assert result.track.details["sources"] == ["Discogs", "MusicBrainz"]
    finally:
        await client.aclose()
        await http.aclose()


async def test_fallback_waits_for_every_configured_provider_and_deeper_lastfm(tmp_path):
    entered, finish = asyncio.Event(), asyncio.Event()
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"recordings": []})))
    mb = MusicBrainzClient(tmp_path / "cache.db", client=http)
    class Lastfm(Provider):
        async def candidates(self, beatmap, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("deep"):
                entered.set()
                await finish.wait()
            return [], []
    lf, dc = Lastfm(), Provider()
    client = MetadataClient(musicbrainz=mb, discogs=dc, lastfm=lf)
    try:
        task = asyncio.create_task(client.lookup(Beatmap("map", "Song", "Artist")))
        await asyncio.wait_for(entered.wait(), 1)
        assert not task.done() and dc.calls
        finish.set()
        result = await task
        assert result.track is None and "exhausted" in result.reason
        assert lf.calls == [{}, {"deep": True}]
    finally:
        await client.aclose()
        await http.aclose()


async def test_verified_single_still_searches_lastfm_and_prefers_comparable_album(tmp_path):
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"recordings": []})))
    mb = MusicBrainzClient(tmp_path / "cache.db", client=http)
    async def lookup(beatmap):
        return LookupResult(track("MusicBrainz", "Song", kind="Single"))
    mb.lookup = lookup
    lf = Provider([track("Last.fm", "Later album", kind="Unknown")])
    client = MetadataClient(musicbrainz=mb, discogs_enabled=False, lastfm=lf)
    try:
        result = await client.lookup(Beatmap("map", "Song", "Artist"))
        assert result.track.album == "Later album" and not result.track.album_fallback
        assert lf.calls == [{}, {"deep": True}]
    finally:
        await client.aclose()
        await http.aclose()


async def test_timeout_and_source_failure_are_bounded_and_diagnostic(tmp_path):
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    mb = MusicBrainzClient(tmp_path / "cache.db", client=http)
    slow = Provider(hang=True)
    logs = []
    client = MetadataClient(musicbrainz=mb, discogs_enabled=False, lastfm=slow, debug_sink=logs.append)
    client.fast_budget = client.deep_budget = 0.02
    try:
        result = await asyncio.wait_for(client.lookup(Beatmap("map", "Song", "Artist")), 1)
        assert result.track is None and "incomplete" in result.reason
        assert logs[0]["fallback"] is True
        assert logs[0]["providers"]["Last.fm"] == "unavailable or timed out"
    finally:
        await client.aclose()
        await http.aclose()


async def test_normalized_cache_reuses_feat_variants_and_keeps_current_raw_metadata(tmp_path):
    calls = []
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200, json={"recordings": []})))
    mb = MusicBrainzClient(tmp_path / "cache.db", client=http)
    lf = Provider([track("Last.fm", artist="wotaku", album_artist="wotaku")])
    client = MetadataClient(musicbrainz=mb, discogs_enabled=False, lastfm=lf)
    try:
        first = Beatmap("map", "Song", "wotaku feat. 初音ミク", duration=180)
        await client.lookup(first)
        count = len(calls), len(lf.calls)
        result = await client.lookup(replace(first, artist="wotaku ft. 初音ミク"))
        assert (len(calls), len(lf.calls)) == count
        assert result.track.raw_artist == "wotaku ft. 初音ミク"
        assert result.track.artist == "wotaku"
    finally:
        await client.aclose()
        await http.aclose()


def test_opt_in_diagnostics_are_bounded_and_source_preferences_survive_restart(tmp_path):
    config = Config(metadata_debug=True, lastfm_metadata_enabled=False)
    config.save(tmp_path)
    assert Config.load(tmp_path).metadata_debug is True
    assert Config.load(tmp_path).lastfm_metadata_enabled is False
    assert "metadata_debug" not in json.loads((tmp_path / "config.json").read_text("utf-8"))
    log = ResolutionLog(tmp_path)
    log({"reason": "match", "selected": {"album_artist": "Various Artists"}})
    assert json.loads(log.path.read_text("utf-8"))["selected"]["album_artist"] == "Various Artists"
    log.path.write_text("x" * (256 * 1024 + 1), "utf-8")
    log({"reason": "rotated"})
    assert log.path.with_name("metadata-debug.1.jsonl").exists()
    assert log.path.stat().st_size < 1024
