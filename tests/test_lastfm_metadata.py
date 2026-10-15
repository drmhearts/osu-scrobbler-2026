import asyncio
from dataclasses import replace
from html import escape
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from osu_scrobbler.credits import split_credit, primary
from osu_scrobbler.lastfm_metadata import LastfmMetadataClient, LastfmMetadataError, AlbumRef, featured_on, html_album, music_url, release_candidates
from osu_scrobbler.musicbrainz import MusicBrainzClient
from osu_scrobbler.metadata import MetadataClient
from osu_scrobbler.models import Beatmap, Track, Submission
from osu_scrobbler.lastfm import LastFmClient, Credentials
from osu_scrobbler.releases import reconcile


EXAMPLES = json.loads((Path(__file__).parent / "fixtures/lastfm_release_regressions.json").read_text("utf-8"))


@pytest.fixture(autouse=True)
def fast_spacing(monkeypatch):
    import osu_scrobbler.lastfm_metadata as module
    sleep = asyncio.sleep
    async def fast(seconds):
        await sleep(0)
    monkeypatch.setattr(module.asyncio, "sleep", fast)


def album(artist="Artist", title="Release", track="Song", *, track_artist=None, listeners=1000, duration=180):
    return {"name": title, "artist": artist, "url": music_url(artist, title), "listeners": str(listeners),
            "tracks": {"track": [
                {"name": "Intro", "artist": {"name": artist}, "duration": 60, "@attr": {"rank": "1"}},
                {"name": track, "artist": {"name": track_artist or artist}, "duration": duration, "@attr": {"rank": "2"}}]}}


def featured_page(artist, title, refs):
    items = "".join(f'<li class="resource-list--release-list-item"><h4><a href="{music_url(a, r)}">{escape(r)}</a></h4><p>{escape(a)}</p><p>{count:,} listeners</p></li>' for a, r, count in refs)
    return f'<html><head><link rel="canonical" href="{music_url(artist, "_", title)}"></head><body><h1 class="header-new-title">{escape(title)}</h1><section><h3>Featured On</h3><ul>{items}</ul></section><section><h3>Similar Albums</h3><a href="{music_url("Unrelated", "Popular unrelated release")}">Unrelated</a></section></body></html>'


@pytest.mark.parametrize("example", EXAMPLES, ids=["Undead-Corporation-The-Empress", "wotaku-Illuminati"])
async def test_required_release_regressions_use_lastfm_featured_on_and_verify_membership(tmp_path, example):
    bm = Beatmap("map", example["track"], example["artist"], duration=example["duration"])
    artist = example.get("canonical_artist", example["artist"])
    requested = []
    chosen = album(example["album_artist"], example["album"], example["track"], track_artist=artist,
                   listeners=example["listeners"], duration=example["duration"])
    less_popular = album(artist, "Less popular edition", example["track"], listeners=5, duration=example["duration"])
    def handler(request):
        requested.append(request)
        method = request.url.params.get("method")
        assert "sk" not in request.url.params and "api_sig" not in request.url.params
        if method:
            assert request.url.params["api_key"] == "test-api-key"
        if method == "track.getInfo":
            assert request.url.params["artist"] == artist
            return httpx.Response(200, json={"track": {"name": example["track"], "artist": {"name": artist}, "duration": example["duration"] * 1000}})
        if method == "album.getInfo":
            data = chosen if request.url.params["album"] == example["album"] else less_popular
            return httpx.Response(200, json={"album": data})
        return httpx.Response(200, text=featured_page(artist, example["track"], [(artist, "Less popular edition", 5), (artist, example["album"], example["listeners"])]))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        mb_http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"recordings": []})))
        mb = MusicBrainzClient(tmp_path / "cache.db", client=mb_http)
        lf = LastfmMetadataClient(api_key="test-api-key", client=http, cache_get=mb._get, cache_put=mb._put)
        client = MetadataClient(musicbrainz=mb, discogs_enabled=False, lastfm=lf)
        try:
            result = await client.lookup(bm)
            assert result.track.album == example["album"]
            assert result.track.artist == artist
            assert result.track.album_artist == example["album_artist"]
            assert not result.track.album_fallback
            assert result.track.raw_artist == example["artist"]
            assert result.track.primary_artist == artist
            assert result.track.identifiers["lastfm_release"] == chosen["url"]
            assert result.track.track_number == "2"
            assert result.track.details["provider_status"]["Last.fm featured on"] == "complete"
            count = len(requested)
            cached = await client.lookup(replace(bm, selection_key="other difficulty"))
            assert cached.track.album == example["album"] and len(requested) == count
        finally:
            await client.aclose()
            await mb_http.aclose()


