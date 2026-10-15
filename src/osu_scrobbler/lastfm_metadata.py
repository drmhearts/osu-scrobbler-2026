"""Read-only Last.fm release evidence, separate from scrobble delivery.

The API supplies track/album metadata; a bounded public-page fallback supplies
featured-on candidates. HTML is untrusted discovery data until membership checks.
"""

import asyncio
from dataclasses import dataclass
from html.parser import HTMLParser
import json
import math
import os
import re
import time
from urllib.parse import quote, unquote_plus, urlparse

import httpx

from . import __version__
from .credits import primary
from .lastfm import API_URL, CredentialStore, CredentialError
from .models import Beatmap, Track
from .musicbrainz import _rank, _versions
from .releases import normal


WEB_URL = "https://www.last.fm"


class LastfmMetadataError(RuntimeError):
    pass


def configured_api_key():
    value = os.environ.get("OSU_LIFE_HACKS_LASTFM_API_KEY", "").strip()
    if value:
        return value
    try:
        credentials = CredentialStore().load()
        return credentials.api_key if credentials else ""
    except CredentialError:
        return ""


def music_url(artist, *parts):
    return WEB_URL + "/music/" + "/".join(quote(str(part), safe="").replace("%20", "+") for part in (artist, *parts))


def music_parts(url):
    parsed = urlparse(str(url))
    try:
        port = parsed.port
    except ValueError:
        return []
    if parsed.scheme != "https" or parsed.hostname != "www.last.fm" or port not in {None, 443}:
        return []
    parts = [unquote_plus(part) for part in parsed.path.split("/") if part]
    return parts[1:] if parts and parts[0] == "music" else []


def number(value):
    try:
        result = float(str(value).replace(",", ""))
        return max(0, int(result)) if math.isfinite(result) else 0
    except (ValueError, TypeError, OverflowError):
        return 0


def popularity(text):
    match = re.search(r"([\d,.]+)\s*([kKmM]?)\s+listeners", text)
    if not match:
        return 0
    return number(float(match[1].replace(",", "")) * {"k": 1000, "m": 1000000}.get(match[2].lower(), 1))


class Node:
    def __init__(self, tag="", attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self):
        return " ".join(child.text() if isinstance(child, Node) else child for child in self.children
                        if not isinstance(child, Node) or child.tag not in {"script", "style"}).strip()

    def has_class(self, name):
        return name in self.attrs.get("class", "").split()


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.current = self.root
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.current.children.append(Node(tag, attrs, self.current))

    def handle_endtag(self, tag):
        node = self.current
        while node.parent is not None:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


@dataclass(frozen=True)
class AlbumRef:
    artist: str
    title: str
    url: str
    listeners: int = 0


def featured_on(html, beatmap):
    """Only links inside Featured On, on a matching canonical track page."""
    nodes = list(Page(html).root.walk())
    canonical = next((n.attrs.get("href") for n in nodes if n.tag == "link" and n.attrs.get("rel") == "canonical"), "")
    parts = music_parts(canonical)
    if len(parts) != 3:
        return []
    _, safe = _rank(beatmap, {"title": parts[-1], "artist-credit": [{"name": parts[0]}], "score": 100})
    if not safe:
        return []
    heading = next((n.text() for n in nodes if n.tag == "h1" and n.has_class("header-new-title")), "")
    if heading and normal(heading) != normal(parts[-1]):
        return []
    active = False
    result = {}
    for node in nodes:
        ancestor = node.parent
        card_heading = False
        while ancestor:
            if ancestor.tag in {"li", "article"} or ancestor.has_class("resource-list--release-list-item"):
                card_heading = True
                break
            ancestor = ancestor.parent
        if node.tag in {"h1", "h2", "h3"} and node.text() and not card_heading:
            active = normal(node.text()) == "featured on"
        if not active or node.tag != "a":
            continue
        url = node.attrs.get("href", "")
        if url.startswith("/music/"):
            url = WEB_URL + url
        release = music_parts(url)
        if len(release) != 2 or release[1].startswith(("_", "+")):
            continue
        block = node.parent
        while block and block.parent and block.tag not in {"li", "article"} and not any(
                block.has_class(name) for name in ("resource-list--release-list-item", "catalogue-metadata-item")):
            if block.tag == "section":
                break
            block = block.parent
        listeners = popularity(block.text()) if block else 0
        ref = AlbumRef(release[0], release[1], music_url(*release), listeners)
        if ref.url not in result or ref.listeners > result[ref.url].listeners:
            result[ref.url] = ref
    return sorted(result.values(), key=lambda ref: (-ref.listeners, normal(ref.title), ref.url))[:6]


