"""Source-neutral, deterministic release selection after recording verification.

Release preferences cannot make an unsafe recording eligible. Unknown releases
stay unknown; track counts alone never distinguish an EP from a single.
"""

from dataclasses import dataclass, replace
import math
from .credits import primary, split_credit
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


@dataclass(frozen=True)
class ReleaseCandidate:
    track: Track
    source: str
    score: float
    evidence: dict


def album_artist_match(track):
    if not track.album_artist:
        return 0.0
    if normal(track.album_artist) in {"various", "various artists"}:
        return 0.8
    return max(0.55, similarity(primary(track.artist), primary(track.album_artist)))


def score_candidate(track, beatmap, supporters=()):
    evidence = release_evidence(track, beatmap)
    relationship = album_artist_match(track)
    strength = track.details.get("membership_strength", 1.0 if track.details.get("source") == "MusicBrainz" else 0.97)
    consensus = len({t.details.get("source") for t in supporters} - {track.details.get("source"), None})
    score = track.confidence * 0.86 + strength * 0.08 + relationship * 0.06
    score += min(0.14, consensus * 0.07)
    if evidence["release_title_match"] is not None:
        score += evidence["release_title_match"] * 0.04
    score += 0.015 if evidence["position_match"] else 0
    score += 0.005 if evidence["date_match"] else 0
    evidence.update(album_artist_match=round(relationship, 4), membership_strength=strength,
                    agreeing_providers=consensus, recording_confidence=track.confidence)
    return ReleaseCandidate(track, track.details.get("source", "Unknown"), round(min(0.999, score), 5), evidence)


def release_key(track: Track, beatmap: Beatmap):
    """Comparable evidence first; release preference/popularity cannot rescue an unsafe match."""
    release = track.details.get("release") or {}
    scored = score_candidate(track, beatmap)
    evidence = track.details.get("release_evidence") or scored.evidence
    score = track.details.get("release_score", scored.score)
    edition = bool(re.search(r"\b(deluxe|expanded|anniversary|remaster|greatest hits|best of)\b",
                             normal(track.album + " " + str(release.get("disambiguation") or ""))))
    edition = edition or ("Live" in (release.get("secondary_types") or []) and "live" not in normal(beatmap.title))
    multi = track.release_type in MULTI_TYPES or (track.release_type == "Unknown" and (track.track_count or 0) > 1)
    kind = 0 if multi else (1 if track.release_type == "Single" else 2)
    listeners = track.details.get("listeners", 0)
    try:
        popularity = min(7.0, math.log10(max(0, float(listeners)) + 1))
    except (ValueError, TypeError):
        popularity = 0
    return (not evidence["identifier_match"], -(similarity(beatmap.album, track.album) >= 0.90 if beatmap.album else False),
            -int(score / 0.02),
            release.get("status") in {"Bootleg", "Unofficial", "Pseudo-Release", "Withdrawn"},
            -int(round(album_artist_match(track) / 0.1)), kind, edition,
            track.release_type in {"Compilation", "Soundtrack"},
            -popularity if track.details.get("source") == "Last.fm" else 0,
            date_key(track.release_date), -score,
            {"MusicBrainz": 0, "Discogs": 1, "Last.fm": 2}.get(track.details.get("source"), 3),
            normal(track.album), str(track.release_id or track.identifiers.get("discogs_release") or track.identifiers.get("lastfm_release") or ""))


def choose_releases(beatmap: Beatmap, candidates: list[Track]) -> list[Track]:
    # A failed identifier hint is not permission to silently use a different
    # edition. It can still be displayed as an optional manual candidate.
    safe = [t for t in candidates if t.verified and not t.album_fallback
            and t.details.get("membership_verified") and t.confidence >= 0.86]
    safe.sort(key=lambda t: release_key(t, beatmap))
    unique = []
    seen = set()
    for track in safe:
        key = (track.details.get("source"), track.release_id or track.identifiers.get("discogs_release") or track.identifiers.get("lastfm_release"),
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
    left_features, right_features = split_credit(left.artist).featured, split_credit(right.artist).featured
    if left_features and right_features and {normal(f) for f in left_features} != {normal(f) for f in right_features}:
        return False
    def album_credit(value):
        return "various artists" if normal(value) in {"various", "various artists"} else normal(value)
    return (similarity(left.album, right.album) >= 0.96
            and similarity(left.title, right.title) >= 0.96
            and similarity(primary(left.artist), primary(right.artist)) >= 0.96
            and (not left.album_artist or not right.album_artist or similarity(album_credit(left.album_artist), album_credit(right.album_artist)) >= 0.90)
            and (not left.duration or not right.duration or abs(left.duration - right.duration) <= max(4, min(left.duration, right.duration) * 0.04)))


def reconcile(beatmap: Beatmap, candidates: list[Track]):
    ranked = choose_releases(beatmap, candidates)
    if not ranked:
        return None, []
    # Consensus contributes before selection, once per independent provider.
    scored = []
    for candidate in ranked:
        support = [other for other in ranked if other.details.get("source") != candidate.details.get("source") and same_release(candidate, other)]
        model = score_candidate(candidate, beatmap, support)
        scored.append(replace(candidate, details={**candidate.details, "release_score": model.score, "release_evidence": model.evidence}))
    ranked = sorted(scored, key=lambda track: release_key(track, beatmap))
    selected = ranked[0]
    supporters = [t for t in ranked[1:] if t.details.get("source") != selected.details.get("source")
                  and same_release(selected, t)]
    supported_types = {t.release_type for t in supporters if t.release_type != "Unknown"}
    if selected.release_type == "Unknown" and len(supported_types) == 1:
        selected = replace(selected, release_type=next(iter(supported_types)))
    details = dict(selected.details)
    details["sources"] = sorted({selected.details.get("source"), *[t.details.get("source") for t in supporters]} - {None})
    details["source_metadata"] = {t.details.get("source"): t.details.get("release") or {}
                                  for t in [selected, *supporters]}
    details["release_confidence"] = selected.details["release_score"]
    details["release_evidence"] = selected.details["release_evidence"]
    details["ranking_rule"] = "identifiers, release hint, recording and membership confidence, provider agreement, album artist, release type, edition, Last.fm relevance, date, stable ID"
    details["release_alternatives"] = [
        {"title": t.album, "artist": t.album_artist, "type": t.release_type, "date": t.release_date,
         "track_number": t.track_number, "identifiers": t.identifiers, "confidence": t.confidence,
         "source": t.details.get("source"), "score": t.details.get("release_score")} for t in ranked[:10]
    ]
    identifiers = dict(selected.identifiers)
    for other in supporters:
        for key, value in other.identifiers.items():
            # Different editions must not overwrite the chosen edition's ID.
            identifiers.setdefault(key, value)
    return replace(selected, details=details, identifiers=identifiers), ranked
