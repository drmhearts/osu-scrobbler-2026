import asyncio
from pathlib import Path

import httpx
import pytest

from osu_scrobbler import musicbrainz
from osu_scrobbler import __version__
from osu_scrobbler.models import Beatmap, Track
from osu_scrobbler.musicbrainz import MusicBrainzClient, MusicBrainzError


RECORDING = "ed6af3e6-0717-4ec4-87c3-e30754019f6a"
RECORDING_2 = "9adc612c-5d0a-42e5-ae23-5a37d9a1cf0d"
RELEASE = "48b14517-5acb-4ab5-807c-d9d61e537a4b"


def recording(title="Song", artist="Artist", *, recording_id=RECORDING,
              length=180000, releases=None, comment=""):
    result = {"id": recording_id, "title": title, "score": 100,
              "length": length, "artist-credit": [{"name": artist}],
              "disambiguation": comment}
    if releases is not None:
        result["releases"] = releases
    return result


@pytest.fixture
def no_real_sleep(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(musicbrainz.time, "monotonic", lambda: now[0])

    async def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(musicbrainz.asyncio, "sleep", sleep)
    return now


def client(tmp_path, handler):
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return MusicBrainzClient(tmp_path / "cache.sqlite", client=http_client), http_client


async def test_selection_lookup_unicode_and_album_fallback(tmp_path, no_real_sleep):
    calls = []

    def handler(request):
        calls.append(request)
        data = recording("光", "宇多田ヒカル")
        return httpx.Response(200, json={"recordings": [data]} if request.url.path.endswith("/recording") else data)

    mb, http_client = client(tmp_path, handler)
    beatmap = Beatmap("selection", "Hikari", "Utada Hikaru", "光", "宇多田ヒカル", 180)
    result = await mb.lookup(beatmap)
    assert result.track.title == "光"
    assert result.track.artist == "宇多田ヒカル"
    assert result.track.album == "光"
    assert result.track.album_fallback is True
    assert result.track.album_artist == "宇多田ヒカル"
    assert result.track.release_id is None
    assert result.track.details["album_source"] == "track-title fallback"
    assert all(f"osu-life-hacks/{__version__}" in request.headers["User-Agent"] for request in calls)
    assert "光" in calls[0].url.params["query"]
    await mb.aclose()
    await http_client.aclose()


async def test_release_credits_and_recording_and_release_details(tmp_path, no_real_sleep):
    rel = {"id": RELEASE, "title": "Album", "status": "Official", "date": "2024-05-09"}
    rec = recording(releases=[rel])
    rec.update({"genres": [{"name": "electronic", "count": 2}], "isrcs": ["TEST123"]})
    release = {**rel, "artist-credit": [{"name": "Album Artist"}], "country": "JP",
               "release-group": {"primary-type": "Album", "secondary-types": []},
               "label-info": [{"label": {"name": "Test Label"}}]}

    def handler(request):
        if "/release/" in request.url.path:
            return httpx.Response(200, json=release)
        return httpx.Response(200, json={"recordings": [rec]} if request.url.path.endswith("/recording") else rec)

    mb, http_client = client(tmp_path, handler)
    result = await mb.lookup(Beatmap("selected", "Song", "Artist", duration=180))
    track = result.track
    assert track.album == "Album"
    assert track.album_artist == "Album Artist"
    assert track.album_fallback is False
    assert track.recording_id == RECORDING
    assert track.release_id == RELEASE
    assert track.details["recording"]["genres"][0]["name"] == "electronic"
    assert track.details["release"]["date"] == "2024-05-09"
    assert track.details["release"]["labels"] == ["Test Label"]
    assert track.details["source_links"]["recording"].endswith(RECORDING)
    await mb.aclose()
    await http_client.aclose()


async def test_ambiguous_recordings_require_choice(tmp_path, no_real_sleep):
    records = [recording(), recording(recording_id=RECORDING_2)]
    mb, http_client = client(tmp_path, lambda _: httpx.Response(200, json={"recordings": records}))
    result = await mb.lookup(Beatmap("selection", "Song", "Artist", duration=180))
    assert result.track is None
    assert len(result.candidates) == 2
    assert "Ambiguous" in result.reason
    await mb.aclose()
    await http_client.aclose()


@pytest.mark.parametrize("wanted,found", [
    ("Song (Alice remix)", "Song"),
    ("Song (Alice remix)", "Song (Bob remix)"),
    ("Song", "Song (instrumental)"),
])
async def test_wrong_version_not_silently_selected(tmp_path, no_real_sleep, wanted, found):
    mb, http_client = client(tmp_path, lambda _: httpx.Response(200, json={"recordings": [recording(found)]}))
    result = await mb.lookup(Beatmap("selection", wanted, "Artist", duration=180))
    assert result.track is None
    assert result.candidates[0].details["version_match"] is False
    assert "version" in result.reason
    await mb.aclose()
    await http_client.aclose()


async def test_tv_size_can_match_longer_recording_with_explicit_evidence(tmp_path, no_real_sleep):
    rec = recording(length=240000)
    mb, http_client = client(tmp_path, lambda request: httpx.Response(
        200, json={"recordings": [rec]} if request.url.path.endswith("/recording") else rec))
    result = await mb.lookup(Beatmap("selection", "Song (TV Size)", "Artist", duration=90))
    assert result.track is not None
    assert result.track.duration == 240
    assert "shorter" in result.track.details["game_version"]
    await mb.aclose()
    await http_client.aclose()


def test_featured_artists_and_romanization_rank_conservatively():
    bm = Beatmap("selection", "Café", "Beyoncé feat. Guest", duration=180)
    rec = recording("Cafe", "Beyonce feat. Guest")
    match, safe = musicbrainz._rank(bm, rec)
    assert safe
    assert match.confidence > 0.95
    wrong_feature, safe = musicbrainz._rank(bm, recording("Cafe", "Beyonce feat. Stranger"))
    assert not safe
    assert wrong_feature.confidence < match.confidence
    _, safe = musicbrainz._rank(bm, recording("Cafe", "Beyonce"))
    assert not safe


def test_unicode_field_cannot_remove_explicit_remix_requirement():
    bm = Beatmap("selection", "Song (Alice Remix)", "Artist", title_unicode="Song")
    _, safe = musicbrainz._rank(bm, recording())
    assert not safe


def test_recording_alias_matches_romanized_title():
    bm = Beatmap("selection", "Hikari", "Artist")
    rec = recording("光")
    rec["aliases"] = [{"name": "Hikari"}]
    _, safe = musicbrainz._rank(bm, rec)
    assert safe


async def test_durable_cache_and_corrections_across_difficulties(tmp_path, no_real_sleep):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"recordings": []})

    mb, http_client = client(tmp_path, handler)
    bm = Beatmap("first difficulty", "Song", "Artist", duration=90)
    await mb.lookup(bm)
    await mb.aclose()
    mb = MusicBrainzClient(tmp_path / "cache.sqlite", client=http_client)
    await mb.lookup(bm)
    assert len(calls) == 1
    correction = Track("Correct title", "Correct artist", "Correct album", "Correct album artist")
    mb.set_correction(bm, correction)
    await mb.aclose()
    mb = MusicBrainzClient(tmp_path / "cache.sqlite", client=http_client)
    different_difficulty = Beatmap("second difficulty", "Song", "Artist", duration=91)
    result = await mb.lookup(different_difficulty)
    assert result.track.title == "Correct title"
    assert result.track.verified
    assert result.track.details["manual_correction"]
    assert len(calls) == 1
    mb.clear_correction(bm)
    result = await mb.lookup(bm)
    assert result.track is None
    await mb.aclose()
    await http_client.aclose()