def html_album(html, ref):
    """Fail closed if a public album page cannot establish its own identity."""
    nodes = list(Page(html).root.walk())
    canonical = next((n.attrs.get("href") for n in nodes if n.tag == "link" and n.attrs.get("rel") == "canonical"), "")
    parts = music_parts(canonical)
    if len(parts) != 2 or normal(parts[0]) != normal(ref.artist) or normal(parts[1]) != normal(ref.title):
        return None
    for node in nodes:
        if node.tag != "script" or node.attrs.get("type") != "application/ld+json":
            continue
        try:
            document = json.loads(node.text())
        except ValueError:
            continue
        objects = document if isinstance(document, list) else [document]
        objects += [item for obj in list(objects) if isinstance(obj, dict) for item in obj.get("@graph") or []]
        for obj in objects:
            if not isinstance(obj, dict) or obj.get("@type") != "MusicAlbum" or normal(obj.get("name")) != normal(ref.title):
                continue
            artist = obj.get("byArtist") or {}
            if not isinstance(artist, dict) or normal(artist.get("name")) != normal(ref.artist):
                continue
            entries = obj.get("track") or []
            if isinstance(entries, dict):
                entries = entries.get("itemListElement") or []
            if not isinstance(entries, list):
                continue
            items = []
            for index, entry in enumerate(entries[:200], 1):
                if not isinstance(entry, dict):
                    continue
                entry = entry.get("item") or entry
                if not isinstance(entry, dict):
                    continue
                credited = entry.get("byArtist") or {}
                if entry.get("name") and isinstance(credited, dict) and credited.get("name"):
                    items.append({"name": entry["name"], "artist": {"name": credited["name"]}, "@attr": {"rank": index}})
            if items:
                return {"name": ref.title, "artist": ref.artist, "url": ref.url,
                        "listeners": ref.listeners, "tracks": {"track": items}, "_evidence": "featured-on+public-tracklist"}
    items = []
    for row in [n for n in nodes if n.tag == "tr" and n.has_class("chartlist-row")]:
        descendants = list(row.walk())
        data = {key: value for n in descendants for key, value in n.attrs.items() if key.startswith("data-")}
        name_node = next((n for n in descendants if n.has_class("chartlist-name")), None)
        artist_node = next((n for n in descendants if n.has_class("chartlist-artist")), None)
        title = data.get("data-track-name") or (name_node.text() if name_node else "")
        artist = data.get("data-artist-name") or (artist_node.text() if artist_node else "")
        if not artist:
            urls = [music_parts(WEB_URL + n.attrs["href"] if n.attrs.get("href", "").startswith("/") else n.attrs.get("href", ""))
                    for n in descendants if n.tag == "a"]
            artist = next((p[0] for p in urls if len(p) == 3 and normal(p[-1]) == normal(title)), "")
        index = next((n.text() for n in descendants if n.has_class("chartlist-index")), "")
        duration = next((n.text() for n in descendants if n.has_class("chartlist-duration")), "")
        length = None
        if re.fullmatch(r"\d+:\d{2}", duration.strip()):
            minutes, seconds = duration.strip().split(":")
            length = int(minutes) * 60 + int(seconds)
        if title:
            items.append({"name": title, "artist": {"name": artist}, "duration": length, "@attr": {"rank": index}})
    return {"name": parts[1], "artist": parts[0], "url": music_url(*parts), "listeners": ref.listeners,
            "tracks": {"track": items}, "_evidence": "featured-on+public-tracklist"}


