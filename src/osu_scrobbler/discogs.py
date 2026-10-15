"""Bounded read-only Discogs database access; never use search hits as track proof."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import os
import re
import time
from urllib.parse import urlparse

import httpx

from . import __version__
from .lastfm import CredentialStore, CredentialError
from .models import Beatmap, Track
from .credits import primary
from .musicbrainz import _rank, _versions
from .releases import classify, normal


API_URL = "https://api.discogs.com"


class DiscogsError(RuntimeError):
    pass


class DiscogsTokenStore(CredentialStore):
    def __init__(self, *, backend=None):
        super().__init__(service="osu-life-hacks-discogs", username="token", backend=backend)

    def load_token(self):
        try:
            return self._secure_backend().get_password(self.service, self.username) or ""
        except Exception:
            return ""  # Metadata remains usable when the vault is unavailable.

    def save_token(self, value):
        if not value or not re.fullmatch(r"[A-Za-z0-9_-]{8,256}", value):
            raise ValueError("Enter a valid Discogs personal access token")
        try:
            self._secure_backend().set_password(self.service, self.username, value)
        except Exception as error:
            raise CredentialError("Could not store Discogs token in the OS credential vault") from error

    def remove_token(self):
        try:
            if self.load_token():
                self._secure_backend().delete_password(self.service, self.username)
        except Exception as error:
            raise CredentialError("Could not remove Discogs token from the OS credential vault") from error


def configured_token():
    return os.environ.get("OSU_LIFE_HACKS_DISCOGS_TOKEN", "").strip() or DiscogsTokenStore().load_token()


def credit(artists):
    result = []
    for artist in artists or []:
        if isinstance(artist, dict):
            # Discogs disambiguates artists with '(2)'; this is not part of a
            # recording's displayed credit. ANVs preserve the printed spelling.
            name = re.sub(r"\s+\(\d+\)$", "", str(artist.get("anv") or artist.get("name") or ""))
            result.append(name + str(artist.get("join") or ""))
    return " ".join(result).strip()


def seconds(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,3}:\d{2}(?::\d{2})?", value):
        return None
    parts = [int(x) for x in value.split(":")]
    if any(x >= 60 for x in parts[1:]):
        return None
    total = 0
    for part in parts:
        total = total * 60 + part
    return float(total) if total > 0 else None


def tracks(items, inherited=None):
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get("sub_tracks"):
            yield from tracks(item["sub_tracks"], item.get("artists") or inherited)
        elif item.get("type_", "track") == "track" and item.get("title"):
            yield {**item, "artists": item.get("artists") or inherited}


def release_tracks(beatmap: Beatmap, release: dict):
    artist = credit(release.get("artists"))
    descriptions = [value for fmt in release.get("formats") or [] if isinstance(fmt, dict)
                    for value in fmt.get("descriptions") or []]
    kind = classify("", descriptions)
    items = list(tracks(release.get("tracklist")))
    result = []
    for item in items:
        track_artist = credit(item.get("artists")) or artist
        # A compilation's Various Artists credit is never proof of track artist.
        if normal(track_artist) in {"various", "various artists", "unknown"} or not track_artist:
            continue
        duration = seconds(item.get("duration"))
        candidate, eligible = _rank(beatmap, {
            "title": str(item["title"]), "artist-credit": [{"name": track_artist}],
            "length": duration * 1000 if duration else None, "score": 100,
        })
        if not eligible:
            continue
        if (normal(item["title"]) in {"intro", "outro", "untitled", "interlude", "opening", "ending"}
                and not beatmap.album and not beatmap.discogs_release_id
                and not (beatmap.duration and duration and abs(beatmap.duration - duration) <= 4)):
            continue
        identifier = str(release.get("id") or "")
        if not identifier.isdigit() or not release.get("title"):
            continue
        date = release.get("released") or str(release.get("year") or "") or None
        identifiers = {"discogs_release": identifier}
        if release.get("master_id"):
            identifiers["discogs_master"] = str(release["master_id"])
        details = {
            **candidate.details, "source": "Discogs", "membership_verified": True,
            "album_source": "Discogs release", "source_links": {"discogs_release": f"https://www.discogs.com/release/{identifier}"},
            "recording": {"title": candidate.title, "artist": candidate.artist, "duration": duration},
            "release": {"title": release["title"], "artist": artist, "type": kind,
                        "date": date, "country": release.get("country"),
                        "status": "Unofficial" if "Unofficial Release" in descriptions else None,
                        "formats": descriptions, "track_count": len(items),
                        "track_number": item.get("position"), "id": identifier},
        }
        result.append(replace(candidate, album=str(release["title"]), album_artist=artist,
                              album_fallback=False, release_type=kind, release_date=date,
                              track_number=str(item.get("position") or "") or None,
                              track_count=len(items) or None, identifiers=identifiers, details=details))
    return result


def linked_ids(links):
    result = []
    for link in links:
        parsed = urlparse(str(link))
        if parsed.scheme != "https" or parsed.hostname not in {"discogs.com", "www.discogs.com"}:
            continue
        match = re.match(r"^/(release|master)/(\d+)(?:[-/]|$)", parsed.path)
        if match:
            result.append(("releases" if match[1] == "release" else "masters", match[2]))
    return sorted(set(result))


class DiscogsClient:
    def __init__(self, *, token=None, client=None, cache_get=None, cache_put=None):
        self._token = configured_token() if token is None else token
        self._client = client or httpx.AsyncClient(timeout=4, follow_redirects=False)
        self._owns_client = client is None
        self._get = cache_get or (lambda key: None)
        self._put = cache_put or (lambda key, value, seconds: None)
        self._lock = asyncio.Lock()
        self._last = None
        self._blocked_until = 0.0

    @property
    def search_enabled(self):
        return True

    @property
    def authenticated(self):
        return bool(self._token)

    async def _request(self, path, params=None):
        key = "discogs:v1:" + path + ":" + repr(sorted((params or {}).items()))
        cached = self._get(key)
        if cached is not None:
            return cached
        async with self._lock:
            if time.monotonic() < self._blocked_until:
                raise DiscogsError("Discogs is rate limited; retry later")
            interval = 1.1 if self._token else 2.5
            if self._last is not None:
                await asyncio.sleep(max(0, interval - (time.monotonic() - self._last)))
            self._last = time.monotonic()
            headers = {"User-Agent": f"osu-life-hacks/{__version__} (Hky Sigh)", "Accept": "application/json"}
            if self._token:
                headers["Authorization"] = "Discogs token=" + self._token
            try:
                response = await self._client.get(API_URL + "/" + path, params=params, headers=headers)
                if response.status_code == 429:
                    try:
                        delay = min(900, max(60, float(response.headers.get("Retry-After", "60"))))
                    except ValueError:
                        delay = 60
                    self._blocked_until = time.monotonic() + delay
                if response.status_code in {401, 403}:
                    self._blocked_until = time.monotonic() + 60
                    if path == "database/search":
                        raise DiscogsError("Discogs search requires authentication; configure a personal access token with discogs auth")
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError()
            except (httpx.HTTPError, ValueError) as error:
                # Never include headers, credentials, request objects or bodies.
                raise DiscogsError("Discogs lookup unavailable; MusicBrainz remains available") from None
            if response.headers.get("X-Discogs-Ratelimit-Remaining") == "0":
                self._blocked_until = time.monotonic() + 60
            self._put(key, data, 24 * 60 * 60)
            return data

    async def candidates(self, beatmap: Beatmap, *, links=(), search=True, fast=False, budget=None):
        deadline = time.monotonic() + (budget if budget is not None else (5.5 if fast else 18))
        async def request(path, params=None):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DiscogsError("Discogs candidate time budget reached")
            try:
                return await asyncio.wait_for(self._request(path, params), remaining)
            except TimeoutError:
                raise DiscogsError("Discogs candidate time budget reached") from None
        ids = linked_ids(links)
        if str(beatmap.discogs_release_id).isdigit():
            ids.insert(0, ("releases", str(beatmap.discogs_release_id)))
        warnings = []
        if search and self.search_enabled and (beatmap.artist or beatmap.artist_unicode) and (beatmap.title or beatmap.title_unicode):
            # The API's track parameter finds appearances on compilations as well
            # as artist releases. Search summaries themselves are not trusted.
            spellings = list(dict.fromkeys([
                (beatmap.artist_unicode or beatmap.artist, beatmap.title_unicode or beatmap.title),
                (beatmap.artist, beatmap.title)]))
            artist, title = spellings[0]
            # Reserve searches for EPs and compilations: duplicate single
            # pressings can otherwise fill an entire general-search page.
            queries = [(artist, title, None), (artist, title, "EP"), (artist, title, "Compilation")]
            queues = []
            for artist, title, format_hint in (queries[:1] if fast else queries):
                params = {"type": "release", "track": _versions(title)[0], "artist": primary(artist), "per_page": 20, "page": 1}
                if format_hint:
                    params["format"] = format_hint
                if beatmap.album and not format_hint:
                    params["release_title"] = beatmap.album
                try:
                    data = await request("database/search", params)
                except DiscogsError as error:
                    warnings.append(str(error))
                    break
                hits = sorted([hit for hit in data.get("results", []) if isinstance(hit, dict)],
                              key=lambda hit: (normal(hit.get("title")), str(hit.get("id"))))
                # Rotate formats to avoid spending the whole budget on duplicate
                # single pressings when an EP/compilation is in the same page.
                hits.sort(key=lambda hit: classify("", hit.get("format") or []) == "Single")
                queues.append([("releases", str(hit.get("id"))) for hit in hits if str(hit.get("id")).isdigit()])
                if not hits and not format_hint and len(spellings) > 1:
                    alternate_artist, alternate_title = spellings[1]
                    queries[1] = (alternate_artist, alternate_title, "EP")
                    queries[2] = (alternate_artist, alternate_title, "Compilation")
            for index in range(max((len(queue) for queue in queues), default=0)):
                ids.extend(queue[index] for queue in queues if index < len(queue))
        found = []
        seen = set()
        for kind, identifier in ids:
            if (kind, identifier) in seen:
                continue
            if len(seen) >= (2 if fast else 4):
                warnings.append("Discogs candidate budget reached")
                break
            seen.add((kind, identifier))
            try:
                data = await request(f"{kind}/{identifier}")
                if kind == "masters":
                    main = str(data.get("main_release") or "")
                    if not main.isdigit():
                        continue
                    data = await request("releases/" + main)
                found.extend(release_tracks(beatmap, data))
            except DiscogsError as error:
                warnings.append(str(error))
        return found, warnings

    async def aclose(self):
        if self._owns_client:
            await self._client.aclose()
