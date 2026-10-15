"""The single owner of telemetry, selection, attempts, and durable delivery."""

import asyncio
from contextlib import aclosing
import json
import math
import os
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

from .config import Config, WatchLock
from .display import render
from .ipc import CommandServer
from .lastfm import CredentialStore, LastFmClient
from .models import Beatmap, Phase, Track
from .metadata import MetadataClient
from .diagnostics import ResolutionLog
from .playback import DuplicateSuppressed, PlaybackEngine
from .storage import Storage
from .telemetry import InternalAdapter, StateDecoder
from .welcome import welcome, commands
from .threshold import Threshold
from .terminal import threshold_prompt, enable_colors, style_message


def display_info(track: Track) -> dict:
    """Display absent recording/release fields honestly without changing metadata."""
    details = track.details
    recording = {key: None for key in ("duration", "genres", "tags", "isrcs", "disambiguation", "first_release_date")}
    recording.update(details.get("recording") or {})
    recording.setdefault("id", track.recording_id)
    recording.setdefault("title", track.title)
    recording.setdefault("artist", track.artist)
    release = {key: None for key in ("date", "country", "status", "type", "secondary_types", "genres", "tags", "labels", "release_group_id")}
    release.update(details.get("release") or {})
    release.update(id=track.release_id or release.get("id"), album=track.album, album_artist=track.album_artist,
                   album_source="track-title fallback" if track.album_fallback else details.get("album_source", "Manual metadata"))
    release.update(classification=track.release_type, track_number=track.track_number,
                   disc_number=track.disc_number, track_count=track.track_count)
    if track.release_date:
        release["date"] = track.release_date
    def unknown(value):
        if value is None or value == "" or value == []:
            return "unknown"
        if isinstance(value, dict):
            return {key: unknown(item) for key, item in value.items()}
        if isinstance(value, list):
            return [unknown(item) for item in value]
        return value
    return unknown({"selected_metadata": {"title": track.title, "artist": track.artist, "album": track.album,
                                           "album_artist": track.album_artist, "duration": track.duration,
                                           "release_type": track.release_type},
                    "recording": recording, "release": release,
                    "source": details.get("source", "MusicBrainz"), "identifiers": track.identifiers,
                    "sources": details.get("sources") or [], "lookup_warnings": details.get("lookup_warnings") or [],
                    "release_alternatives": details.get("release_alternatives") or [],
                    "source_links": details.get("source_links") or {"recording": None, "release": None}})