def release_candidates(beatmap, album):
    if not isinstance(album, dict) or not album.get("name") or not isinstance(album.get("artist"), str):
        return []
    album_artist = album["artist"]
    container = album.get("tracks") or {}
    if not isinstance(container, dict):
        return []
    items = container.get("track") or []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return []
    found = []
    for item in items[:200]:
        if not isinstance(item, dict):
            continue
        artist = item.get("artist") or {}
        artist = artist.get("name", "") if isinstance(artist, dict) else str(artist)
        # Missing compilation track credits cannot be inferred from popularity.
        if not artist:
            continue
        try:
            duration = float(item.get("duration") or 0)
            duration = duration if math.isfinite(duration) and duration > 0 else None
        except (ValueError, TypeError):
            duration = None
        recording = {"title": item.get("name"), "artist-credit": [{"name": artist}], "score": 100,
                     "length": duration * 1000 if duration else None}
        candidate, safe = _rank(beatmap, recording)
        if not safe:
            continue
        url = album.get("url") or music_url(album_artist, album["name"])
        if isinstance(url, str) and url.startswith("http://www.last.fm/"):
            url = "https://" + url[len("http://"):]
        if len(music_parts(url)) != 2:
            continue
        url = music_url(*music_parts(url))
        ids = {"lastfm_release": url}
        if album.get("mbid"):
            ids["lastfm_album_mbid"] = str(album["mbid"])
        confidence = candidate.confidence
        # API tracklist evidence is stronger than a public-page fallback.
        strength = 0.98 if album.get("_evidence") != "featured-on+public-tracklist" else 0.93
        found.append(Track(candidate.title, candidate.artist, str(album["name"]), album_artist,
                           duration, confidence=confidence, verified=True, track_count=len(items) or None,
                           track_number=str((item.get("@attr") or {}).get("rank") or "") or None,
                           identifiers=ids, details={**candidate.details, "source": "Last.fm",
                           "membership_verified": True, "membership_strength": strength,
                           "album_source": "Last.fm release", "listeners": number(album.get("listeners")),
                           "playcount": number(album.get("playcount")), "source_links": {"release": url},
                           "discovery": album.get("_evidence", "track.getInfo+album.getInfo"),
                           "release": {"title": album["name"], "artist": album_artist, "type": None,
                                       "track_count": len(items), "id": url},
                           "recording": {"title": candidate.title, "artist": candidate.artist, "duration": duration}}))
    return found


