"""Gameplay state and scrobble eligibility, independent of adapters and delivery.

Position is used only to notice restarts. Listening time comes from monotonic
clock intervals, and a delivery retry retains the original Submission object.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
import uuid

from .models import Beatmap, Phase, Submission, Telemetry, Track


DUPLICATE_MESSAGE = (
    "Already scrobbled. Retry the map and play at least 29 seconds to scrobble again."
)
ACTIVE_PHASES = {Phase.PLAYING, Phase.LISTENING}


class DuplicateSuppressed(ValueError):
    """A requested force would duplicate a successful play or force."""


@dataclass
class _Attempt:
    id: str
    serial: int
    selection_key: str
    started_at: float
    active_seconds: float = 0.0
    token: str | None = None
    suppressed: bool = False
    claim: str | None = None


@dataclass
class _Issued:
    submission: Submission
    aliases: set[str]
    attempt: _Attempt | None
    attempt_serial: int
    status: str = "pending"
    next_attempt: _Attempt | None = None


class PlaybackEngine:
    """Track a selection episode, a gameplay attempt and a recording separately.

    ``update`` must receive clocks sampled together by the caller. Unknown
    connection gaps are excluded; reconnecting directly to gameplay conserves
    the old attempt until a visible new attempt boundary appears. ``bind_track``
    may run later than ``update`` without resetting elapsed gameplay.

    Returning a submission reserves its attempt immediately. Callers enqueue or
    deliver that same object, then report acceptance with ``mark_success``.
    HTTP success alone is not acceptance. A definitive ignored/failed response
    consumes the attempt; retries of delivery should reuse its submission ID.
    """

    def __init__(self, mode: str = "custom", threshold: float = 29.0) -> None:
        if mode not in {"custom", "standard", "lastfm"}:
            raise ValueError("Threshold mode must be custom or standard")
        if not math.isfinite(threshold) or threshold < 29:
            raise ValueError("Custom gameplay threshold must be at least 29 seconds")
        self.mode = "standard" if mode == "lastfm" else mode
        self.threshold = float(threshold)
        self.beatmap: Beatmap | None = None
        self.track: Track | None = None
        self.phase = Phase.DISCONNECTED
        self.selection_episode = uuid.uuid4().hex
        self._attempt: _Attempt | None = None
        self._attempt_serial = 0
        self._position: float | None = None
        self._last_monotonic: float | None = None
        self._last_wall_time: float | None = None
        self._issued: dict[str, _Issued] = {}
        self._selection_claim: str | None = None
        self._last_success: dict[str, float] = {}
        self._success_monotonic: dict[str, float] = {}
        self._force_credits: list[tuple[set[str], int]] = []

    @property
    def attempt_id(self) -> str | None:
        return self._attempt.id if self._attempt else None

    @property
    def active_seconds(self) -> float:
        return self._attempt.active_seconds if self._attempt else 0.0

    @property
    def playback_start(self) -> float | None:
        return self._attempt.started_at if self._attempt else None

    @property
    def suppressed(self) -> bool:
        return bool(self._attempt and self._attempt.suppressed)

    def _aliases(self, track: Track | None = None) -> set[str]:
        aliases: set[str] = set()
        if self.beatmap:
            aliases.add(self.beatmap.track_key)
            aliases.add(Track.fallback(self.beatmap).identity)
        current = track if track is not None else self.track
        if current:
            aliases.add(current.identity)
        return aliases

    def bind_track(self, track: Track | None) -> None:
        """Bind metadata for the current selection; preserve its gameplay timer."""
        self.track = track
        self._attach_pending_force_target()
        self._apply_force_credit()

    def set_history(self, identity: str, wall_time: float) -> None:
        """Load an accepted recording's durable timestamp, without secrets."""
        if not math.isfinite(wall_time):
            raise ValueError("History timestamp must be finite")
        self._last_success[identity] = max(
            wall_time, self._last_success.get(identity, float("-inf"))
        )

    def dump_history(self) -> dict[str, float]:
        """Persist accepted recording identities and their beatmap aliases."""
        return dict(self._last_success)

    def restore_history(self, history: dict[str, float]) -> None:
        """Restore accepted aliases; elapsed time uses wall time after restart."""
        for identity, wall_time in history.items():
            self.set_history(identity, wall_time)

    def dump_force_credits(self) -> list[list[str]]:
        """Serialize unused successful-force suppression for durable storage."""
        return [sorted(aliases) for aliases, _after_serial in self._force_credits]

    def restore_force_credits(self, credits: list[list[str]]) -> None:
        """Restore a saved credit; it is consumed by exactly one future attempt."""
        restored = []
        for aliases in credits:
            if not isinstance(aliases, list) or not all(isinstance(item, str) for item in aliases):
                raise ValueError("Force suppression credits must contain identity lists")
            if aliases:
                restored.append((set(aliases), self._attempt_serial))
        self._force_credits = restored

    def submission_aliases(self, submission: Submission) -> list[str]:
        """Snapshot aliases with the queued ID, including unresolved map names."""
        issued = self._issued.get(submission.id)
        if issued:
            return sorted(issued.aliases)
        track = submission.track
        return sorted({
            track.identity,
            f"{track.artist.casefold().strip()}\0{track.title.casefold().strip()}",
        })

    def restore_pending(self, submission: Submission, aliases: list[str] | None = None) -> None:
        """Restore queued or uncertain delivery without creating a new scrobble.

        Restore before telemetry starts so a pending manual force reserves its
        first future gameplay attempt even across a watcher restart.
        """
        if submission.id in self._issued:
            self._issued[submission.id].aliases.update(aliases or [])
            self.mark_pending(submission)
            self._attach_pending_force_target()
            return
        self._issued[submission.id] = _Issued(
            submission,
            set(self.submission_aliases(submission)) | set(aliases or []),
            None,
            self._attempt_serial,
        )

    def _apply_force_credit(self) -> None:
        if not self._attempt or self._attempt.suppressed:
            return
        aliases = self._aliases()
        for index, (credit, after_serial) in enumerate(self._force_credits):
            if self._attempt.serial > after_serial and aliases & credit:
                self._attempt.suppressed = True
                del self._force_credits[index]
                return

    def _start_attempt(self, telemetry: Telemetry, wall_time: float) -> None:
        assert self.beatmap is not None
        self._attempt_serial += 1
        self._attempt = _Attempt(
            uuid.uuid4().hex,
            self._attempt_serial,
            self.beatmap.selection_key,
            wall_time,
            token=telemetry.attempt_token,
        )
        # Reserve the first future attempt while a force is still queued. Its
        # later acceptance suppresses this exact attempt, not every later play.
        self._attach_pending_force_target()
        self._apply_force_credit()

    def _attach_pending_force_target(self) -> None:
        if self._attempt is None:
            return
        aliases = self._aliases()
        for issued in self._issued.values():
            if (
                issued.submission.manual
                and issued.status == "pending"
                and issued.next_attempt is None
                and self._attempt.serial > issued.attempt_serial
                and aliases & issued.aliases
            ):
                issued.next_attempt = self._attempt

    def _advance(self, monotonic: float) -> None:
        if (
            self._last_monotonic is not None
            and self.phase in ACTIVE_PHASES
            and self._attempt is not None
        ):
            self._attempt.active_seconds += max(0.0, monotonic - self._last_monotonic)
        self._last_monotonic = monotonic

    def update(self, telemetry: Telemetry, monotonic: float, wall_time: float) -> None:
        if not math.isfinite(monotonic) or not math.isfinite(wall_time):
            raise ValueError("Clock readings must be finite")
        incoming_phase = telemetry.phase if telemetry.connected else Phase.DISCONNECTED
        previous_phase = self.phase
        connected = incoming_phase != Phase.DISCONNECTED
        selection_changed = False
        if connected:
            old_key = self.beatmap.selection_key if self.beatmap else None
            new_key = telemetry.beatmap.selection_key if telemetry.beatmap else None
            selection_changed = old_key != new_key
            self.beatmap = telemetry.beatmap
            if selection_changed:
                self.selection_episode = uuid.uuid4().hex
                self.track = None
                self._attempt = None
                self._position = None
                self._selection_claim = None

        fresh = connected and incoming_phase in ACTIVE_PHASES and self.beatmap is not None
        if fresh:
            token_changed = bool(
                self._attempt
                and telemetry.attempt_token is not None
                and self._attempt.token is not None
                and telemetry.attempt_token != self._attempt.token
            )
            rewound = bool(
                previous_phase in ACTIVE_PHASES
                and self._position is not None
                and telemetry.position is not None
                and telemetry.position < self._position - 1.0
            )
            fresh = bool(
                self._attempt is None
                or selection_changed
                or previous_phase in {Phase.MENU, Phase.RESULTS, Phase.FAILED}
                or token_changed
                or rewound
            )

        # A disconnect makes the interval since the last sample unknowable. Do
        # not credit that interval, or the interval returning from disconnect.
        if (
            connected
            and previous_phase != Phase.DISCONNECTED
            and not fresh
            and getattr(telemetry, "interval_reliable", True)
        ):
            self._advance(monotonic)
        else:
            self._last_monotonic = monotonic
        if fresh:
            self._start_attempt(telemetry, wall_time)
        elif self._attempt and telemetry.attempt_token is not None and connected:
            self._attempt.token = telemetry.attempt_token
        self.phase = incoming_phase
        if connected and incoming_phase in ACTIVE_PHASES:
            self._position = telemetry.position
        self._last_wall_time = wall_time

    def _required_seconds(self) -> float | None:
        if self.mode == "custom":
            return self.threshold
        duration = self.track.duration if self.track else None
        if duration is None and self.beatmap:
            duration = self.beatmap.duration
        if duration is None or not math.isfinite(duration) or duration <= 30:
            return None
        # Last.fm: more than 30 seconds long, played halfway or four minutes,
        # whichever comes first. This application's duplicate guard additionally
        # requires a minimum of 29 seconds for every subsequent fresh attempt.
        return max(29.0, min(duration / 2.0, 240.0))

    def _history_allows(self, aliases: set[str], monotonic: float, wall_time: float) -> bool:
        for identity in aliases:
            if identity not in self._last_success:
                continue
            since = (
                monotonic - self._success_monotonic[identity]
                if identity in self._success_monotonic
                else wall_time - self._last_success[identity]
            )
            if since < 29.0 or self.active_seconds < 29.0:
                return False
        return True

    def next_submission(
        self, monotonic: float, wall_time: float, manual: bool = False
    ) -> Submission | None:
        if not math.isfinite(monotonic) or not math.isfinite(wall_time):
            raise ValueError("Clock readings must be finite")
        self._advance(monotonic)
        self._last_wall_time = wall_time
        if self.beatmap is None or self.track is None:
            return None
        aliases = self._aliases()
        attempt = self._attempt
        blocked = bool(attempt and attempt.suppressed)
        if manual and any(aliases & credit for credit, _serial in self._force_credits):
            # A restored successful force may use a MusicBrainz identity while
            # the current unresolved selection uses beatmap fallback metadata.
            # Its persisted aliases still prohibit another unplayed force.
            blocked = True
        if attempt and attempt.claim:
            claimed = self._issued[attempt.claim]
            blocked = blocked or claimed.status == "success"
            if not blocked:
                return None
        if not self._history_allows(aliases, monotonic, wall_time):
            blocked = True
        if blocked:
            if manual:
                raise DuplicateSuppressed(DUPLICATE_MESSAGE)
            return None
        if any(
            issued.status == "pending"
            and issued.submission.manual
            and aliases & issued.aliases
            for issued in self._issued.values()
        ):
            return None
        if not manual:
            required = self._required_seconds()
            if (
                not attempt
                or self.phase == Phase.DISCONNECTED
                or required is None
                or attempt.active_seconds < required
            ):
                return None
        elif not attempt and self._selection_claim:
            return None

        current_gameplay = attempt is not None and self.phase in ACTIVE_PHASES | {Phase.PAUSED}
        submission = Submission(
            id=uuid.uuid4().hex,
            attempt_id=(
                attempt.id
                if attempt and (not manual or current_gameplay)
                else f"selection:{self.selection_episode}"
            ),
            track=deepcopy(self.track),
            playback_start=int(
                attempt.started_at
                if attempt and (not manual or current_gameplay)
                else wall_time
            ),
            requested_at=wall_time,
            manual=manual,
        )
        self._issued[submission.id] = _Issued(
            submission, aliases, attempt, self._attempt_serial
        )
        if attempt:
            attempt.claim = submission.id
        else:
            self._selection_claim = submission.id
        return submission

    def mark_pending(self, submission: Submission) -> None:
        """Keep the original attempt reserved while its delivery is queued."""
        issued = self._issued.get(submission.id)
        if issued and issued.status != "success":
            issued.status = "pending"

    def mark_success(
        self, submission: Submission, wall_time: float, monotonic: float | None = None
    ) -> None:
        """Record only an accepted scrobble, at its actual acceptance time."""
        issued = self._issued.get(submission.id)
        if issued and issued.status == "success":
            return
        aliases = issued.aliases if issued else {submission.track.identity}
        for identity in aliases:
            self._last_success[identity] = max(
                wall_time, self._last_success.get(identity, float("-inf"))
            )
            success_clock = monotonic if monotonic is not None else self._last_monotonic
            if success_clock is not None:
                self._success_monotonic[identity] = success_clock
        if issued:
            issued.status = "success"
            if submission.manual:
                if issued.attempt:
                    issued.attempt.suppressed = True
                if issued.next_attempt:
                    issued.next_attempt.suppressed = True
                else:
                    self._force_credits.append((set(aliases), issued.attempt_serial))
        elif submission.manual:
            self._force_credits.append((set(aliases), self._attempt_serial))

    def mark_failed(self, submission: Submission, retryable: bool = False) -> None:
        """A terminal failure consumes this attempt; delivery retries reuse its ID.

        ``retryable=True`` explicitly releases a terminal reservation. Normal
        offline/uncertain deliveries must instead use ``mark_pending``.
        """
        issued = self._issued.get(submission.id)
        if not issued or issued.status == "success":
            return
        issued.status = "failed"
        if retryable:
            if issued.attempt and issued.attempt.claim == submission.id:
                issued.attempt.claim = None
            if self._selection_claim == submission.id:
                self._selection_claim = None

    def status(self) -> dict[str, object]:
        return {
            "phase": self.phase.value,
            "selection_episode": self.selection_episode,
            "attempt_id": self.attempt_id,
            "active_seconds": round(self.active_seconds, 3),
            "playback_start": self.playback_start,
            "threshold_mode": self.mode,
            "required_seconds": self._required_seconds(),
            "suppressed": self.suppressed,
            "pending": bool(
                self._attempt
                and self._attempt.claim
                and self._issued[self._attempt.claim].status == "pending"
            ),
        }
