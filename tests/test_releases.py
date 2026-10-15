from dataclasses import replace
from itertools import permutations

import pytest

from osu_scrobbler.models import Beatmap, Track
from osu_scrobbler.releases import classify, reconcile


def candidate(title="Original", kind="Album", date="2000", *, source="MusicBrainz", confidence=0.99, identifier="release", **kwargs):
    return Track("Song", "Artist", title, "Artist", 180, recording_id="recording" if source == "MusicBrainz" else None,
                 release_id=identifier if source == "MusicBrainz" else None, confidence=confidence,
                 verified=True, release_type=kind, release_date=date, track_count=10,
                 identifiers={"musicbrainz_release" if source == "MusicBrainz" else "discogs_release": identifier},
                 details={"source": source, "membership_verified": True, "release": {"status": "Official"}}, **kwargs)


@pytest.mark.parametrize("primary,secondary,expected", [
    ("Album", [], "Album"), ("EP", [], "EP"), ("Single", [], "Single"),
    ("Album", ["Compilation"], "Compilation"), ("Album", ["Soundtrack"], "Soundtrack"),
    ("Album", ["Mixtape/Street"], "Mixtape"), ("", [], "Unknown"),
    ("", ["LP", '12"'], "Unknown"), ("", ["EP", "Mini-Album"], "EP"),
])
def test_explicit_types(primary, secondary, expected):
    assert classify(primary, secondary) == expected


@pytest.mark.parametrize("kind", ["Album", "EP", "Compilation", "Soundtrack", "Mixtape"])
def test_confident_real_release_beats_single_in_any_api_order(kind):
    choices = [candidate("Song", "Single", "1999", identifier="single"), candidate(kind=kind)]
    for order in permutations(choices):
        winner, _ = reconcile(Beatmap("map", "Song", "Artist"), list(order))
        assert winner.release_type == kind


def test_hint_can_choose_compilation_over_original_or_exact_single():
    album = candidate()
    compilation = candidate("Collection", "Compilation", "2010", identifier="comp")
    single = candidate("Song", "Single", "1999", identifier="single")
    for order in permutations([album, compilation, single]):
        winner, _ = reconcile(Beatmap("map", "Song", "Artist", album="Collection"), list(order))
        assert winner.release_type == "Compilation"
        winner, _ = reconcile(Beatmap("map", "Song", "Artist", release_id="single"), list(order))
        assert winner.release_type == "Single"


def test_album_preferences_never_rescue_weak_or_unverified_matches():
    single = candidate("Song", "Single", identifier="single")
    weak = candidate(confidence=0.6)
    unverified = replace(candidate(), details={"source": "Discogs"})
    winner, _ = reconcile(Beatmap("map", "Song", "Artist"), [single, weak, unverified])
    assert winner.release_type == "Single"


def test_original_then_earlier_date_and_stable_id_are_deterministic():
    choices = [candidate("Original Deluxe", date="1998", identifier="deluxe"),
               candidate(date="2000", identifier="b"), candidate(date="2000", identifier="a"),
               candidate("Collection", "Compilation", "1990", identifier="comp")]
    for order in permutations(choices):
        winner, _ = reconcile(Beatmap("map", "Song", "Artist"), list(order))
        assert winner.release_id == "a"


def test_sources_corroborate_without_conflating_types_or_edition_dates():
    mb = candidate()
    discogs = candidate(source="Discogs", identifier="123")
    winner, _ = reconcile(Beatmap("map", "Song", "Artist"), [discogs, mb])
    assert winner.details["sources"] == ["Discogs", "MusicBrainz"]
    assert winner.identifiers["discogs_release"] == "123"
    other = replace(discogs, release_date="2020")
    winner, _ = reconcile(Beatmap("map", "Song", "Artist"), [mb, other])
    assert winner.details["sources"] == ["MusicBrainz"]


def test_unknown_fallback_is_never_a_confirmed_single():
    fallback = Track.fallback(Beatmap("map", "Song", "Artist"))
    assert fallback.release_type == "Unknown"
    assert reconcile(Beatmap("map", "Song", "Artist"), [fallback]) == (None, [])


def test_track_position_hint_normalizes_padding_and_total_count():
    first = replace(candidate(identifier="a"), track_number="1")
    second = replace(candidate(identifier="b"), track_number="02")
    winner, _ = reconcile(Beatmap("map", "Song", "Artist", track_number="2/10"), [first, second])
    assert winner.release_id == "b"
    assert winner.details["release_evidence"]["position_match"] is True
