"""Read-only local tosu adapters; no process handles or memory offsets.

v2 supplies metadata, pauses and failures.  Its StreamCompanion endpoint supplies
the watching/playing distinction absent from v2.  Unknown evidence never counts
as gameplay.  See docs/telemetry.md for the schema and compatibility boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import math
import time
from collections.abc import AsyncIterator
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .models import Beatmap, Phase, Telemetry

# tosu applies these read-only filters to this connection, not to game settings.
# Player names are compared in memory solely to exclude spectating.
V2_FILTERS = [
    "client", "error", {"field": "state", "keys": ["number"]},
    {"field": "beatmap", "keys": ["title", "artist", "titleUnicode", "artistUnicode",
                                  "checksum", "id", "set", "version",
                                  {"field": "time", "keys": ["mp3Length", "live"]}]},
    {"field": "game", "keys": ["paused"]},
    {"field": "play", "keys": ["failed", "playerName"]},
    {"field": "profile", "keys": ["name", {"field": "banchoStatus", "keys": ["number"]}]},
]
TOKEN_FILTERS = ["status", "rawStatus", "md5", "time"]


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


def _decode(message: str | bytes) -> dict:
    value = json.loads(message)
    if not isinstance(value, dict):
        raise ValueError("tosu sent a JSON value instead of an object")
    return value


def _project(payload: dict, filters: list) -> dict:
    """Discard unrequested fields even if an older tosu sends a full snapshot."""
    result = {}
    for item in filters:
        if isinstance(item, str):
            if item in payload:
                result[item] = payload[item]
        elif item["field"] in payload:
            result[item["field"]] = _project(_object(payload[item["field"]]), item["keys"])
    return result


class TosuAdapter:
    """Read-only stable/lazer adapter for tosu's current public endpoints.

    ``parse`` accepts a matching ``/tokens`` snapshot for deterministic replay.
    ``stream`` obtains those snapshots itself and rejects stale observations.
    Only loopback URLs are allowed: tosu publishes local game information.
    """

    compatibility_notice = (
        "Gameplay and menu listening use current tosu v2 plus fresh /tokens evidence. "
        "Stable and lazer are supported; replay, spectator and unknown pause/status "
        "evidence are excluded. Lazer requires matching local/gameplay player names."
    )

    def __init__(
        self,
        url: str = "ws://127.0.0.1:24050/websocket/v2",
        *,
        reconnect_min: float = 0.5,
        reconnect_max: float = 10.0,
        stale_after: float = 2.0,
    ) -> None:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = hostname.lower() == "localhost"
        if (
            parsed.scheme not in {"ws", "wss"}
            or not loopback
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != "/websocket/v2"
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("tosu URL must be a loopback ws(s)://host:port/websocket/v2 URL")
        # Accessing port checks malformed/out-of-range ports before startup.
        _ = parsed.port
        if not 0 < reconnect_min <= reconnect_max <= 60 or not 0 < stale_after <= 60:
            raise ValueError("reconnect and stale timeouts must be positive and bounded")
        self.url = url
        self.tokens_url = urlunsplit(parsed._replace(path="/tokens"))
        self.reconnect_min = reconnect_min
        self.reconnect_max = reconnect_max
        self.stale_after = stale_after
        self._phase = Phase.DISCONNECTED
        self._selection: str | None = None
        self._position: float | None = None
        self._attempt: str | None = None
        self._attempt_counter = 0
        self._companion: dict | None = None
        self._companion_received = -math.inf
        self._menu_context = False
        self._sample_monotonic: float | None = None

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
        )

    @staticmethod
    def _personal_play(payload: dict, companion: dict | None, position: float | None) -> bool:
        """Require same-map, near-synchronous source-backed positive evidence."""
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
            # tosu maps single-player spectating to play; its v2 feed does not
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
        interval_reliable = True
        now = time.monotonic() if monotonic is None else monotonic
        elapsed = max(0.0, now - self._sample_monotonic) if self._sample_monotonic is not None else 0.0
        menu_context = number in {0, 5, 11, 12, 13}
        context_changed = menu_context != self._menu_context
        supported_client = _text(payload.get("client")).casefold() in {"stable", "lazer"}
        if number in {7, 14, 17, 18}:
            phase = Phase.RESULTS
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

        selection_changed = selection != self._selection
        retry = (
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
            self._attempt = f"tosu:{self._attempt_counter}"
        elif retry:
            # A retry observed while paused begins counting only once it resumes.
            self._attempt = None
        self._selection = selection
        self._position = position
        self._phase = phase
        self._menu_context = menu_context
        self._sample_monotonic = now
        return Telemetry(beatmap, phase, position, self._attempt, interval_reliable=interval_reliable)

    async def _watch_tokens(self) -> None:
        delay = self.reconnect_min
        while True:
            try:
                async with connect(self.tokens_url, open_timeout=5, max_size=4 * 1024 * 1024) as socket:
                    await socket.send("applyFilters:" + json.dumps(TOKEN_FILTERS))
                    while True:
                        async with asyncio.timeout(self.stale_after):
                            message = await socket.recv()
                        self._companion = _project(_decode(message), TOKEN_FILTERS)
                        del message
                        self._companion_received = time.monotonic()
                        delay = self.reconnect_min
            except (OSError, TimeoutError, WebSocketException, ValueError):
                self._companion = None
                self._companion_received = -math.inf
                await asyncio.sleep(delay)
                delay = min(self.reconnect_max, delay * 2)

    async def stream(self) -> AsyncIterator[Telemetry]:
        """Reconnect indefinitely with bounded backoff and explicit loss events."""
        companion_task = asyncio.create_task(self._watch_tokens())
        delay = self.reconnect_min
        try:
            while True:
                try:
                    async with connect(self.url, open_timeout=5, max_size=4 * 1024 * 1024) as socket:
                        await socket.send("applyFilters:" + json.dumps(V2_FILTERS))
                        while True:
                            async with asyncio.timeout(self.stale_after):
                                message = await socket.recv()
                            payload = _project(_decode(message), V2_FILTERS)
                            del message
                            companion = (
                                self._companion
                                if time.monotonic() - self._companion_received <= self.stale_after
                                else None
                            )
                            yield self.parse(payload, companion=companion)
                            delay = self.reconnect_min
                except (OSError, TimeoutError, WebSocketException, ValueError):
                    yield self.disconnected()
                    await asyncio.sleep(delay)
                    delay = min(self.reconnect_max, delay * 2)
        finally:
            companion_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await companion_task
