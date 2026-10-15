"""Source-neutral, deterministic release selection after recording verification.

Release preferences cannot make an unsafe recording eligible. Unknown releases
stay unknown; track counts alone never distinguish an EP from a single.
"""

from dataclasses import replace
from difflib import SequenceMatcher
import re
import unicodedata

from unidecode import unidecode

from .models import Beatmap, Track


MULTI_TYPES = {"Album", "EP", "Compilation", "Soundtrack", "Mixtape"}


def normal(value):
    return " ".join(re.sub(r"[^\w]+", " ", unidecode(unicodedata.normalize(
        "NFKC", str(value or ""))).casefold()).split())


def similarity(left, right):
    left, right = normal(left), normal(right)
    return SequenceMatcher(None, left, right).ratio() if left and right else 0.0


def classify(primary="", secondary=()):
    values = {normal(value) for value in [primary, *secondary]}
    # Keep primary and secondary types separately in details, but expose the
    # more descriptive secondary type for soundtracks/compilations/mixtapes.
    for label in ("Soundtrack", "Compilation", "Mixtape", "EP", "Album", "Single"):
        if (normal(label) in values or (label == "Mixtape" and "mixtape street" in values)
                or (label == "Single" and "maxi single" in values)):
            return label
    return "Unknown"


def date_key(value):
    if not re.fullmatch(r"\d{4}(?:-\d{2})?(?:-\d{2})?", value or ""):
        return "9999-99-99"
    return (value + "-00-00")[:10]


def position_key(value):
    value = str(value or "").strip()
    if re.fullmatch(r"\d+(?:/\d+)?", value):
        return str(int(value.split("/")[0]))
    return normal(value)


def release_evidence(track, beatmap):
    identifiers = track.identifiers
    exact = bool(
        (beatmap.release_id and track.release_id == beatmap.release_id)
        or (beatmap.release_group_id and identifiers.get("musicbrainz_release_group") == beatmap.release_group_id)
        or (beatmap.discogs_release_id and identifiers.get("discogs_release") == str(beatmap.discogs_release_id)))
    return {"identifier_match": exact,
            "recording_confidence": track.confidence,
            "title_match": track.details.get("title_match"), "artist_match": track.details.get("artist_match"),
            "duration_match": track.details.get("duration_match"),
            "release_title_match": similarity(beatmap.album, track.album) if beatmap.album else None,
            "position_match": position_key(beatmap.track_number) == position_key(track.track_number) if beatmap.track_number and track.track_number else None,
            "date_match": track.release_date.startswith(beatmap.release_date) if beatmap.release_date and track.release_date else None,
            "membership_verified": bool(track.details.get("membership_verified")),
            "track_count": track.track_count, "release_type": track.release_type}


def release_key(track: Track, beatmap: Beatmap):
    """Identity/hints, confidence, official original releases, date, stable ID."""
    release = track.details.get("release") or {}
    ids = track.identifiers
    exact = bool(
        (beatmap.release_id and track.release_id == beatmap.release_id)
        or (beatmap.release_group_id and ids.get("musicbrainz_release_group") == beatmap.release_group_id)
        or (beatmap.discogs_release_id and ids.get("discogs_release") == str(beatmap.discogs_release_id))
    )
    hint = similarity(beatmap.album, track.album) if beatmap.album else 0.0
    position = bool(beatmap.track_number and position_key(beatmap.track_number) == position_key(track.track_number))
    date = bool(beatmap.release_date and track.release_date
                and track.release_date.startswith(beatmap.release_date))
    evidence = track.confidence + hint * 0.08 + position * 0.02 + date * 0.01
    status = release.get("status") or ""
    edition = bool(re.search(r"\b(deluxe|expanded|anniversary|remaster|greatest hits|best of)\b",
                             normal(track.album + " " + str(release.get("disambiguation") or ""))))
    edition = edition or ("Live" in (release.get("secondary_types") or []) and "live" not in normal(beatmap.title))
    # Confidence is compared in 0.02 bands: soft release preferences only resolve
    # similarly strong recordings. Explicit album/identifier hints take priority.
    return (
        not exact, -(hint >= 0.90), -int(round(evidence / 0.02)),
        status in {"Bootleg", "Unofficial", "Pseudo-Release", "Withdrawn"},
        track.album_fallback, track.release_type == "Single",
        track.release_type not in MULTI_TYPES,
        not bool(track.track_count and track.track_count > 1),
        edition, track.release_type in {"Compilation", "Soundtrack"},
        date_key(track.release_date), -evidence,
        track.details.get("source") != "MusicBrainz",
        normal(track.album), str(track.release_id or ids.get("discogs_release") or ""),
    )


def choose_releases(beatmap: Beatmap, candidates: list[Track]) -> list[Track]:
    # A failed identifier hint is not permission to silently use a different
    # edition. It can still be displayed as an optional manual candidate.
    safe = [t for t in candidates if t.verified and not t.album_fallback
            and t.details.get("membership_verified") and t.confidence >= 0.86]
    safe.sort(key=lambda t: release_key(t, beatmap))
    unique = []
    seen = set()
    for track in safe:
        key = (track.details.get("source"), track.release_id or track.identifiers.get("discogs_release"),
               track.track_number, track.identity)
        if key not in seen:
            unique.append(track)
            seen.add(key)
    return unique


def same_release(left: Track, right: Track):
    # Text agreement is corroboration, never a new recording identifier.
    if left.release_type != right.release_type and "Unknown" not in {left.release_type, right.release_type}:
        return False
    if left.release_date and right.release_date and left.release_date[:4] != right.release_date[:4]:
        return False
    return (similarity(left.album, right.album) >= 0.96
            and similarity(left.title, right.title) >= 0.96
            and similarity(left.artist, right.artist) >= 0.96)


def reconcile(beatmap: Beatmap, candidates: list[Track]):
    ranked = choose_releases(beatmap, candidates)
    if not ranked:
        return None, []
    selected = ranked[0]
    supporters = [t for t in ranked[1:] if t.details.get("source") != selected.details.get("source")
                  and same_release(selected, t)]
    details = dict(selected.details)
    details["sources"] = sorted({selected.details.get("source"), *[t.details.get("source") for t in supporters]} - {None})
    details["source_metadata"] = {t.details.get("source"): t.details.get("release") or {}
                                  for t in [selected, *supporters]}
    details["release_confidence"] = round(selected.confidence, 4)
    details["release_evidence"] = release_evidence(selected, beatmap)
    details["ranking_rule"] = "identifiers, release hint, recording confidence, official multi-track release, original edition, date, stable ID"
    details["release_alternatives"] = [
        {"title": t.album, "artist": t.album_artist, "type": t.release_type, "date": t.release_date,
         "track_number": t.track_number, "identifiers": t.identifiers, "confidence": t.confidence,
         "source": t.details.get("source")} for t in ranked[:10]
    ]
    identifiers = dict(selected.identifiers)
    for other in supporters:
        for key, value in other.identifiers.items():
            # Different editions must not overwrite the chosen edition's ID.
            identifiers.setdefault(key, value)
    return replace(selected, details=details, identifiers=identifiers), ranked