@pytest.mark.parametrize("marker", ["feat.", "ft.", "featuring", "FEAT."])
def test_featured_credit_normalization_and_fallback(marker):
    raw = f"wotaku {marker} 初音ミク"
    credit = split_credit(raw)
    assert credit.raw == raw and credit.primary == "wotaku" and credit.featured == ("初音ミク",)
    fallback = Track.fallback(Beatmap("map", "イルミナティ", raw))
    assert fallback.artist == "wotaku" and fallback.featured_artists == ["初音ミク"]
    assert fallback.album_artist == ""


def test_collaboration_is_not_stripped_like_featured_suffix():
    assert primary("Producer & Vocalist") == "Producer & Vocalist"
    assert primary("Producer with Vocalist") == "Producer with Vocalist"


def test_authoritative_featured_track_credit_and_different_album_artist():
    bm = Beatmap("map", "Song", "Producer feat. Singer", duration=180)
    found = release_candidates(bm, album("Producer", track_artist="Producer feat. Singer"))
    assert found[0].artist == "Producer feat. Singer"
    assert found[0].album_artist == "Producer"


def test_various_artists_compilation_uses_track_credit_and_never_popularity_alone():
    bm = Beatmap("map", "Song", "Artist", duration=180)
    good = album("Various Artists", "Compilation", track_artist="Artist", listeners=1000000)
    found = release_candidates(bm, good)
    assert found[0].album_artist == "Various Artists" and found[0].artist == "Artist"
    wrong = album("Various Artists", "Unrelated", track_artist="Other", listeners=99999999)
    assert release_candidates(bm, wrong) == []
    good["tracks"]["track"][1].pop("artist")
    assert release_candidates(bm, good) == []


def test_artist_relationship_precedes_lastfm_popularity():
    bm = Beatmap("map", "Song", "Artist", duration=180)
    own = release_candidates(bm, album("Artist", "Own release", listeners=100))
    compilation = release_candidates(bm, album("Various Artists", "Popular collection", track_artist="Artist", listeners=99999999))
    winner, _ = reconcile(bm, compilation + own)
    assert winner.album == "Own release"


def test_featured_parser_ignores_related_sidebar_and_mismatching_track_page():
    bm = Beatmap("map", "Song", "Artist")
    html = featured_page("Artist", "Song", [("Artist", "Release", 1000), ("Artist", "Other", 5000)])
    refs = featured_on(html, bm)
    assert [ref.title for ref in refs] == ["Other", "Release"]
    assert all(ref.artist != "Unrelated" for ref in refs)
    assert featured_on(html, replace(bm, title="Different")) == []
    assert featured_on(html.replace('<h1 class="header-new-title">Song', '<h1 class="header-new-title">Wrong'), bm) == []
    assert featured_on(html.replace("<h4>", "<h3>").replace("</h4>", "</h3>"), bm) == refs


@pytest.mark.parametrize("matching_album", [True, False])
async def test_fast_api_release_requires_tracklist_and_requested_album_identity(matching_album):
    cache = {}
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.params["method"] == "track.getInfo":
            return httpx.Response(200, json={"track": {"name": "Song", "artist": {"name": "Artist"},
                "duration": "180000", "album": {"title": "Release", "artist": "Artist"}}})
        return httpx.Response(200, json={"album": album(title="Release" if matching_album else "Wrong release")})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        lf = LastfmMetadataClient(api_key="private-api-key", client=http, cache_get=cache.get,
                                  cache_put=lambda key, value, seconds: cache.update({key: value}))
        bm = Beatmap("map", "Song", "Artist feat. Guest", duration=180)
        found, notes = await lf.candidates(bm)
        assert not notes
        assert bool(found) == matching_album
        if found:
            assert found[0].artist == "Artist" and found[0].duration == 180
            assert found[0].release_type == "Unknown"
        count = len(calls)
        await lf.candidates(bm)
        assert len(calls) == count
        assert all("private-api-key" not in key for key in cache)