class LastfmMetadataClient:
    def __init__(self, *, api_key=None, client=None, cache_get=None, cache_put=None):
        self._api_key = configured_api_key() if api_key is None else api_key
        self._client = client or httpx.AsyncClient(timeout=2, follow_redirects=False)
        self._owns_client = client is None
        self._get = cache_get or (lambda key: None)
        self._put = cache_put or (lambda key, value, seconds: None)
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._blocked = {"api": 0.0, "web": 0.0}

    @property
    def api_enabled(self):
        return bool(self._api_key)

    async def _request(self, *, method=None, params=None, url=None):
        kind = "api" if method else "web"
        key = "lastfm:v1:" + (method + json.dumps(params, sort_keys=True, ensure_ascii=False) if method else url)
        cached = self._get(key)
        if cached is not None:
            return cached
        if time.monotonic() < self._blocked[kind]:
            raise LastfmMetadataError("Last.fm " + kind + " metadata temporarily unavailable")
        if url and not music_parts(url):
            raise LastfmMetadataError("Invalid Last.fm music URL")
        async with self._lock:
            await asyncio.sleep(max(0, 0.25 - (time.monotonic() - self._last)))
            self._last = time.monotonic()
        try:
            response = await self._client.get(API_URL if method else url,
                params={"method": method, "api_key": self._api_key, "format": "json", "autocorrect": "0", **(params or {})} if method else None,
                headers={"User-Agent": f"osu-life-hacks/{__version__} (Hky Sigh)", "Accept-Language": "en"}, timeout=2)
            if response.status_code in {401, 403, 406, 429}:
                self._blocked[kind] = time.monotonic() + 60
            response.raise_for_status()
            if len(response.content) > 1500000:
                raise ValueError()
            data = response.json() if method else {"html": response.text}
            if not isinstance(data, dict):
                raise ValueError()
            if data.get("error"):
                if number(data["error"]) in {10, 26, 29}:
                    self._blocked[kind] = time.monotonic() + 60
                if number(data["error"]) == 6:
                    return {}
                raise ValueError()
        except (httpx.HTTPError, ValueError):
            raise LastfmMetadataError("Last.fm " + kind + " metadata unavailable") from None
        self._put(key, data, 7 * 86400)
        return data

    async def candidates(self, beatmap: Beatmap, *, deep=False):
        artist = primary(beatmap.artist_unicode or beatmap.artist)
        title = beatmap.title_unicode or beatmap.title
        if not artist or not title:
            return [], []
        found, notes = [], []
        if not deep and self.api_enabled:
            try:
                data = await self._request(method="track.getInfo", params={"artist": artist, "track": title})
                info = data.get("track") or {}
                if not isinstance(info, dict):
                    raise LastfmMetadataError("Last.fm returned invalid track metadata")
                actual_artist = info.get("artist") or {}
                actual_artist = actual_artist.get("name", "") if isinstance(actual_artist, dict) else actual_artist
                _, safe = _rank(beatmap, {"title": info.get("name"), "artist-credit": [{"name": actual_artist}],
                                        "length": info.get("duration"), "score": 100})
                release = info.get("album") or {}
                if not isinstance(release, dict):
                    return [], []
                if safe and release.get("title") and release.get("artist"):
                    data = await self._request(method="album.getInfo", params={"artist": release["artist"], "album": release["title"]})
                    album = data.get("album") or {}
                    if (isinstance(album, dict) and normal(album.get("name")) == normal(release["title"])
                            and normal(album.get("artist")) == normal(release["artist"])):
                        found.extend(release_candidates(beatmap, album))
            except LastfmMetadataError as error:
                notes.append(str(error))
            return found, notes
        if not deep:
            return [], []  # Public pages are stage 3, never a hidden fast-path delay.
        deadline = time.monotonic() + 3.7
        try:
            url = music_url(artist, "_", _versions(title)[0])
            data = await self._request(url=url)
            refs = featured_on(data["html"], beatmap)
        except LastfmMetadataError as error:
            return [], [str(error)]
        # Strong artist relationships first; popularity never outranks that gate.
        refs.sort(key=lambda ref: (normal(primary(ref.artist)) != normal(artist), -ref.listeners, normal(ref.title), ref.url))
        async def verify(ref):
            try:
                if self.api_enabled:
                    data = await self._request(method="album.getInfo", params={"artist": ref.artist, "album": ref.title})
                    album = data.get("album")
                    if not isinstance(album, dict) or normal(album.get("name")) != normal(ref.title) or normal(album.get("artist")) != normal(ref.artist):
                        return [], ["Last.fm album response did not match the requested release"]
                    if not number(album.get("listeners")) and ref.listeners:
                        album = {**album, "listeners": ref.listeners}
                else:
                    data = await self._request(url=ref.url)
                    album = html_album(data["html"], ref)
                return release_candidates(beatmap, album), []
            except LastfmMetadataError as error:
                return [], [str(error)]
        tasks = {asyncio.create_task(verify(ref)) for ref in refs[:3]}
        try:
            while tasks:
                done, tasks = await asyncio.wait(tasks, timeout=max(0, deadline - time.monotonic()),
                                                 return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    candidates, warnings = task.result()
                    found.extend(candidates)
                    notes.extend(warnings)
                if not done:
                    notes.append("Last.fm featured-on candidate budget reached")
                    break
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        return found, notes

    async def aclose(self):
        if self._owns_client:
            await self._client.aclose()
