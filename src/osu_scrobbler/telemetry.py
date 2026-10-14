"""Internal game-state normalization and retry-safe playback observations."""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import replace
from collections.abc import AsyncIterator
from typing import Any, Protocol


from .models import Beatmap, Phase, Telemetry

class TelemetryAdapter(Protocol):
    """Boundary for alternate read-only stable/lazer telemetry adapters."""

    def stream(self) -> AsyncIterator[Telemetry]: ...


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _id(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number > 0 and number.is_integer() else None


class StateDecoder:
    """Pure state machine; parse also supports historical offline fixtures."""

    def __init__(self):
        self._phase = Phase.DISCONNECTED
        self._selection = None
        self._position = None
        self._attempt = None
        self._attempt_counter = 0
        self._menu_context = False
        self._sample_monotonic = None
        self._state_number = None

    def disconnected(self) -> Telemetry:
        self._phase = Phase.DISCONNECTED
        # A transport loss is not proof of a fresh gameplay attempt. Preserve
        # the token and last position so reconnect cannot duplicate a scrobble.
        return Telemetry(
            None, Phase.DISCONNECTED, attempt_token=self._attempt,
            connected=False, interval_reliable=False,
        )

    def _beatmap(self, payload: dict) -> Beatmap | None:
        raw = _object(payload.get("beatmap"))
        title = _text(raw.get("title"))
        artist = _text(raw.get("artist"))
        title_unicode = _text(raw.get("titleUnicode"))
        artist_unicode = _text(raw.get("artistUnicode"))
        if not (title or title_unicode) or not (artist or artist_unicode):
            return None
        checksum = _text(raw.get("checksum"))
        difficulty = _text(raw.get("version"))
        map_id = _id(raw.get("id"))
        set_id = _id(raw.get("set"))
        selection = checksum or (
            f"{map_id or ''}\0{set_id or ''}\0{artist or artist_unicode}\0"
            f"{title or title_unicode}\0{difficulty}"
        )
        # Switching between clients must never continue the other client's timer.
        selection = f"{_text(payload.get('client')).casefold()}:{selection}"
        audio_ms = _number(_object(raw.get("time")).get("mp3Length"))
        duration = audio_ms / 1000 if audio_ms is not None and audio_ms > 0 else None
        last_object = _number(_object(raw.get("time")).get("lastObject"))
        return Beatmap(
            selection,
            title or title_unicode,
            artist or artist_unicode,
            title_unicode,
            artist_unicode,
            duration,
            map_id,
            set_id,
            difficulty,
            play_duration=last_object / 1000 if last_object is not None and last_object > 0 else None,
        )

    @staticmethod
    def _personal_play(payload: dict, companion: dict | None, position: float | None) -> bool:
        """Require same-map, near-synchronous source-backed positive evidence."""
        if "personal" in payload:
            return payload["personal"] is True
        if companion is None or companion.get("status") != 2 or companion.get("rawStatus") != 2:
            return False
        checksum = _text(_object(payload.get("beatmap")).get("checksum"))
        if not checksum or companion.get("md5") != checksum:
            return False
        companion_position = _number(companion.get("time"))  # /tokens is already seconds.
        if position is None or companion_position is None or abs(position - companion_position) > 1.0:
            return False
        profile = _object(payload.get("profile"))
        # Bancho watching is corroborating exclusion, never an account requirement.
        if _object(profile.get("banchoStatus")).get("number") == 6:
            return False
        local_name = _text(profile.get("name"))
        player_name = _text(_object(payload.get("play")).get("playerName"))
        if _text(payload.get("client")).casefold() == "lazer" and not (local_name and player_name):
            # The historical fixture source maps single-player spectating to play; its v2 feed does not
            # export the private spectating flag. Unknown identity cannot prove
            # personal gameplay (no osu! API key or account credentials needed).
            return False
        if local_name and player_name and local_name.casefold() != player_name.casefold():
            return False
        return True

    def parse(self, payload: dict, *, companion: dict | None = None, monotonic: float | None = None) -> Telemetry:
        """Translate a v2 snapshot, retaining selection independently of gameplay.

        Duration is audio length, never last hit-object time. Position detects
        fresh retries and confirms menu audio; elapsed listening is measured elsewhere
        with a monotonic clock. Without explicit safety evidence play is paused.
        """
        if not isinstance(payload, dict) or payload.get("error"):
            return self.disconnected()
        state = _object(payload.get("state"))
        number = _number(state.get("number"))
        if number is None or number == 3:
            return self.disconnected()
        beatmap = self._beatmap(payload)
        selection = beatmap.selection_key if beatmap is not None else None
        live = _number(_object(_object(payload.get("beatmap")).get("time")).get("live"))
        position = live / 1000 if live is not None else None
        phase = Phase.MENU
        interval_reliable = False
        loop_complete = False
        game_complete = False
        reported_position = position
        now = time.monotonic() if monotonic is None else monotonic
        elapsed = max(0.0, now - self._sample_monotonic) if self._sample_monotonic is not None else 0.0
        menu_context = number == 0  # Only the main menu counts as automatic listening.
        context_changed = menu_context != self._menu_context
        supported_client = _text(payload.get("client")).casefold() in {"stable", "lazer"}
        if number in {7, 14, 17, 18}:
            phase = Phase.RESULTS
            local_name = _text(_object(payload.get("profile")).get("name"))
            player_name = _text(_object(payload.get("play")).get("playerName"))
            same_player = not (local_name and player_name and local_name.casefold() != player_name.casefold())
            if (self._state_number == 2 and self._phase == Phase.PLAYING and self._attempt
                    and beatmap and selection == self._selection and beatmap.play_duration
                    and position is not None and position >= beatmap.play_duration - 0.25
                    and companion and ((companion.get("status") == 32 and companion.get("rawStatus") == number)
                                       or (companion.get("status") == 2 and companion.get("rawStatus") == 2))
                    and companion.get("md5") == _text(_object(payload.get("beatmap")).get("checksum"))
                    and _number(companion.get("time")) is not None and abs(position - companion["time"]) <= 1
                    and _object(payload.get("play")).get("failed") is False and same_player
                    and _object(_object(payload.get("profile")).get("banchoStatus")).get("number") != 6):
                phase = Phase.PLAYING  # Final gameplay interval, before the results state.
                game_complete = True
                interval_reliable = True
        elif number == 2:
            game = _object(payload.get("game"))
            play = _object(payload.get("play"))
            personal_play = self._personal_play(payload, companion, position)
            interval_reliable = bool(
                supported_client
                and beatmap is not None
                and isinstance(game.get("paused"), bool)
                and isinstance(play.get("failed"), bool)
                and position is not None
                and position >= 0
                and personal_play
            )
            if play.get("failed") is True:
                phase = Phase.FAILED
            elif companion is not None and companion.get("status") == 8:
                phase = Phase.MENU  # Watching is never a listening attempt.
                interval_reliable = False
            elif (
                not supported_client
                or beatmap is None
                or not isinstance(game.get("paused"), bool)
                or play.get("failed") is not False
                or position is None
                or position < 0
                or not personal_play
            ):
                phase = Phase.PAUSED  # Unknown evidence contributes no active time.
                interval_reliable = False
            else:
                phase = Phase.PAUSED if game["paused"] else Phase.PLAYING

        elif menu_context:
            # Menu selection alone proves no listening. Require the same song's
            # audio clock to advance normally; seeking and frozen clocks earn no time.
            companion_position = _number(companion.get("time")) if companion else None
            evidence = bool(
                supported_client and beatmap and position is not None and position >= 0
                and companion and companion.get("status") == 1 and companion.get("rawStatus") == number
                and companion.get("md5") == _text(_object(payload.get("beatmap")).get("checksum"))
                and companion_position is not None and abs(position - companion_position) <= 1.0
            )
            delta = position - self._position if position is not None and self._position is not None else 0.0
            advancing = bool(evidence and selection == self._selection and not context_changed
                             and 0 < delta <= max(1.0, elapsed * 2 + 0.25))
            phase = Phase.LISTENING if advancing else Phase.PAUSED if self._attempt and not context_changed else Phase.MENU
            interval_reliable = advancing
            if (evidence and selection == self._selection and self._phase == Phase.LISTENING
                    and self._attempt and beatmap.duration and self._position is not None
                    and 0 <= position <= 1 and self._position >= beatmap.duration - 1
                    and delta < 0 and 0 < beatmap.duration + delta <= max(1.0, elapsed * 2 + 0.25)):
                # Credit the final proven interval before resetting the next loop.
                # Otherwise a full-length timer would always miss the last tick.
                loop_complete = True
                phase = Phase.LISTENING
                interval_reliable = True
                reported_position = beatmap.duration

        selection_changed = selection != self._selection
        retry = (
            not loop_complete
            and
            self._attempt is not None
            and not selection_changed
            and phase in {Phase.PLAYING, Phase.LISTENING, Phase.PAUSED}
            and position is not None
            and self._position is not None
            and position < self._position - 0.25
        )
        if selection_changed or context_changed or phase not in {Phase.PLAYING, Phase.LISTENING, Phase.PAUSED}:
            self._attempt = None
        if phase == Phase.PLAYING and self._attempt is None and not retry and position > 1.0:
            # Attaching midway cannot establish the actual playback-start time.
            # Wait for an observed beginning/retry instead of inventing one.
            phase = Phase.PAUSED
            interval_reliable = False
        if phase in {Phase.PLAYING, Phase.LISTENING} and (self._attempt is None or retry):
            self._attempt_counter += 1
            self._attempt = f"osu:{self._attempt_counter}"
        elif retry:
            # A retry observed while paused begins counting only once it resumes.
            self._attempt = None
        self._selection = selection
        self._position = position
        self._phase = phase
        self._menu_context = menu_context
        self._sample_monotonic = now
        self._state_number = number
        event = Telemetry(beatmap, phase, reported_position, self._attempt, interval_reliable=interval_reliable,
                          completed=game_complete)
        if loop_complete:
            self._attempt = None
        return event



class InternalAdapter(StateDecoder):
    compatibility_notice = "Internal read-only Stable/lazer readers; unsupported layouts pause counting."

    def __init__(self, *, manager=None, output=lambda message: None, poll_interval=.1):
        super().__init__()
        from .readers.manager import ReaderManager
        self.manager = manager if manager is not None else ReaderManager()
        self.output = output
        self.poll_interval = poll_interval
        self._last_notice = None
        self._last_sample = None
        self._last_at = None
        self._process_identity = None

    def observe(self, sample, *, monotonic=None):
        sample.validate()
        now = time.monotonic() if monotonic is None else monotonic
        previous = self._last_sample
        same = previous and previous.client == sample.client and previous.beatmap['checksum'] == sample.beatmap['checksum']
        delta = sample.position_ms - previous.position_ms if same else 0
        elapsed = now - self._last_at if self._last_at is not None else 0
        advancing = same and 0 < delta <= max(1000, elapsed * 2000 + 250)
        # An initial <=1s observation establishes an attempt, but earns no time.
        start = sample.state == 2 and 0 <= sample.position_ms <= 1000 and (not same or delta < -250 or previous.state != 2)
        paused = not (advancing or start)
        payload = {'client': sample.client, 'state': {'number': sample.state},
                   'beatmap': sample.beatmap | {'time': sample.beatmap['time'] | {'live': sample.position_ms}},
                   'personal': sample.personal, 'game': {'paused': paused}, 'play': {'failed': sample.failed}}
        # A single coherent local sample replaces the two independent feeds.
        evidence = {'rawStatus': sample.state, 'status': 2 if sample.state == 2 and sample.personal else
                    32 if sample.state in {7, 14, 17, 18} else 1 if sample.state == 0 else 8,
                    'md5': sample.beatmap['checksum'], 'time': sample.position_ms / 1000}
        event = self.parse(payload, companion=evidence, monotonic=now)
        if (self._last_at is not None and elapsed > 2) or (sample.state == 2 and not advancing and not start):
            event = replace(event, interval_reliable=False, completed=False)
        self._last_sample, self._last_at = sample, now
        return event

    async def stream(self):
        from .readers.memory import ReaderUnavailable
        try:
            while True:
                try:
                    # Polling is bounded and synchronous: no orphan worker thread
                    # can continue using a process handle after cancellation.
                    sample = self.manager.read()
                    identity = getattr(self.manager, 'selected', None)
                    if self._process_identity is not None and identity != self._process_identity:
                        self._attempt = self._selection = self._position = None
                        self._last_sample = None
                        yield self.disconnected()
                    self._process_identity = identity
                    event = self.observe(sample)
                    self._last_notice = None
                    yield event
                    delay = self.poll_interval
                except (ReaderUnavailable, OSError, ValueError) as error:
                    message = str(error)
                    if message != self._last_notice:
                        self.output(message)
                        self._last_notice = message
                    self._last_sample = None
                    yield self.disconnected()
                    delay = 1.0
                await asyncio.sleep(delay)
        finally:
            self.manager.close()