def test_public_jsonld_tracklist_requires_official_track_artist_and_valid_shape():
    ref = AlbumRef("Artist", "Release", music_url("Artist", "Release"))
    def page(track):
        data = {"@type": "MusicAlbum", "name": "Release", "byArtist": {"name": "Artist"}, "track": track}
        return f'<link rel="canonical" href="{ref.url}"><script type="application/ld+json">{json.dumps(data)}</script>'
    result = html_album(page([{"name": "Song", "byArtist": {"name": "Artist"}}]), ref)
    assert release_candidates(Beatmap("map", "Song", "Artist"), result)[0].album == "Release"
    for malformed in [123, {"itemListElement": 123}, [{"item": 123}], [{"name": "Song"}]]:
        result = html_album(page(malformed), ref)
        assert not release_candidates(Beatmap("map", "Song", "Artist"), result)


async def test_no_api_key_uses_verified_public_tracklist_without_credentials():
    bm = Beatmap("map", "Song", "Artist", duration=180)
    release_url = music_url("Artist", "Public release")
    page = f'<link rel="canonical" href="{release_url}"><table><tr class="chartlist-row" data-track-name="Song" data-artist-name="Artist"><td class="chartlist-index">2</td><td class="chartlist-name">Song</td><td class="chartlist-duration">3:00</td></tr></table>'
    def handler(request):
        assert "api_key" not in request.url.params and "Authorization" not in request.headers
        return httpx.Response(200, text=page if str(request.url) == release_url else featured_page("Artist", "Song", [("Artist", "Public release", 1000)]))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        lf = LastfmMetadataClient(api_key="", client=http)
        result, warnings = await lf.candidates(bm, deep=True)
        assert not warnings and result[0].album == "Public release"
        assert result[0].details["discovery"] == "featured-on+public-tracklist"


@pytest.mark.parametrize("status", [403, 406, 429])
async def test_public_page_failures_are_explicit_and_do_not_become_cached_empty_success(status):
    saved, calls = [], []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(status))) as http:
        lf = LastfmMetadataClient(api_key="", client=http, cache_put=lambda *args: saved.append(args))
        for _ in range(2):
            found, warnings = await lf.candidates(Beatmap("map", "Song", "Artist"), deep=True)
            assert not found and warnings
        assert not saved and len(calls) == 1


async def test_api_errors_do_not_expose_key_and_do_not_require_scrobble_authentication():
    cache = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"error": 10, "message": "secret-key-in-upstream-body"}))) as http:
        lf = LastfmMetadataClient(api_key="secret-key-in-upstream-body", client=http, cache_put=lambda *args: cache.append(args))
        with pytest.raises(LastfmMetadataError) as error:
            await lf._request(method="track.getInfo", params={"artist": "Artist", "track": "Song"})
        assert "secret-key" not in str(error.value) and not cache


async def test_pending_scrobble_omits_unresolved_album_without_delaying_delivery():
    sent = []
    def handler(request):
        sent.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"scrobbles": {"@attr": {"accepted": "1", "ignored": "0"}, "scrobble": {"ignoredMessage": {"code": "0"}}}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = LastFmClient(Credentials("key", "secret", "session"), http)
        pending = Track.pending(Beatmap("map", "Song", "Artist feat. Vocalist", duration=180))
        result = await client.submit(Submission("id", "attempt", pending, 1700000000, 1700000100))
        assert result.status == "accepted"
        assert "album" not in sent[0] and "albumArtist" not in sent[0]
        assert sent[0]["artist"] == ["Artist"]