async def test_all_requests_serialized_and_rate_limited(tmp_path, no_real_sleep):
    starts = []

    def handler(request):
        starts.append(no_real_sleep[0])
        assert "Hky Sigh" in request.headers["User-Agent"]
        assert "github.com/drmhearts/osu-scrobbler-2026" in request.headers["User-Agent"]
        return httpx.Response(200, json={"recordings": []})

    mb, http_client = client(tmp_path, handler)
    await asyncio.gather(*[mb.lookup(Beatmap(str(i), f"Song {i}", "Artist")) for i in range(4)])
    assert len(starts) == 4
    assert all(b - a >= 1 for a, b in zip(starts, starts[1:]))
    await mb.aclose()
    await http_client.aclose()


@pytest.mark.parametrize("failure", ["timeout", "http", "json"])
async def test_failed_request_is_explicit_and_not_cached(tmp_path, no_real_sleep, failure):
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ConnectTimeout("offline", request=request)
        if failure == "http":
            return httpx.Response(503)
        return httpx.Response(200, content="invalid-json")

    mb, http_client = client(tmp_path, handler)
    for _ in range(2):
        with pytest.raises(MusicBrainzError):
            await mb.lookup(Beatmap("selection", "Song", "Artist"))
    assert len(calls) == 2
    await mb.aclose()
    await http_client.aclose()


async def test_missing_info_has_unknown_fields_no_invented_release(tmp_path, no_real_sleep):
    rec = recording(length=None)
    mb, http_client = client(tmp_path, lambda _: httpx.Response(200, json=rec))
    result = await mb.info(Track("Song", "Artist", "Song", "Artist", recording_id=RECORDING))
    assert result.details["release"]["date"] is None
    assert result.details["release"]["country"] is None
    assert result.details["release"]["type"] is None
    assert result.duration is None
    assert result.album_fallback
    await mb.aclose()
    await http_client.aclose()


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), "NaN", "Infinity", True, False, -5, 0])
def test_invalid_recording_duration_is_unknown(value):
    assert musicbrainz._seconds(value) is None


async def test_info_enriches_without_overwriting_manual_correction(tmp_path, no_real_sleep):
    rec = recording("Canonical song", "Canonical artist", releases=[{"id": RELEASE, "title": "Canonical album"}])
    release = {"id": RELEASE, "title": "Canonical album", "artist-credit": [{"name": "Canonical album artist"}],
               "date": "2024", "country": "JP"}
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=release if "/release/" in request.url.path else rec)

    mb, http_client = client(tmp_path, handler)
    corrected = Track("My title", "My artist", "My album", "My album artist", duration=90,
                      recording_id=RECORDING, release_id=RELEASE, details={"manual_correction": True})
    for _ in range(2):
        enriched = await mb.info(corrected)
        assert (enriched.title, enriched.artist, enriched.album, enriched.album_artist, enriched.duration) == (
            "My title", "My artist", "My album", "My album artist", 90)
        assert enriched.recording_id == RECORDING
        assert enriched.release_id == RELEASE
        assert enriched.details["recording"]["title"] == "Canonical song"
        assert enriched.details["recording"]["duration"] == 180
        assert enriched.details["release"]["date"] == "2024"
        assert enriched.details["release"]["title"] == "Canonical album"
        assert enriched.details["album_source"] == "manual correction"
    assert len(requests) == 2  # Second info call uses durable canonical cache.
    normal = await mb.info(Track("Song", "Artist", "Song", "Artist", recording_id=RECORDING, release_id=RELEASE))
    assert normal.title == "Canonical song"
    assert "manual_correction" not in normal.details
    await mb.aclose()
    await http_client.aclose()
