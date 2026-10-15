import asyncio
from dataclasses import replace
import json

import httpx
import pytest

from osu_scrobbler.discogs import DiscogsClient, DiscogsError, DiscogsTokenStore, linked_ids, release_tracks, seconds
from osu_scrobbler.models import Beatmap


def release(kind="EP", *, artist="Artist", title="Song", track_artist=None):
    return {"id": 123, "title": "Real release", "artists": [{"name": artist}], "released": "2020-03-04", "master_id": 456,
            "formats": [{"name": "CD", "descriptions": [kind]}],
            "tracklist": [{"type_": "heading", "title": "Side one"},
                          {"type_": "track", "title": "Other", "position": "1", "duration": "1:00"},
                          {"type_": "track", "title": title, "position": "2", "duration": "3:00",
                           "artists": [{"name": track_artist}] if track_artist else []}]}


@pytest.mark.parametrize("kind", ["Album", "EP", "Compilation", "Single"])
def test_actual_track_membership_and_types(kind):
    found = release_tracks(Beatmap("private", "Song", "Artist", duration=180), release(kind))
    assert len(found) == 1
    track = found[0]
    assert track.release_type == kind
    assert track.track_count == 2 and track.track_number == "2"
    assert track.release_date == "2020-03-04"
    assert track.identifiers == {"discogs_release": "123", "discogs_master": "456"}
    assert track.recording_id is None and track.release_id is None


def test_compilation_requires_track_artist_and_respects_version():
    beatmap = Beatmap("map", "Song", "Artist", duration=180)
    assert release_tracks(beatmap, release("Compilation", artist="Various")) == []
    good = release("Compilation", artist="Various", track_artist="Artist (2)")
    assert release_tracks(beatmap, good)[0].album_artist == "Various"
    assert release_tracks(replace(beatmap, artist="Wrong Artist"), good) == []
    assert release_tracks(beatmap, release(title="Song (Alice Remix)")) == []


def test_subtracks_and_unknown_format_are_not_single_inferences():
    data = release("LP")
    item = data["tracklist"].pop()
    data["tracklist"].append({"title": "Index", "type_": "index", "sub_tracks": [item]})
    found = release_tracks(Beatmap("map", "Song", "Artist"), data)
    assert found[0].release_type == "Unknown"
    assert found[0].track_count == 2


@pytest.mark.parametrize("value,expected", [("3:00", 180), ("1:02:03", 3723), ("", None), ("NaN", None), ("3:70", None), ("0:00", None)])
def test_discogs_duration(value, expected):
    assert seconds(value) == expected


async def test_search_verifies_tracklist_and_keeps_token_out_of_urls_and_cache():
    calls, cache = [], {}
    token = "private-token-test"
    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Discogs token=" + token
        assert token not in str(request.url)
        assert "private-player" not in str(request.url)
        return httpx.Response(200, json={"results": [{"id": 123, "title": "Artist - Real release"}]} if "search" in request.url.path else release())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        dc = DiscogsClient(token=token, client=http, cache_get=cache.get, cache_put=lambda key, value, seconds: cache.update({key: value}))
        bm = Beatmap("private-player", "Song", "Artist", duration=180, audio_path="C:/private/audio.mp3")
        found, notes = await dc.candidates(bm)
        assert found[0].release_type == "EP" and not notes
        assert set(calls[0].url.params) == {"type", "track", "artist", "per_page", "page"}
        assert token not in json.dumps(cache)
        await dc.candidates(bm)
        assert len(calls) == 4  # General, EP, compilation searches + one deduplicated release.


async def test_linked_release_lookup_without_token_uses_only_trusted_ids():
    calls = []
    def handler(request):
        calls.append(request)
        assert "Authorization" not in request.headers
        return httpx.Response(200, json=release())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        dc = DiscogsClient(token="", client=http)
        found, notes = await dc.candidates(Beatmap("map", "Song", "Artist"), search=False)
        assert not found and not notes and not calls
        found, notes = await dc.candidates(Beatmap("map", "Song", "Artist"), search=False,
                                         links=["https://www.discogs.com/release/123-Name", "https://attacker.test/release/456"])
        assert found and len(calls) == 1
        assert str(calls[0].url) == "https://api.discogs.com/releases/123"


def test_link_allowlist():
    assert linked_ids(["https://www.discogs.com/master/42-Test", "https://discogs.com/release/123",
                       "http://discogs.com/release/1", "https://discogs.com.evil/release/1"]) == [("masters", "42"), ("releases", "123")]


@pytest.mark.parametrize("status", [401, 403, 429])
async def test_api_cooldown_is_bounded_and_does_not_leak_credentials(status):
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(status, headers={"Retry-After": "99999"}))) as http:
        dc = DiscogsClient(token="secret-test-token", client=http)
        for _ in range(2):
            with pytest.raises(DiscogsError) as error:
                await dc._request("releases/123")
            assert "secret-test-token" not in str(error.value)
        assert len(calls) == 1


async def test_authentication_and_response_failures_do_not_create_negative_cache():
    cache = {}
    for response in [httpx.Response(500), httpx.Response(200, content="not-json"), httpx.Response(200, json=[])]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as http:
            dc = DiscogsClient(token="", client=http, cache_put=lambda key, value, seconds: cache.update({key: value}))
            with pytest.raises(DiscogsError):
                await dc._request("releases/123")
    assert not cache


def test_discogs_token_uses_separate_vault_and_never_lastfm_entry():
    values = {}
    class Vault:
        def set_password(self, service, username, value):
            values[service, username] = value
        def get_password(self, service, username):
            return values.get((service, username))
        def delete_password(self, service, username):
            del values[service, username]
    store = DiscogsTokenStore(backend=Vault())
    store.save_token("test-token-123")
    assert store.load_token() == "test-token-123"
    assert list(values) == [("osu-life-hacks-discogs", "token")]
    store.remove_token()
    assert not values


async def test_anonymous_search_works_and_search_hits_without_membership_do_not_match():
    def handler(request):
        assert "Authorization" not in request.headers
        data = {"results": [{"id": 123, "title": "Artist - Song", "format": ["EP"]}]} if "search" in request.url.path else release(title="Different song")
        return httpx.Response(200, json=data)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DiscogsClient(token="", client=http)
        found, notes = await client.candidates(Beatmap("map", "Song", "Artist"))
        assert not found and not notes
