"""Conservative MusicBrainz matching, durable caching, and explicit corrections.

Only public, read-only endpoints are used. MusicBrainz scores are search relevance,
not a guarantee that a recording is the song in a beatmap.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from difflib import SequenceMatcher
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
import unicodedata
from urllib.parse import quote

import httpx
from unidecode import unidecode

from .models import Beatmap, LookupResult, Track
from .releases import classify, choose_releases, release_key


from . import __version__

DEFAULT_USER_AGENT = (
    f"osu-life-hacks/{__version__} (Hky Sigh)"
)
API_URL = "https://musicbrainz.org/ws/2"
_CACHE_SECONDS = 7 * 24 * 60 * 60
_FEATURED = re.compile(r"\s+(?:feat\.?|ft\.?|featuring|with)\s+", re.I)
_SHORT_VERSION = re.compile(
    r"\b(?:tv(?:\s+size|\s+ver(?:sion)?)?|game\s+(?:size|ver(?:sion)?)|"
    r"short(?:\s+ver(?:sion)?|\s+edit)?|cut\s+ver(?:sion)?)\b", re.I
)
_ALTERNATE_VERSION = re.compile(
    r"\b(?:remix|mix|live|instrumental|karaoke|acoustic|cover|extended|"
    r"radio\s+edit|nightcore|sped\s+up|slowed|rework|remaster(?:ed)?)\b", re.I
)


class MusicBrainzError(RuntimeError):
    """A failed lookup, rather than evidence that no matching song exists."""


def _normal(value: str) -> str:
    value = unidecode(unicodedata.normalize("NFKC", value)).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", value).split())


def _similarity(left: str, right: str) -> float:
    left, right = _normal(left), _normal(right)
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _credit(value: object) -> str:
    """Preserve displayed artist credits, including MusicBrainz join phrases."""
    if not isinstance(value, list):
        return ""
    result = []
    for item in value:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            artist = item.get("artist") or {}
            result.append(str(item.get("name") or artist.get("name") or ""))
            result.append(str(item.get("joinphrase") or ""))
    return "".join(result).strip()


def _artist_names(recording: dict) -> list[str]:
    names = [_credit(recording.get("artist-credit"))]
    credits = recording.get("artist-credit") or []
    if credits and isinstance(credits[0], dict):
        artist = credits[0].get("artist") or {}
        names.extend([str(artist.get("name") or ""), str(artist.get("sort-name") or "")])
        names.extend(str(alias.get("name") or "") for alias in artist.get("aliases") or [])
    return [name for name in names if name]


def _versions(title: str, comment: str = "") -> tuple[str, set[str], list[str]]:
    """Separate explicit version labels while retaining meaningful title text."""
    short = set()
    alternate = []
    plain = title
    for match in re.finditer(r"\(([^()]*)\)|\[([^\[\]]*)\]", title):
        part = match.group(1) or match.group(2)
        if _SHORT_VERSION.search(part):
            short.add("short")
            plain = plain.replace(match.group(0), " ")
        if _ALTERNATE_VERSION.search(part):
            alternate.append(_normal(part))
            plain = plain.replace(match.group(0), " ")
    # A suffix such as '- TV Size' is common in osu! metadata.
    suffix = re.search(r"\s+[-–—]\s+(.+)$", plain)
    if suffix:
        part = suffix.group(1)
        if _SHORT_VERSION.search(part):
            short.add("short")
            plain = plain[:suffix.start()]
        if _ALTERNATE_VERSION.search(part):
            alternate.append(_normal(part))
            plain = plain[:suffix.start()]
    if comment:
        if _SHORT_VERSION.search(comment):
            short.add("short")
        if _ALTERNATE_VERSION.search(comment):
            alternate.append(_normal(comment))
    return plain.strip(), short, alternate


def _duration_score(expected: float | None, actual: float | None, shortened: bool) -> float:
    if expected is None or actual is None:
        return 0.7
    gap = abs(actual - expected)
    if gap <= max(4.0, expected * 0.04):
        return 1.0
    # A cut used in the game can belong to a longer recording, but this is weaker
    # evidence than matching the actual short recording.
    if actual > expected and (shortened or expected / actual >= 0.4):
        return 0.55
    return max(0.0, 1.0 - gap / max(expected * 0.5, 20.0))


def _seconds(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value) / 1000
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _release_key(release: dict) -> tuple:
    group = release.get("release-group") or {}
    primary = group.get("primary-type") or ""
    secondary = group.get("secondary-types") or []
    return (
        release.get("status") != "Official",
        "Compilation" in secondary or "Live" in secondary,
        {"Album": 0, "EP": 1, "Single": 3}.get(primary, 3),
        release.get("date") or "9999",
        release.get("id") or "",
    )


def _release_track(recording: dict, release: dict, confidence: float) -> Track:
    artist = _credit(recording.get("artist-credit")) or "Unknown"
    title = str(recording.get("title") or "Unknown")
    group = release.get("release-group") or {}
    kind = classify(group.get("primary-type"), group.get("secondary-types") or [])
    media = release.get("media") or []
    position = None
    disc = None
    count = sum(m.get("track-count") or len(m.get("tracks") or []) for m in media if isinstance(m, dict)) or None
    for medium in media:
        for item in medium.get("tracks") or []:
            if (item.get("recording") or {}).get("id") == recording.get("id"):
                position = str(item.get("number") or item.get("position") or "") or None
                disc = medium.get("position")
                break
    ids = {}
    for key, value in (("musicbrainz_recording", recording.get("id")), ("musicbrainz_release", release.get("id")),
                       ("musicbrainz_release_group", group.get("id"))):
        if value:
            ids[key] = value
    links = {key.replace("musicbrainz_", ""): "https://musicbrainz.org/" + key.replace("musicbrainz_", "").replace("_", "-") + "/" + value
             for key, value in ids.items()}
    discogs_links = [relation.get("url", {}).get("resource") for obj in (recording, release)
                     for relation in obj.get("relations") or [] if isinstance(relation.get("url"), dict)]
    return Track(
        title=title, artist=artist, album=str(release.get("title") or title),
        album_artist=_credit(release.get("artist-credit")) or artist,
        duration=_seconds(recording.get("length")), recording_id=recording.get("id"),
        release_id=release.get("id"), album_fallback=not bool(release.get("title")),
        confidence=confidence, verified=True, release_type=kind,
        release_date=release.get("date"), track_number=position, disc_number=disc,
        track_count=count, identifiers=ids,
        details={
            "source": "MusicBrainz", "membership_verified": bool(release.get("id")),
            "album_source": "MusicBrainz release" if release.get("title") else "track-title fallback",
            "recording": {"title": recording.get("title"), "artist": artist, "id": recording.get("id"),
                          "duration": _seconds(recording.get("length")), "genres": recording.get("genres") or [],
                          "tags": recording.get("tags") or [], "isrcs": recording.get("isrcs") or [],
                          "disambiguation": recording.get("disambiguation") or None,
                          "first_release_date": recording.get("first-release-date")},
            "release": {"title": release.get("title"), "artist": _credit(release.get("artist-credit")) or None,
                        "id": release.get("id"), "date": release.get("date"), "country": release.get("country"),
                        "status": release.get("status"), "type": group.get("primary-type"),
                        "classification": kind, "secondary_types": group.get("secondary-types") or [],
                        "genres": release.get("genres") or [], "tags": release.get("tags") or [],
                        "labels": [(item.get("label") or {}).get("name") for item in release.get("label-info") or []
                                   if isinstance(item, dict) and (item.get("label") or {}).get("name")],
                        "track_count": count, "track_number": position, "disc_number": disc,
                        "disambiguation": release.get("disambiguation"), "release_group_id": group.get("id")},
            "source_links": links, "discogs_links": [link for link in discogs_links if link],
        },
    )


def _track(recording: dict, confidence: float = 0.0) -> Track:
    releases = [r for r in recording.get("releases") or [] if isinstance(r, dict) and r.get("title")]
    return _release_track(recording, min(releases, key=_release_key) if releases else {}, confidence)


def _rank(beatmap: Beatmap, recording: dict) -> tuple[Track, bool]:
    requested_titles = list(dict.fromkeys(t for t in (beatmap.title, beatmap.title_unicode) if t))
    requested_artists = list(dict.fromkeys(a for a in (beatmap.artist, beatmap.artist_unicode) if a))
    requested_versions = [v for title in requested_titles for v in _versions(title)[2]]
    candidate_title, candidate_short, candidate_versions = _versions(
        str(recording.get("title") or ""), str(recording.get("disambiguation") or "")
    )
    candidate_titles = [candidate_title] + [
        _versions(str(alias.get("name") or ""))[0] for alias in recording.get("aliases") or []
        if isinstance(alias, dict)
    ]
    title_scores = []
    version_safe = False
    shortened = False
    for title in requested_titles:
        base, short, versions = _versions(title)
        # Versions are deliberately strict: 'remix' alone does not establish that
        # the remix by a different producer is the correct one.
        safe = (not requested_versions and not candidate_versions) or (
            bool(requested_versions) and bool(candidate_versions)
            and all(any(_similarity(a, b) >= 0.86 for b in candidate_versions) for a in requested_versions)
            and all(any(_similarity(a, b) >= 0.86 for a in requested_versions) for b in candidate_versions)
        )
        # Only a spelling that actually matches the candidate title can establish
        # a safe version; an unrelated Unicode field cannot excuse a wrong remix.
        base_similarity = max(_similarity(base, title) for title in candidate_titles)
        version_safe = version_safe or (safe and base_similarity >= 0.87)
        shortened = shortened or bool(short or candidate_short)
        title_scores.append(base_similarity)
    title_score = max(title_scores, default=0.0)
    artist_score = 0.0
    feature_mismatch = False
    credited_artists = _normal(_credit(recording.get("artist-credit")))
    for expected in requested_artists:
        expected_parts = _FEATURED.split(expected)
        if len(expected_parts) > 1 and _normal(" ".join(expected_parts[1:])) not in credited_artists:
            feature_mismatch = True
        for actual in _artist_names(recording):
            actual_parts = _FEATURED.split(actual)
            score = _similarity(expected, actual)
            if len(expected_parts) > 1 or len(actual_parts) > 1:
                main_score = _similarity(expected_parts[0], actual_parts[0])
                score = max(score, main_score * 0.93)
                if len(expected_parts) > 1 and len(actual_parts) > 1:
                    if _similarity(" ".join(expected_parts[1:]), " ".join(actual_parts[1:])) < 0.75:
                        feature_mismatch = True
                        score *= 0.75
            artist_score = max(artist_score, score)
    duration = _seconds(recording.get("length"))
    duration_score = _duration_score(beatmap.duration, duration, shortened)
    try:
        relevance = min(1.0, float(recording.get("score", 0)) / 100)
    except (TypeError, ValueError):
        relevance = 0.0
    confidence = title_score * 0.50 + artist_score * 0.38 + duration_score * 0.10 + relevance * 0.02
    if not version_safe:
        confidence = min(confidence, 0.65)
    eligible = (
        version_safe and not feature_mismatch and title_score >= 0.87 and artist_score >= 0.85
        and confidence >= 0.86
        and (beatmap.duration is None or duration is None or duration >= beatmap.duration * 0.75)
    )
    identifier_match = bool((beatmap.recording_id and recording.get("id") == beatmap.recording_id)
                            or (beatmap.isrc and beatmap.isrc.upper() in [str(i).upper() for i in recording.get("isrcs") or []]))
    if identifier_match:
        confidence, eligible = 1.0, True
    result = _track(recording, round(confidence, 4))
    result.details["identifier_match"] = identifier_match
    result.details.update({"title_match": round(title_score, 4), "artist_match": round(artist_score, 4),
                           "version_match": version_safe, "duration_match": round(duration_score, 4)})
    if beatmap.duration and duration and duration > beatmap.duration * 1.25:
        result.details["game_version"] = "Beatmap audio is shorter than the MusicBrainz recording"
    return result, eligible


def _query(beatmap: Beatmap) -> str:
    def field(name: str, values: list[str]) -> str:
        terms = []
        for value in dict.fromkeys(values):
            if value:
                escaped = re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r'\\\1', value)
                terms.append(f'{name}:"{escaped}"')
        return "(" + " OR ".join(terms) + ")" if terms else ""
    titles = [_versions(title)[0] for title in (beatmap.title, beatmap.title_unicode) if title]
    artists = [_FEATURED.split(a)[0] for a in (beatmap.artist, beatmap.artist_unicode) if a]
    return " AND ".join(part for part in (field("recording", titles), field("artist", artists)) if part)


class MusicBrainzClient:
    def __init__(self, cache_path: Path, user_agent: str = DEFAULT_USER_AGENT,
                 client: httpx.AsyncClient | None = None):
        if not user_agent.strip() or "/" not in user_agent or "(" not in user_agent:
            raise ValueError("MusicBrainz User-Agent must identify app/version and contact information")
        cache_path = Path(cache_path)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(cache_path, timeout=10)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA secure_delete=ON")
        self._db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, expires REAL NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS corrections (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self._prune_cache()
        if self._db.execute("PRAGMA user_version").fetchone()[0] < 1:
            for key, value in self._db.execute("SELECT key,value FROM corrections"):
                corrected = replace(Track.from_dict(json.loads(value)), details={"manual_correction": True})
                self._db.execute("UPDATE corrections SET value=? WHERE key=?",
                                 (json.dumps(corrected.to_dict(), ensure_ascii=False), key))
            self._db.execute("PRAGMA user_version=1")
        self._db.commit()
        self._client = client or httpx.AsyncClient(timeout=5, follow_redirects=False)
        self._owns_client = client is None
        # Keep a usable contact for the API protocol without displaying a source
        # link in the application's default configuration or welcome output.
        if re.fullmatch(r"osu-life-hacks/[\d.]+ \(Hky Sigh\)", user_agent):
            user_agent = user_agent[:-1] + "; https://github.com/drmhearts/osu-scrobbler-2026)"
        self._headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self._request_lock = asyncio.Lock()
        self._last_request: float | None = None

    @staticmethod
    def _lookup_key(beatmap: Beatmap) -> str:
        value = [beatmap.title, beatmap.artist, beatmap.title_unicode, beatmap.artist_unicode, beatmap.duration,
                 beatmap.album, beatmap.track_number, beatmap.release_date, beatmap.recording_id,
                 beatmap.release_id, beatmap.release_group_id, beatmap.isrc, beatmap.discogs_release_id]
        return "lookup:v2:" + hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()

    def _get(self, key: str) -> dict | None:
        with self._db:
            self._prune_cache()
        row = self._db.execute("SELECT value, expires FROM cache WHERE key = ?", (key,)).fetchone()
        if not row or row[1] < time.time():
            return None
        try:
            value = json.loads(row[0])
            return value if isinstance(value, dict) else None
        except (TypeError, ValueError):
            return None

    def _put(self, key: str, value: dict, seconds: int = _CACHE_SECONDS) -> None:
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
                             (key, json.dumps(value, ensure_ascii=False), time.time() + seconds))
            self._prune_cache()

    def _prune_cache(self) -> None:
        self._db.execute("DELETE FROM cache WHERE expires <= ?", (time.time(),))
        self._db.execute("DELETE FROM cache WHERE key IN (SELECT key FROM cache ORDER BY expires DESC, key LIMIT -1 OFFSET 256)")

    def set_correction(self, beatmap: Beatmap, track: Track) -> None:
        corrected = replace(track, confidence=1.0, verified=True,
                            details={"manual_correction": True})
        self._db.execute("INSERT OR REPLACE INTO corrections VALUES (?, ?)",
                         (beatmap.track_key, json.dumps(corrected.to_dict(), ensure_ascii=False)))
        self._db.commit()

    def clear_correction(self, beatmap: Beatmap) -> None:
        self._db.execute("DELETE FROM corrections WHERE key = ?", (beatmap.track_key,))
        self._db.commit()

    async def _request(self, entity: str, params: dict) -> dict:
        async with self._request_lock:
            if self._last_request is not None:
                wait = 1.0 - (time.monotonic() - self._last_request)
                if wait > 0:
                    await asyncio.sleep(wait)
            self._last_request = time.monotonic()
            try:
                response = await self._client.get(f"{API_URL}/{entity}",
                                                  params={**params, "fmt": "json"}, headers=self._headers)
                response.raise_for_status()
                data = response.json()
            except httpx.HTTPStatusError as error:
                raise MusicBrainzError(f"MusicBrainz returned HTTP {error.response.status_code}; lookup unavailable") from error
            except httpx.HTTPError as error:
                raise MusicBrainzError(f"MusicBrainz connection failed ({type(error).__name__}); retry when online") from error
            except ValueError as error:
                raise MusicBrainzError("MusicBrainz returned invalid JSON") from error
            if not isinstance(data, dict):
                raise MusicBrainzError("MusicBrainz returned an unexpected response")
            return data

    def get_correction(self, beatmap: Beatmap) -> Track | None:
        correction = self._db.execute("SELECT value FROM corrections WHERE key = ?", (beatmap.track_key,)).fetchone()
        if correction:
            try:
                track = Track.from_dict(json.loads(correction[0]))
                return track
            except (TypeError, ValueError):
                pass
        return None

    async def lookup(self, beatmap: Beatmap) -> LookupResult:
        correction = self.get_correction(beatmap)
        if correction:
            return LookupResult(correction, [correction], "Persistent manual correction")
        key = self._lookup_key(beatmap)
        cached = self._get(key)
        if cached:
            try:
                return LookupResult(Track.from_dict(cached["track"]) if cached.get("track") else None,
                                    [Track.from_dict(t) for t in cached.get("candidates", [])], cached.get("reason", ""))
            except (TypeError, ValueError, KeyError):
                pass
        query = _query(beatmap)
        if not query and not (beatmap.recording_id or beatmap.isrc):
            return LookupResult(None, [], "Beatmap title and artist are unknown; select or correct a track")
        if beatmap.recording_id:
            data = await self._request("recording/" + quote(beatmap.recording_id, safe=""),
                                       {"inc": "artist-credits+releases+isrcs"})
            response = {"recordings": [data]}
        else:
            if beatmap.isrc:
                query = 'isrc:"' + re.sub(r"[^A-Za-z0-9]", "", beatmap.isrc) + '"'
            response = await self._request("recording", {"query": query, "limit": 25})
        ranked = [_rank(beatmap, r) for r in response.get("recordings", []) if isinstance(r, dict)]
        ranked.sort(key=lambda row: (-row[0].confidence, row[0].identity))
        # The search may return the same recording more than once. Different
        # releases of the same recording are not ambiguous recording identities.
        unique: list[tuple[Track, bool]] = []
        seen: set[str] = set()
        for candidate, safe in ranked:
            if candidate.identity not in seen:
                unique.append((candidate, safe))
                seen.add(candidate.identity)
        candidates = [row[0] for row in unique[:10]]
        track = None
        reason = "No matching MusicBrainz recording; track-title album fallback is available"
        if unique:
            best, safe = unique[0]
            ambiguous = len(unique) > 1 and best.confidence - unique[1][0].confidence < 0.08
            if safe and not ambiguous:
                track = await self.info(best, beatmap=beatmap)
                reason = "Confident MusicBrainz recording match"
            else:
                reason = "Ambiguous MusicBrainz matches; choose a candidate or correct the metadata" if ambiguous else (
                    "Low-confidence or different-version match; choose a candidate or correct the metadata")
        result = LookupResult(track, candidates, reason)
        self._put(key, {"track": track.to_dict() if track else None,
                        "candidates": [t.to_dict() for t in candidates], "reason": reason},
                  300 if track and track.details.get("lookup_warnings") else (_CACHE_SECONDS if track else 24 * 60 * 60))
        return result

    async def info(self, track: Track, *, beatmap: Beatmap | None = None) -> Track:
        if not track.recording_id:
            return track
        key = f"info:v2:{track.recording_id}:{track.release_id or ''}"
        if beatmap is not None:
            key += self._lookup_key(beatmap)
        cached = self._get(key)
        if cached:
            try:
                return self._enriched(track, Track.from_dict(cached))
            except (TypeError, ValueError):
                pass
        recording = await self._request("recording/" + quote(track.recording_id, safe=""),
                                        {"inc": "artist-credits+releases+genres+tags+isrcs+url-rels"})
        recording.setdefault("id", track.recording_id)
        releases = {r["id"]: r for r in recording.get("releases") or [] if isinstance(r, dict) and r.get("id")}
        warnings = []
        if beatmap is not None:
            try:
                # Embedded recording releases are limited to 25, sorted by UUID,
                # not by confidence or date. Browse real recording membership.
                page = await self._request("release", {"recording": track.recording_id, "limit": 100,
                    "offset": 0, "inc": "artist-credits+release-groups+media"})
                releases.update({r["id"]: r for r in page.get("releases") or [] if isinstance(r, dict) and r.get("id")})
                if page.get("release-count", 0) > 100:
                    warnings.append("MusicBrainz release candidate budget reached (first 100)")
            except MusicBrainzError as error:
                warnings.append(str(error))
        provisional = [_release_track(recording, r, track.confidence) for r in releases.values()]
        context = beatmap or Beatmap("info", track.title, track.artist, release_id=track.release_id or "")
        provisional.sort(key=lambda t: release_key(t, context))
        ids = [t.release_id for t in provisional]
        if beatmap is None and track.release_id:
            ids = [track.release_id]
        elif beatmap and beatmap.release_id:
            # A trusted release tag still requires this recording in its tracklist.
            ids = [beatmap.release_id] + [rid for rid in ids if rid != beatmap.release_id]
        # Fetch a bounded set of distinct groups rather than duplicate pressings.
        selected_ids = []
        groups = set()
        for rid in ids:
            group = ((releases.get(rid) or {}).get("release-group") or {}).get("id") or rid
            if group not in groups:
                selected_ids.append(rid)
                groups.add(group)
            if len(selected_ids) >= (3 if beatmap is not None else 1):
                break
        for rid in selected_ids:
            try:
                release = await self._request("release/" + quote(rid, safe=""),
                    {"inc": "artist-credits+release-groups+labels+genres+tags+recordings+url-rels"})
                release.setdefault("id", rid)
                items = [item for m in release.get("media") or [] for item in m.get("tracks") or []]
                if items and not any((item.get("recording") or {}).get("id") == track.recording_id for item in items):
                    releases.pop(rid, None)
                    warnings.append("Rejected a release whose tracklist does not contain this recording")
                    continue
                # Direct release hints must have tracklist evidence; title alone
                # cannot establish membership in a different release.
                if rid not in releases and not items:
                    continue
                releases[rid] = {**releases.get(rid, {}), **release}
            except MusicBrainzError as error:
                warnings.append(str(error))
        options = choose_releases(context, [_release_track(recording, r, track.confidence) for r in releases.values()])
        resolved = options[0] if options else _release_track(recording, {}, track.confidence)
        # info() respects a user-selected release even without automatic confidence.
        if beatmap is None and track.release_id in releases:
            resolved = _release_track(recording, releases[track.release_id], track.confidence)
        resolved.details["release_candidates"] = [t.to_dict() for t in options[:10]]
        resolved.details["lookup_warnings"] = warnings
        self._put(key, resolved.to_dict(), 300 if warnings else _CACHE_SECONDS)
        return self._enriched(track, resolved)

    @staticmethod
    def _enriched(requested: Track, canonical: Track) -> Track:
        details = {**requested.details, **canonical.details}
        if requested.details.get("manual_correction"):
            details["album_source"] = "track-title fallback" if requested.album_fallback else "manual correction"
            return replace(requested, details=details)
        return replace(canonical, confidence=requested.confidence, details=details)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
        self._db.close()

    async def close(self) -> None:
        await self.aclose()