class Watcher:
    def __init__(self, directory: Path, config: Config, *, dry_run=False, fixture=False,
                 musicbrainz=None, lastfm=None, storage=None, output=print, json_output=False, keyboard=True):
        self.directory = directory
        self.config = config
        self.dry_run = dry_run
        self.fixture = fixture
        self.output = output
        self.json_output = json_output
        self.enable_keyboard = keyboard
        self.delivery_notices = {}
        self.threshold = Threshold.load(directory)
        self.engine = PlaybackEngine(mode=self.threshold.mode, seconds=self.threshold.seconds)
        self.musicbrainz = musicbrainz or MetadataClient(directory / "musicbrainz.db", config.musicbrainz_user_agent,
                                                       discogs_enabled=config.discogs_enabled,
                                                       lastfm_enabled=config.lastfm_metadata_enabled,
                                                       debug_sink=ResolutionLog(directory) if config.metadata_debug else None)
        self.storage = storage or Storage(directory / "history.db")
        self.lastfm = lastfm
        self.lookup_generation = 0
        self.candidates: list[Track] = []
        self.lookup_status = "No selection"
        self.delivery_status = "Dry run: no Last.fm writes" if dry_run else "Awaiting authorization"
        self.tasks: set[asyncio.Task] = set()
        self.stop = asyncio.Event()
        self.delivery_lock = asyncio.Lock()
        self.retry_after: dict[str, float] = {}
        self.delivery_success_clock: dict[str, float] = {}
        self.observed_monotonic = None
        self.observed_wall = None
        self.previewed = []
        self.saved_force_credits = None

    def say(self, value, *, full=False):
        message = render(value, full=full, json_output=self.json_output)
        if message:
            self.output(message if self.json_output else style_message(message))

    def _task(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        def finished(task):
            self.tasks.discard(task)
            if not task.cancelled() and task.exception() is not None:
                self.say("Background task failed; watcher stopping. Durable queue/history are preserved.")
                self.stop.set()
        task.add_done_callback(finished)
        return task

    def _persist_credits(self):
        if self.dry_run:
            return
        credits = self.engine.dump_force_credits()
        if credits != self.saved_force_credits:
            self.storage.set_state("force_credits", credits)
            self.saved_force_credits = credits

    def _bind(self, track: Track | None):
        if track:
            if not track.title.strip() or not track.artist.strip():
                track = None
            else:
                # Unknown/pending releases stay empty; an album artist is a
                # distinct release credit, never guessed from the track artist.
                track = replace(track, album_artist=track.album_artist or "")
        self.engine.bind_track(track)
        if track:
            last = self.storage.last_success(track.identity)
            if last is not None:
                self.engine.set_history(track.identity, last)
        self._persist_credits()

    async def ingest(self, telemetry, monotonic=None, wall_time=None, *, fixture_track=None):
        mono = time.monotonic() if monotonic is None else monotonic
        wall = time.time() if wall_time is None else wall_time
        self.observed_monotonic, self.observed_wall = mono, wall
        previous_key = self.engine.beatmap.selection_key if self.engine.beatmap else None
        previous_phase = self.engine.phase
        self.engine.update(telemetry, mono, wall)
        self._persist_credits()
        current_key = self.engine.beatmap.selection_key if self.engine.beatmap else None
        if previous_key != current_key:
            self.lookup_generation += 1
            self.candidates = []
            self._bind(None)
            if self.engine.beatmap:
                self.say({"selected": asdict(self.engine.beatmap)})
                self.lookup_status = "Looking up selection"
                if self.fixture:
                    self._bind(fixture_track or Track.fallback(self.engine.beatmap))
                    self.lookup_status = "Fixture metadata (no live MusicBrainz request)"
                else:
                    # Metadata enrichment must never hold up an eligible play.
                    self._bind(Track.pending(self.engine.beatmap))
                    self._task(self._lookup(self.engine.beatmap, self.lookup_generation))
        if previous_phase != self.engine.phase:
            if previous_phase == Phase.DISCONNECTED and self.engine.phase != Phase.DISCONNECTED:
                self.say({"connected": True})
            self.say({"phase": self.engine.phase.value})
        await self._automatic(mono, wall)

    async def _lookup(self, beatmap: Beatmap, generation: int):
        try:
            result = await self.musicbrainz.lookup(beatmap)
        except Exception:
            if generation == self.lookup_generation:
                self.lookup_status = "Metadata sources unavailable; using beatmap metadata for scrobbling"
                self._bind(Track.fallback(beatmap))
                if self.json_output:
                    self.say(self.lookup_status)
            return
        if generation != self.lookup_generation:
            return
        self.candidates = result.candidates
        self.lookup_status = (result.reason or "Identified") if result.track else "Using beatmap metadata; optional corrections are available"
        self._bind(result.track or Track.fallback(beatmap))
        self.say({"identification": self.lookup_status, "track": self.engine.track.to_dict() if self.engine.track else None,
                  "beatmap_fallback": result.track is None,
                  "candidates": [candidate.to_dict() for candidate in self.candidates]})
        # A late answer may still qualify the same ongoing attempt.
        # Metadata callbacks are not proof that gameplay continued after the
        # last telemetry sample. Do not extrapolate through a stalled socket.
        if self.observed_monotonic is not None:
            await self._automatic(self.observed_monotonic, self.observed_wall)

    async def _automatic(self, mono, wall):
        submission = self.engine.next_submission(mono, wall)
        if submission:
            await self._submit(submission)

    async def _submit(self, submission):
        self.engine.mark_pending(submission)
        exact = {"id": submission.id, "manual": submission.manual, "playback_start": submission.playback_start,
                 "metadata": submission.track.to_dict()}
        if self.dry_run:
            self.previewed.append(submission)
            self.say({"DRY RUN - would submit": exact})
        else:
            self.storage.enqueue(submission, state={"aliases:" + submission.id: self.engine.submission_aliases(submission),
                                                   "threshold:" + submission.id: self.engine.status()["required_seconds"]})
            self.say({"queued": exact, "offline": self.lastfm is None})

    async def deliver_once(self):
        if self.dry_run or self.lastfm is None:
            return
        async with self.delivery_lock:
            for submission in self.storage.pending():
                now = time.time()
                mono = time.monotonic()
                prior = self.storage.last_success(submission.track.identity)
                since = (mono - self.delivery_success_clock[submission.track.identity]
                         if submission.track.identity in self.delivery_success_clock
                         else now - prior if prior is not None else float("inf"))
                duration = submission.track.duration
                spacing = self.storage.get_state("threshold:" + submission.id,
                                                 duration / 2 if duration and math.isfinite(duration) and duration > 0 else 0) or 0
                if since < spacing or mono < self.retry_after.get(submission.id, 0):
                    continue
                self.storage.mark(submission.id, "sending")
                try:
                    result = await self.lastfm.submit(submission)
                except asyncio.CancelledError:
                    self.storage.mark(submission.id, "uncertain", "Watcher stopped during delivery; review before explicitly retrying")
                    raise
                except Exception:
                    # Unknown failures after dispatch cannot safely be redelivered automatically.
                    self.storage.mark(submission.id, "uncertain", "Unexpected failure during delivery; outcome unknown")
                    self.delivery_status = "Uncertain outcome; inspect queue before explicitly retrying"
                    self.say("Delivery outcome uncertain; inspect queue before retrying.")
                    continue
                accepted_at = time.time() if result.status == "accepted" else None
                if result.status == "accepted":
                    accepted_mono = time.monotonic()
                    self.delivery_success_clock[submission.track.identity] = accepted_mono
                    self.engine.mark_success(submission, accepted_at, monotonic=accepted_mono)
                    credits = self.engine.dump_force_credits()
                    self.storage.mark(submission.id, result.status, result.message, submitted_at=accepted_at,
                                      state={"force_credits": credits, "identity_history": self.engine.dump_history()})
                    self.saved_force_credits = credits
                else:
                    self.storage.mark(submission.id, result.status, result.message)
                    if result.status in {"ignored", "failed"}:
                        self.engine.mark_failed(submission)
                self.delivery_status = f"{result.status}: {result.message}"
                notice = (result.status, result.message)
                if self.json_output or self.delivery_notices.get(submission.id) != notice:
                    self.say({"delivery": result.status, "id": submission.id, "message": result.message,
                              "track": submission.track.to_dict()})
                self.delivery_notices[submission.id] = notice
                if result.status == "accepted":
                    if self.config.auto_export:
                        try:
                            count = self.storage.export(self.export_path())
                            self.say({"exported": count})
                        except Exception:
                            self.say("Export failed; accepted scrobble remains in history. Run export to try again.")
                elif result.status == "retry":
                    self.retry_after[submission.id] = time.monotonic() + 30

    async def _delivery_loop(self):
        while not self.stop.is_set():
            await self.deliver_once()
            try:
                await asyncio.wait_for(self.stop.wait(), 1)
            except asyncio.TimeoutError:
                pass

    def export_path(self):
        return Path(self.config.export_path).expanduser() if self.config.export_path else self.directory / "scrobbles.txt"

    def status(self):
        return {**self.engine.status(), "dry_run": self.dry_run, "lookup": self.lookup_status,
                "beatmap": asdict(self.engine.beatmap) if self.engine.beatmap else None,
                "track": self.engine.track.to_dict() if self.engine.track else None,
                "candidates": [track.to_dict() for track in self.candidates], "delivery": self.delivery_status,
                "queue": [{"id": row["id"], "status": row["status"], "message": row.get("message", "")}
                          for row in self.storage.history() if row["status"] != "accepted"]}

    async def command(self, request):
        cmd = request.get("command")
        if cmd == "threshold":
            if request.get("mode") is not None:
                try:
                    setting = Threshold(request["mode"], request.get("seconds", 30)).validate()
                    setting.save(self.directory)
                except (ValueError, TypeError):
                    return {"error": "Choose half, full, or positive seconds."}
                self.threshold = setting
                self.engine.mode, self.engine.seconds = setting.mode, setting.seconds
            return {"threshold": self.threshold.label}
        if cmd == "status":
            return self.status()
        if cmd == "force":
            if self.engine.phase == Phase.DISCONNECTED or not self.engine.beatmap:
                return {"error": "No connected selected beatmap"}
            prior_track = self.engine.track
            prior_lookup = self.lookup_status
            if prior_track is None:
                self._bind(Track.pending(self.engine.beatmap))
            try:
                observed_mono = self.observed_monotonic if self.observed_monotonic is not None else time.monotonic()
                submission = self.engine.next_submission(observed_mono, time.time(), manual=True)
            except DuplicateSuppressed as exc:
                return {"error": str(exc)}
            finally:
                if prior_track is None:
                    self._bind(None)
                    self.lookup_status = prior_lookup
            if submission is None:
                return {"error": "No selected track available"}
            await self._submit(submission)
            return {"dry_run": self.dry_run, "queued": not self.dry_run, "id": submission.id,
                    "exact_metadata": submission.track.to_dict(), "playback_start": submission.playback_start,
                    "custom_behavior": "Force can submit an unplayed selection; Last.fm listening criteria may not be met"}
        if cmd == "info":
            track = self.engine.track
            selected_key = self.engine.beatmap.selection_key if self.engine.beatmap else None
            if track is None:
                return {"error": "Identification unresolved; inspect status candidates or use config correct"}
            try:
                detailed = track if self.fixture else await self.musicbrainz.info(track)
            except Exception:
                return {"error": "Metadata details unavailable", "track": track.to_dict()}
            return {"info": display_info(detailed), "selection_key": selected_key}
        if cmd == "export":
            try:
                path = Path(request["destination"]).expanduser() if request.get("destination") else self.export_path()
                return {"exported": self.storage.export(path), "destination": str(path)}
            except Exception:
                return {"error": "Export failed; successful history is preserved; no scrobbles were resubmitted"}
        if cmd == "auth-reload":
            if self.dry_run:
                return {"dry_run": True}
            async with self.delivery_lock:
                try:
                    credentials = CredentialStore().load()
                    if not credentials or not credentials.session_key:
                        return {"error": "Last.fm authorization is missing"}
                    if self.lastfm:
                        await self.lastfm.aclose()
                    self.lastfm = LastFmClient(credentials)
                    configure = getattr(self.musicbrainz, "configure_lastfm_key", None)
                    if configure:
                        configure(credentials.api_key)
                    self.delivery_status = "Authorized"
                    return {"authorized": True}
                except Exception:
                    return {"error": "Secure credentials unavailable"}
        if cmd == "queue":
            return {"history": self.storage.history()}
        if cmd == "retry":
            if self.dry_run:
                return {"error": "Delivery retries are disabled in dry-run mode"}
            success = self.storage.retry(request.get("id", ""), include_uncertain=request.get("accept_duplicate_risk") is True)
            if success:
                row = self.storage.get(request["id"])
                from .models import Submission
                self.engine.restore_pending(Submission(row["id"], row["attempt_id"], Track.from_dict(row["track"]),
                                                       row["playback_start"], row["requested_at"], row["manual"]),
                                            aliases=self.storage.get_state("aliases:" + row["id"]))
            return {"requeued": success, "warning": "Uncertain retries may duplicate a previously accepted scrobble"}
        if cmd == "config":
            return await self._configure(request)
        if cmd == "quit":
            self.stop.set()
            return {"stopping": True}
        return {"error": "Unknown watcher command"}

    async def _configure(self, request):
        action = request.get("action", "show")
        if action == "show":
            return {"config": asdict(self.config)}
        if action == "set":
            key = request.get("key")
            if key not in asdict(self.config):
                return {"error": "Unknown configuration key"}
            candidate = Config(**{**asdict(self.config), key: request.get("value")})
            candidate.save(self.directory)
            self.config = candidate
            if key == "threshold_mode":
                self.threshold = Threshold()
                self.threshold.save(self.directory)
                self.engine.mode = "half"
            return {"saved": key, "value": getattr(candidate, key),
                    "restart_required": key in {"ipc_port", "musicbrainz_user_agent", "discogs_enabled", "lastfm_metadata_enabled", "metadata_debug"}}
        if not self.engine.beatmap:
            return {"error": "No selected beatmap"}
        if action == "choose":
            index = int(request.get("index", 0)) - 1
            if not 0 <= index < len(self.candidates):
                return {"error": "Candidate index out of range (use 1-based indexes)"}
            track = self.candidates[index]
        elif action == "correct":
            value = request.get("track", {})
            title, artist = value.get("title", "").strip(), value.get("artist", "").strip()
            if not title or not artist:
                return {"error": "Correction requires title and artist"}
            duration = value.get("duration", self.engine.beatmap.duration)
            if duration is not None and (isinstance(duration, bool) or not isinstance(duration, (int, float))
                                         or not math.isfinite(duration) or duration <= 0):
                return {"error": "Duration must be a positive finite number of seconds"}
            album = value.get("album") or ""
            track = Track(title, artist, album, value.get("album_artist") or "",
                          duration, value.get("recording_id"),
                          value.get("release_id"), False, 1, True,
                          {"source": "Manual correction"})
            kind = value.get("release_type") or "Unknown"
            if kind not in {"Album", "EP", "Compilation", "Single", "Soundtrack", "Mixtape", "Unknown"}:
                return {"error": "Unknown release type"}
            track.release_type = kind
        elif action == "clear-correction":
            self.musicbrainz.clear_correction(self.engine.beatmap)
            self.lookup_generation += 1
            self._bind(Track.pending(self.engine.beatmap))
            self._task(self._lookup(self.engine.beatmap, self.lookup_generation))
            return {"cleared": True}
        else:
            return {"error": "Unknown configuration action"}
        self.musicbrainz.set_correction(self.engine.beatmap, track)
        self.lookup_generation += 1  # Old in-flight result cannot replace a manual choice.
        self._bind(track)
        self.lookup_status = "Persistent manual correction"
        return {"saved_correction": track.to_dict()}

    async def run(self, fixture_path=None):
        with WatchLock(self.directory):
            if not self.dry_run:
                self.storage.recover_inflight()
            self.engine.restore_force_credits(self.storage.get_state("force_credits", []))
            for identity, accepted_at in self.storage.get_state("identity_history", {}).items():
                self.engine.set_history(identity, accepted_at)
            self.saved_force_credits = self.engine.dump_force_credits()
            for row in self.storage.history():
                if row["status"] in {"queued", "retry", "uncertain"}:
                    from .models import Submission
                    submission = Submission(row["id"], row["attempt_id"], Track.from_dict(row["track"]),
                                            row["playback_start"], row["requested_at"], row["manual"])
                    self.engine.restore_pending(submission, aliases=self.storage.get_state("aliases:" + row["id"]))
            saved_authorization = False
            if not self.dry_run and self.lastfm is None:
                try:
                    credentials = CredentialStore().load()
                    if credentials and credentials.session_key:
                        self.lastfm = LastFmClient(credentials)
                        self.delivery_status = "Authorized"
                        saved_authorization = True
                except Exception:
                    self.delivery_status = "Secure credentials unavailable; submissions will remain queued. Run auth."
            server = CommandServer(self.directory, self.config.ipc_port, self.command)
            await server.start()
            if self.enable_keyboard and not self.fixture and not self.json_output:
                enable_colors()
                self.output(welcome(self.directory))
            mode = "Timer: " + self.threshold.label
            self.say(f"osu! life hacks | {'DRY RUN — no writes' if self.dry_run else 'Live'} | {mode}")
            if self.enable_keyboard and (self.fixture or self.json_output):
                self.say(commands())
            if saved_authorization:
                self.say("Last.fm: saved authorization loaded automatically.")
            if not self.dry_run and self.lastfm is None:
                self.say("Last.fm not authorized; submissions stay queued. Run auth.")
            if not self.fixture:
                self.say("Waiting for osu!...")
            self._task(self._delivery_loop())
            if self.enable_keyboard:
                self._task(self._keyboard())
            adapter = StateDecoder() if fixture_path else InternalAdapter(output=self.say)
            if self.json_output:
                self.say(getattr(adapter, "compatibility_notice", "Internal osu! telemetry"))
            try:
                if fixture_path:
                    frames = json.loads(Path(fixture_path).read_text("utf-8"))
                    for frame in frames:
                        telemetry = adapter.parse(frame["payload"], companion=frame.get("tokens"))
                        track = Track.from_dict(frame["track"]) if frame.get("track") else None
                        await self.ingest(telemetry, float(frame["at"]), 1_700_000_000 + float(frame["at"]), fixture_track=track)
                    self.say({"fixture_complete": True, "dry_run_submissions": len(self.previewed)})
                else:
                    async def consume():
                        async with aclosing(adapter.stream()) as stream:
                            async for telemetry in stream:
                                await self.ingest(telemetry)
                    consumer = self._task(consume())
                    await self.stop.wait()
                    consumer.cancel()
            finally:
                self.stop.set()
                for task in list(self.tasks):
                    task.cancel()
                await asyncio.gather(*self.tasks, return_exceptions=True)
                await server.close()
                close = getattr(self.musicbrainz, "aclose", None)
                if close:
                    await close()
                if self.lastfm:
                    close = getattr(self.lastfm, "aclose", None)
                    if close:
                        await close()
                self.storage.close()

    async def _keyboard(self):
        if sys.stdin is None or not sys.stdin.isatty() or self.fixture:
            return
        previous = None
        try:
            if os.name != "nt":
                import termios
                import tty
                previous = termios.tcgetattr(sys.stdin.fileno())
                tty.setcbreak(sys.stdin.fileno())
            while not self.stop.is_set():
                key = ""
                if os.name == "nt":
                    import msvcrt
                    if msvcrt.kbhit():
                        key = msvcrt.getwch().lower()
                else:
                    import select
                    if select.select([sys.stdin], [], [], 0)[0]:
                        key = sys.stdin.read(1).lower()
                command = {"f": "force", "i": "info", "v": "info", "e": "export", "s": "status", "q": "quit"}.get(key)
                if command:
                    self.say(await self.command({"command": command}), full=key == "v")
                elif key == "a":
                    self.say(await self.command({"command": "config", "action": "set", "key": "auto_export",
                                                 "value": not self.config.auto_export}))
                elif key == "t":
                    async def local_sender(directory, request):
                        return await self.command(request)
                    if previous is not None:
                        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, previous)
                    try:
                        response = await threshold_prompt(self.directory, sender=local_sender, output=self.output)
                        if response:
                            self.say(response)
                    finally:
                        if previous is not None:
                            tty.setcbreak(sys.stdin.fileno())
                elif key in {"r", "d"}:
                    from .console import view_command
                    if previous is not None:
                        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, previous)
                    try:
                        response = await asyncio.to_thread(lambda: asyncio.run(view_command(self.directory, key)))
                        if response:
                            self.say(response)
                    finally:
                        if previous is not None:
                            tty.setcbreak(sys.stdin.fileno())
                elif key in "bu?x" and key:
                    from .console import view_command
                    if key == "x":
                        self.stop.set()
                    else:
                        response = await view_command(self.directory, key)
                        if response:
                            self.say(response)
                await asyncio.sleep(0.1)
        finally:
            if previous is not None:
                import termios
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, previous)
