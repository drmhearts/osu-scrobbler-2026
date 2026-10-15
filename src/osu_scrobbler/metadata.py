"""Reconcile independent recording/release evidence before using album metadata."""

import asyncio
from dataclasses import replace

from .discogs import DiscogsClient
from .models import Beatmap, LookupResult, Track
from .musicbrainz import MusicBrainzClient, _rank
from .releases import reconcile, similarity


class MetadataClient:
    def __init__(self, cache_path=None, user_agent=None, *, musicbrainz=None, discogs=None, discogs_enabled=True):
        self.musicbrainz = musicbrainz or MusicBrainzClient(cache_path, **({"user_agent": user_agent} if user_agent else {}))
        self.discogs = (discogs or DiscogsClient(cache_get=self.musicbrainz._get, cache_put=self.musicbrainz._put)) if discogs_enabled else None
        self._flights = {}

    def set_correction(self, beatmap, track):
        self.musicbrainz.set_correction(beatmap, track)

    def clear_correction(self, beatmap):
        self.musicbrainz.clear_correction(beatmap)

    async def lookup(self, beatmap: Beatmap):
        correction = self.musicbrainz.get_correction(beatmap)
        if correction:
            return LookupResult(correction, [correction], "Persistent manual correction")
        mode = ("authenticated" if self.discogs.authenticated else "anonymous") if self.discogs else "off"
        key = "metadata:v1:" + mode + ":" + self.musicbrainz._lookup_key(beatmap)
        cached = self.musicbrainz._get(key)
        if cached:
            try:
                return LookupResult(Track.from_dict(cached["track"]) if cached.get("track") else None,
                                    [Track.from_dict(t) for t in cached.get("candidates", [])], cached["reason"])
            except (TypeError, ValueError, KeyError):
                pass
        if key not in self._flights:
            task = asyncio.create_task(self._lookup(beatmap, key))
            self._flights[key] = task
            task.add_done_callback(lambda done: self._flights.pop(key, None))
        result = await asyncio.shield(self._flights[key])
        # A correction saved while network requests were pending has precedence.
        correction = self.musicbrainz.get_correction(beatmap)
        return LookupResult(correction, [correction], "Persistent manual correction") if correction else result

    async def _lookup(self, beatmap, key):
        async def bounded(coro, seconds=20):
            try:
                return await asyncio.wait_for(coro, seconds)
            except Exception as error:
                return error

        tasks = [bounded(self.musicbrainz.lookup(beatmap))]
        if self.discogs:
            tasks.append(bounded(self.discogs.candidates(beatmap)))
        answers = await asyncio.gather(*tasks)
        warnings = []
        mb = answers[0]
        if isinstance(mb, Exception):
            warnings.append("MusicBrainz unavailable; release coverage is incomplete")
            mb = LookupResult(None, [], "MusicBrainz unavailable")
        releases = []
        candidates = list(mb.candidates)
        if mb.track:
            candidates.insert(0, mb.track)
            if not mb.track.album_fallback:
                releases.append(mb.track)
            releases.extend(Track.from_dict(value) for value in mb.track.details.get("release_candidates") or [])
            warnings.extend(mb.track.details.get("lookup_warnings") or [])
        if self.discogs:
            dc = answers[1]
            if isinstance(dc, Exception):
                warnings.append("Discogs unavailable; release coverage is incomplete")
            else:
                found, notes = dc
                releases.extend(found)
                candidates.extend(found)
                warnings.extend(notes)
            links = (mb.track.details.get("discogs_links") or []) if mb.track else []
            if links:
                linked = await bounded(self.discogs.candidates(beatmap, links=links, search=False), 8)
                if isinstance(linked, Exception):
                    warnings.append("Linked Discogs release lookup unavailable")
                else:
                    found, notes = linked
                    releases.extend(found)
                    candidates.extend(found)
                    warnings.extend(notes)
        if beatmap.recording_id or beatmap.isrc:
            if not mb.track:
                releases = [t for t in releases if t.details.get("source") == "MusicBrainz"]
            else:
                # A trusted recording tag outweighs contradictory free text.
                # Verify Discogs against that recording, not the stale title.
                canonical = Beatmap("canonical", mb.track.title, mb.track.artist, duration=mb.track.duration)
                releases = [t for t in releases if t.details.get("source") == "MusicBrainz" or _rank(canonical, {
                    "title": t.title, "artist-credit": [{"name": t.artist}], "score": 100,
                    "length": t.duration * 1000 if t.duration else None})[1]]
        selected, ranked = reconcile(beatmap, releases)
        # A release database cannot disambiguate two indistinguishable MB
        # recordings merely because it has an album with the same song title.
        if mb.track is None and "Ambiguous" in mb.reason:
            selected = None
        if selected and selected.details.get("source") == "Discogs" and mb.track:
            canonical = mb.track
            # Preserve a known MB recording identifier only with independent,
            # strong recording agreement. Never place a Discogs ID in mbid.
            if (similarity(selected.title, canonical.title) >= 0.98
                    and similarity(selected.artist, canonical.artist) >= 0.98
                    and selected.duration and canonical.duration
                    and abs(selected.duration - canonical.duration) <= 4):
                details = {**selected.details, "recording": canonical.details.get("recording") or {}}
                selected = replace(selected, recording_id=canonical.recording_id,
                                   identifiers={**selected.identifiers, "musicbrainz_recording": canonical.recording_id},
                                   details=details)
        track = selected or mb.track
        warnings = list(dict.fromkeys(warnings))
        if track:
            track = replace(track, details={**track.details, "lookup_warnings": warnings})
        if selected:
            reason = "Confident " + "/".join(selected.details.get("sources") or [selected.details.get("source", "metadata")]) + " release match"
        else:
            reason = mb.reason if mb.track or "Ambiguous" in mb.reason else "No confident release match; release type unknown"
        if warnings:
            reason += "; some source coverage is unavailable or bounded"
        # Keep different releases of the same recording selectable, while
        # deduplicating repeated source responses and selected recording rows.
        choices, seen = [], set()
        for candidate in ([track] if track else []) + ranked + candidates:
            identity = (candidate.identity, candidate.release_id, candidate.identifiers.get("discogs_release"))
            if identity not in seen:
                choices.append(candidate)
                seen.add(identity)
        result = LookupResult(track, choices[:15], reason)
        # Failures get a short retry window; never turn an outage into a durable negative.
        seconds = 300 if warnings else (7 * 86400 if track else 86400)
        self.musicbrainz._put(key, {"track": track.to_dict() if track else None,
                                   "candidates": [t.to_dict() for t in result.candidates], "reason": reason}, seconds)
        return result

    async def info(self, track):
        if track.details.get("source") == "Discogs":
            return track
        return await self.musicbrainz.info(track)

    async def aclose(self):
        tasks = list(self._flights.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.discogs:
            await self.discogs.aclose()
        await self.musicbrainz.aclose()

    async def close(self):
        await self.aclose()
