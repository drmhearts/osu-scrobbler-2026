"""Last.fm authorization and deliberately conservative scrobble delivery.

The service has no idempotency key. A request that may have been sent is never
silently retried: its caller must retain the submission as ``uncertain``.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import sys
from typing import Any
from urllib.parse import urlencode

import httpx
import keyring

from .models import Submission

API_URL = "https://ws.audioscrobbler.com/2.0/"
AUTH_URL = "https://www.last.fm/api/auth/"


@dataclass(frozen=True, repr=False)
class Credentials:
    api_key: str
    api_secret: str
    session_key: str | None = None


class CredentialError(RuntimeError):
    """Secure credential storage is unavailable or contains invalid data."""


class CredentialStore:
    """Store all credentials in the native OS vault, never in a local file.

    Windows always uses Windows Credential Manager. Other platforms must have
    an explicitly supported secure system keyring. No plaintext fallback or
    chained backend is accepted. An injected backend is for isolated tests.
    """

    def __init__(self, service: str = "osu-scrobbler", username: str = "credentials",
                 *, backend: Any = None) -> None:
        self.service = service
        self.username = username
        self._backend = backend

    def _secure_backend(self) -> Any:
        if self._backend is not None:
            return self._backend
        try:
            if sys.platform == "win32":
                from keyring.backends.Windows import WinVaultKeyring
                backend = WinVaultKeyring()
            else:
                backend = keyring.get_keyring()
                secure_modules = ("keyring.backends.SecretService", "keyring.backends.libsecret",
                                  "keyring.backends.macOS", "keyring.backends.kwallet")
                if not type(backend).__module__.startswith(secure_modules):
                    raise CredentialError("A secure system credential vault is required; no plaintext fallback is used.")
            if backend.priority <= 0:
                raise CredentialError("The system credential vault is unavailable.")
            self._backend = backend
            return backend
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("The system credential vault is unavailable. Enable Windows Credential Manager or a secure OS keyring.") from None

    def load(self) -> Credentials | None:
        try:
            value = self._secure_backend().get_password(self.service, self.username)
            if value is None:
                return None
            data = json.loads(value)
            if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data[k] for k in ("api_key", "api_secret")):
                raise ValueError()
            session = data.get("session_key")
            if session is not None and (not isinstance(session, str) or not session):
                raise ValueError()
            return Credentials(data["api_key"], data["api_secret"], session)
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("Credentials could not be read from the secure system vault; authorize again.") from None

    def save(self, credentials: Credentials) -> None:
        if not credentials.api_key or not credentials.api_secret:
            raise CredentialError("An API key and API secret are required.")
        try:
            self._secure_backend().set_password(self.service, self.username, json.dumps({
                "api_key": credentials.api_key, "api_secret": credentials.api_secret,
                "session_key": credentials.session_key,
            }))
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("Credentials could not be saved to the secure system vault.") from None

    def delete(self) -> None:
        try:
            backend = self._secure_backend()
            if backend.get_password(self.service, self.username) is not None:
                backend.delete_password(self.service, self.username)
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("Credentials could not be removed from the secure system vault.") from None


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    message: str = ""
    accepted: int = 0
    ignored: int = 0


class LastFmError(RuntimeError):
    """An authorization failure with a safe, credential-free message."""


API_ERRORS = {
    2: "Invalid service.", 3: "Invalid method.", 4: "Authentication failed.",
    5: "Invalid response format.", 6: "Invalid parameters.", 7: "Invalid resource.",
    8: "Last.fm operation failed.", 9: "Invalid session; authorize again.",
    10: "Invalid API key.", 11: "Last.fm service is offline.",
    13: "Invalid API signature.", 14: "Authorization token has not been authorized.",
    15: "Authorization token has expired.", 16: "Last.fm temporarily unavailable.",
    17: "Login required.", 26: "API key suspended.", 29: "Last.fm rate limit exceeded.",
}
IGNORED_MESSAGES = {
    1: "Artist was ignored by Last.fm.", 2: "Track was ignored by Last.fm.",
    3: "Timestamp is too old.", 4: "Timestamp is too new.",
    5: "Daily scrobble limit exceeded.",
}
RETRYABLE_API_ERRORS = {8, 11, 16, 29}


def api_signature(params: dict[str, str], secret: str) -> str:
    """Sign decoded parameter values, excluding protocol-only format/callback."""
    value = "".join(k + params[k] for k in sorted(params) if k not in {"format", "callback", "api_sig"}) + secret
    return hashlib.md5(value.encode("utf-8")).hexdigest()


class LastFmClient:
    def __init__(self, credentials: Credentials, client: httpx.AsyncClient | None = None) -> None:
        self.credentials = credentials
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=False)

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def _params(self, method: str, values: dict[str, str] | None = None, *, session: bool = False) -> dict[str, str]:
        params = {"method": method, "api_key": self.credentials.api_key, **(values or {})}
        if session:
            if not self.credentials.session_key:
                raise LastFmError("Authorize Last.fm first; a session key is required.")
            params["sk"] = self.credentials.session_key
        params["api_sig"] = api_signature(params, self.credentials.api_secret)
        params["format"] = "json"
        return params

    async def _auth(self, method: str, values: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            response = await self.client.get(API_URL, params=self._params(method, values))
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise LastFmError("Last.fm authorization request failed; check connectivity and try again.") from None
        if not isinstance(data, dict):
            raise LastFmError("Last.fm returned an invalid authorization response.")
        if "error" in data:
            try:
                code = int(data["error"])
            except (TypeError, ValueError):
                code = 0
            raise LastFmError(API_ERRORS.get(code, "Last.fm rejected authorization."))
        if response.is_error:
            raise LastFmError(f"Last.fm authorization failed (HTTP {response.status_code}).")
        return data

    async def get_token(self) -> str:
        data = await self._auth("auth.getToken")
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise LastFmError("Last.fm did not return an authorization token.")
        return token

    def authorization_url(self, token: str) -> str:
        return AUTH_URL + "?" + urlencode({"api_key": self.credentials.api_key, "token": token})

    async def get_session(self, token: str) -> str:
        data = await self._auth("auth.getSession", {"token": token})
        session = data.get("session")
        key = session.get("key") if isinstance(session, dict) else None
        if not isinstance(key, str) or not key:
            raise LastFmError("Last.fm did not return an authorized session key.")
        return key

    async def submit(self, submission: Submission) -> DeliveryResult:
        track = submission.track
        values = {"artist": track.artist, "track": track.title, "timestamp": str(submission.playback_start),
                  "chosenByUser": "1"}
        if track.album:
            values["album"] = track.album
        if track.album_artist:
            values["albumArtist"] = track.album_artist
        if track.duration is not None and track.duration > 0:
            values["duration"] = str(int(track.duration))
        if track.recording_id:
            values["mbid"] = track.recording_id
        try:
            params = self._params("track.scrobble", values, session=True)
        except LastFmError as error:
            return DeliveryResult("failed", str(error))
        try:
            response = await self.client.post(API_URL, data=params)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            return DeliveryResult("retry", "Could not connect to Last.fm; submission remains queued.")
        except httpx.HTTPError:
            return DeliveryResult("uncertain", "Last.fm delivery outcome is unknown; review before explicitly retrying.")
        try:
            data = response.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and "error" in data:
            try:
                code = int(data["error"])
            except (TypeError, ValueError):
                code = 0
            return DeliveryResult("retry" if code in RETRYABLE_API_ERRORS else "failed",
                                  API_ERRORS.get(code, "Last.fm rejected the submission."))
        if response.status_code == 429:
            return DeliveryResult("retry", "Last.fm rate limit exceeded; submission remains queued.")
        if response.is_error:
            if response.status_code >= 500:
                return DeliveryResult("uncertain", f"Last.fm returned HTTP {response.status_code}; delivery outcome is unknown.")
            return DeliveryResult("failed", f"Last.fm rejected the submission (HTTP {response.status_code}).")
        try:
            scrobbles = data["scrobbles"]
            accepted = int(scrobbles["@attr"]["accepted"])
            ignored = int(scrobbles["@attr"]["ignored"])
            entry = scrobbles["scrobble"]
            if isinstance(entry, list):
                if len(entry) != 1:
                    raise ValueError()
                entry = entry[0]
            ignore_code = int(entry["ignoredMessage"]["code"])
            if (accepted, ignored, ignore_code) == (1, 0, 0):
                return DeliveryResult("accepted", "Scrobble accepted by Last.fm.", accepted, ignored)
            if accepted == 0 and ignored == 1 and ignore_code != 0:
                return DeliveryResult("ignored", IGNORED_MESSAGES.get(ignore_code, "Scrobble ignored by Last.fm."), accepted, ignored)
        except (TypeError, KeyError, ValueError, OverflowError):
            pass
        return DeliveryResult("uncertain", "Last.fm returned inconsistent or missing scrobble counts; review before retrying.")
