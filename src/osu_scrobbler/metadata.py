"""Staged, bounded release resolution across independent metadata sources."""

import asyncio
from dataclasses import replace

from .credits import primary, split_credit, cache_text
from .discogs import DiscogsClient
from .lastfm_metadata import LastfmMetadataClient
from .models import Beatmap, LookupResult, Track
from .musicbrainz import MusicBrainzClient, _rank
from .releases import reconcile, similarity, normal, MULTI_TYPES, score_candidate


class MetadataClient:
    fast_budget = 7.0
    deep_budget = 4.0
    def __init__(self, cache_path=None, user_agent=None, *, musicbrainz=None, discogs=None,
                 discogs_enabled=True, lastfm=None, lastfm_enabled=True, debug_sink=None):
        self.musicbrainz = musicbrainz or MusicBrainzClient(cache_path, **({"user_agent": user_agent} if user_agent else {}))
        self.discogs = (discogs or DiscogsClient(cache_get=self.musicbrainz._get, cache_put=self.musicbrainz._put)) if discogs_enabled else None
        self.lastfm = (lastfm or LastfmMetadataClient(cache_get=self.musicbrainz._get, cache_put=self.musicbrainz._put)) if lastfm_enabled and lastfm is not False else None
        self.debug_sink = debug_sink
        self._flights = {}

    def configure_lastfm_key(self, api_key):
        if self.lastfm:
            self.lastfm._api_key = api_key
            self.lastfm._blocked["api"] = 0.0

    def set_correction(self, beatmap, track):
        self.musicbrainz.set_correction(beatmap, track)

    def clear_correction(self, beatmap):
        self.musicbrainz.clear_correction(beatmap)

    @staticmethod
    def _context(beatmap, track):
        if not track:
            return None
        credit = split_credit(beatmap.artist_unicode or beatmap.artist)
        return replace(track, raw_artist=credit.raw, raw_title=beatmap.title_unicode or beatmap.title,
                       primary_artist=credit.primary, featured_artists=list(split_credit(track.artist).featured),
                       details={**track.details, "raw_metadata": {"artist": beatmap.artist, "title": beatmap.title,
                           "artist_unicode": beatmap.artist_unicode, "title_unicode": beatmap.title_unicode},
                           "raw_featured_artists": list(credit.featured)})

    def _diagnose(self, beatmap, result, statuses, *, cached=False):
        if not self.debug_sink:
            return
        def summary(track):
            return {"artist": track.artist, "title": track.title, "release": track.album,
                    "album_artist": track.album_artist, "release_type": track.release_type,
                    "provider": track.details.get("source"), "provider_ids": track.identifiers,
                    "recording_confidence": track.confidence,
                    "score": track.details.get("release_score", score_candidate(track, beatmap).score),
                    "eligible": bool(track.verified and track.details.get("membership_verified") and track.confidence >= 0.86),
                    "evidence": track.details.get("release_evidence"),
                    "ranking_rule": track.details.get("ranking_rule"), "sources": track.details.get("sources")}
        value = {"raw_artist": beatmap.artist, "raw_title": beatmap.title,
                 "raw_artist_unicode": beatmap.artist_unicode, "raw_title_unicode": beatmap.title_unicode,
                 "normalized_artist": normal(primary(beatmap.artist_unicode or beatmap.artist)),
                 "normalized_title": normal(beatmap.title_unicode or beatmap.title),
                 "providers": statuses, "candidates": [summary(t) for t in result.candidates],
                 "selected": summary(result.track or Track.fallback(beatmap)),
                 "fallback": result.track is None or result.track.album_fallback, "cached": cached,
                 "reason": result.reason}
        try:
            self.debug_sink(value)
        except (OSError, ValueError):
            pass  # Diagnostics never decide playback or delivery.

    async def lookup(self, beatmap: Beatmap):
        correction = self.musicbrainz.get_correction(beatmap)
        if correction:
            result = LookupResult(self._context(beatmap, correction), [correction], "Persistent manual correction")
            self._diagnose(beatmap, result, {"manual": "selected"})
            return result
        mode = ("authenticated" if self.discogs.authenticated else "anonymous") if self.discogs else "off"
        mode += ":" + ("api" if self.lastfm and self.lastfm.api_enabled else ("public" if self.lastfm else "off"))
        def cache_credit(value):
            credit = split_credit(value)
            return cache_text(credit.primary) + "|" + ",".join(cache_text(part) for part in credit.featured)
        context = replace(beatmap, artist=cache_credit(beatmap.artist), artist_unicode=cache_credit(beatmap.artist_unicode),
                          title=cache_text(beatmap.title), title_unicode=cache_text(beatmap.title_unicode))
        key = "metadata:v3:" + mode + ":" + self.musicbrainz._lookup_key(context)
        cached = self.musicbrainz._get(key)
        if cached:
            try:
                result = LookupResult(self._context(beatmap, Track.from_dict(cached["track"])) if cached.get("track") else None,
                                      [self._context(beatmap, Track.from_dict(t)) for t in cached.get("candidates", [])], cached["reason"])
                self._diagnose(beatmap, result, cached.get("providers", {}), cached=True)
                return result
            except (TypeError, ValueError, KeyError):
                pass
        if key not in self._flights:
            task = asyncio.create_task(self._lookup(beatmap, key))
            self._flights[key] = task
            task.add_done_callback(lambda done: self._flights.pop(key, None))
        result = await asyncio.shield(self._flights[key])
        correction = self.musicbrainz.get_correction(beatmap)
        if correction:
            return LookupResult(self._context(beatmap, correction), [correction], "Persistent manual correction")
        return LookupResult(self._context(beatmap, result.track), [self._context(beatmap, t) for t in result.candidates], result.reason)

    async def _lookup(self, beatmap, key):
        releases, candidates, warnings = [], [], []
        statuses = {}
        settled = False
        mb = LookupResult(None, [], "No confident recording or release")
        async def bounded(name, coro, timeout):
            try:
                return name, await asyncio.wait_for(coro, timeout)
            except Exception:
                return name, None
        tasks = {asyncio.create_task(bounded("MusicBrainz", self.musicbrainz.lookup(beatmap), self.fast_budget))}
        if self.discogs:
            tasks.add(asyncio.create_task(bounded("Discogs", self.discogs.candidates(beatmap, fast=True), min(6, self.fast_budget))))
        if self.lastfm:
            tasks.add(asyncio.create_task(bounded("Last.fm", self.lastfm.candidates(beatmap), min(4.5, self.fast_budget))))
        try:
            while tasks:
                done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    name, result = task.result()
                    statuses[name] = "complete" if result is not None else "unavailable or timed out"
                    if result is None:
                        warnings.append(name + " unavailable; release coverage incomplete")
                        continue
                    if name == "MusicBrainz":
                        mb = result
                        candidates.extend(mb.candidates)
                        if mb.track:
                            candidates.append(mb.track)
                            if not mb.track.album_fallback:
                                releases.append(mb.track)
                            releases.extend(Track.from_dict(t) for t in mb.track.details.get("release_candidates") or [])
                            warnings.extend(mb.track.details.get("lookup_warnings") or [])
                    else:
                        found, notes = result
                        releases.extend(found)
                        candidates.extend(found)
                        warnings.extend(notes)
                selected, ranked = reconcile(beatmap, releases)
                if (selected and selected.details.get("source") != "Last.fm"
                        and selected.release_type in MULTI_TYPES
                        and "MusicBrainz" in statuses
                        and (not (beatmap.recording_id or beatmap.isrc) or selected.details.get("source") == "MusicBrainz")
                        and selected.details.get("release_score", 0) >= 0.97
                        and not (mb.track is None and "Ambiguous" in mb.reason)):
                    if tasks and not settled:
                        # Let already-fast adapters finish their short request
                        # chains before cancelling genuinely slow providers.
                        settled = True
                        completed, _ = await asyncio.wait(tasks, timeout=0.25)
                        if completed:
                            continue  # Process their evidence before choosing.
                    for task in tasks:
                        task.cancel()
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                    for name in ("MusicBrainz", "Discogs", "Last.fm"):
                        if name not in statuses:
                            statuses[name] = "fast release resolved; slow work cancelled"
                    tasks = set()
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        selected, ranked = reconcile(beatmap, releases)
        # Last.fm-only results need the small featured-on list to compare release
        # relevance rather than trusting track.getInfo's single album choice.
        deeper = []
        needs_alternatives = not selected or selected.release_type not in MULTI_TYPES
        if self.lastfm and (needs_alternatives or selected.details.get("source") == "Last.fm"):
            deeper.append(bounded("Last.fm featured on", self.lastfm.candidates(beatmap, deep=True), self.deep_budget))
        if self.discogs:
            links = [link for candidate in candidates for link in candidate.details.get("discogs_links", [])]
            if needs_alternatives or links:
                deeper.append(bounded("Discogs alternatives", self.discogs.candidates(
                    beatmap, links=links, search=needs_alternatives, budget=max(0.001, self.deep_budget - 0.1)), self.deep_budget))
        for name, result in await asyncio.gather(*deeper):
            statuses[name] = "complete" if result is not None else "unavailable or timed out"
            if result is None:
                warnings.append(name + " unavailable; release coverage incomplete")
                continue
            found, notes = result
            releases.extend(found)
            candidates.extend(found)
            warnings.extend(notes)
        if beatmap.recording_id or beatmap.isrc:
            canonical = mb.track
            if canonical:
                context = Beatmap("canonical", canonical.title, canonical.artist, duration=canonical.duration)
                releases = [t for t in releases if t.details.get("source") == "MusicBrainz" or _rank(context, {
                    "title": t.title, "artist-credit": [{"name": t.artist}], "score": 100,
                    "length": t.duration * 1000 if t.duration else None})[1]]
            else:
                releases = [t for t in releases if t.details.get("source") == "MusicBrainz"]
        selected, ranked = reconcile(beatmap, releases)
        if mb.track is None and "Ambiguous" in mb.reason:
            # Cross-source agreement can support a release, but never invent an
            # MB recording ID for indistinguishable recordings.
            if not selected or len(selected.details.get("sources", [])) < 2:
                selected = None
        if selected and selected.details.get("source") != "MusicBrainz" and mb.track:
            canonical = mb.track
            if (similarity(selected.title, canonical.title) >= 0.98
                    and similarity(primary(selected.artist), primary(canonical.artist)) >= 0.98
                    and selected.duration and canonical.duration and abs(selected.duration - canonical.duration) <= 4):
                selected = replace(selected, recording_id=canonical.recording_id,
                                   identifiers={**selected.identifiers, "musicbrainz_recording": canonical.recording_id})
        # Only here, after fast providers and deeper fallback have finished, is a
        # title-as-album fallback allowed. Pending scrobbles omit unknown albums.
        track = selected
        if not track and mb.track:
            track = replace(mb.track, album=mb.track.title, album_artist="", album_fallback=True,
                            release_type="Unknown", release_id=None, identifiers={
                                k: v for k, v in mb.track.identifiers.items() if k in {"musicbrainz_recording", "isrc"}},
                            details={**mb.track.details, "release": {}, "membership_verified": False})
        warnings = list(dict.fromkeys(warnings))
        if track:
            track = self._context(beatmap, replace(track, details={**track.details, "lookup_warnings": warnings,
                      "provider_status": statuses, "resolution_stage": "resolved" if selected else "fallback"}))
        reason = ("Confident " + "/".join(selected.details.get("sources") or [selected.details.get("source", "metadata")]) + " release match") if selected else (
            mb.reason if "Ambiguous" in mb.reason else "All reasonable providers exhausted; release unknown")
        if warnings:
            reason += "; source coverage incomplete"
        choices, seen = [], set()
        for candidate in ([track] if track else []) + ranked + candidates:
            identity = (candidate.details.get("source"), candidate.identity, candidate.release_id,
                        candidate.identifiers.get("discogs_release"), candidate.identifiers.get("lastfm_release"))
            if identity not in seen:
                choices.append(self._context(beatmap, candidate))
                seen.add(identity)
        result = LookupResult(track, choices[:15], reason)
        self.musicbrainz._put(key, {"track": track.to_dict() if track else None,
                                   "candidates": [t.to_dict() for t in result.candidates], "reason": reason,
                                   "providers": statuses}, 300 if warnings else (7 * 86400 if selected else 3600))
        self._diagnose(beatmap, result, statuses)
        return result

    async def info(self, track):
        if track.details.get("membership_verified") or track.details.get("source") in {"Discogs", "Last.fm"}:
            return track
        return await self.musicbrainz.info(track)

    async def aclose(self):
        tasks = list(self._flights.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for source in (self.discogs, self.lastfm, self.musicbrainz):
            if source:
                await source.aclose()

    async def close(self):
        await self.aclose()
