"""Public osu! API credentials and read-only beatmap metadata access.

The public OAuth scope cannot download beatmap archives or act as a user.
Client secrets stay in the OS credential vault; access tokens stay in memory.
"""

from __future__ import annotations

import json

import httpx

from .lastfm import CredentialStore, CredentialError

TOKEN_URL = "https://osu.ppy.sh/oauth/token"
SEARCH_URL = "https://osu.ppy.sh/api/v2/beatmapsets/search"


class OsuApiStore(CredentialStore):
    def __init__(self, *, backend=None):
        super().__init__(service="osu-life-hacks-osu-api", username="public-client", backend=backend)

    def load(self):
        try:
            value = self._secure_backend().get_password(self.service, self.username)
            if value is None:
                return None
            data = json.loads(value)
            client_id = data.get("client_id")
            secret = data.get("client_secret")
            if (type(client_id) is not int or client_id <= 0 or not isinstance(secret, str)
                    or not secret.strip()):
                raise ValueError
            return client_id, secret
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("osu! API credentials could not be read from the OS vault.") from None

    def save(self, client_id, client_secret):
        if type(client_id) is not int or client_id <= 0 or not isinstance(client_secret, str) or not client_secret.strip():
            raise ValueError("Enter a positive osu! client ID and its client secret.")
        try:
            self._secure_backend().set_password(self.service, self.username,
                json.dumps({"client_id": client_id, "client_secret": client_secret}))
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("osu! API credentials could not be saved to the OS vault.") from None


async def public_token(client_id, client_secret, *, client=None):
    """Validate a user's own OAuth app using the supported public scope."""
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)
    try:
        response = await client.post(TOKEN_URL, data={"client_id": client_id,
            "client_secret": client_secret, "grant_type": "client_credentials", "scope": "public"},
            headers={"Accept": "application/json"})
        if response.status_code != 200:
            raise RuntimeError("osu! API connection failed. Check the client ID and secret in your osu! OAuth application.")
        data = response.json()
        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise RuntimeError("osu! API did not return a usable public access token.")
        return token
    except (httpx.HTTPError, ValueError):
        raise RuntimeError("osu! API is unavailable. Try connecting later from setup.") from None
    finally:
        if own_client:
            await client.aclose()


async def preview_ranked(*, store=None, client=None):
    """Use saved public credentials for a small read-only map preview."""
    credentials = (store or OsuApiStore()).load()
    if not credentials:
        raise RuntimeError("Connect the osu! API in setup first.")
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)
    try:
        token = await public_token(*credentials, client=client)
        response = await client.get(SEARCH_URL, params={"m": "0", "s": "ranked", "sort": "ranked_desc"},
            headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
        if response.status_code != 200:
            raise RuntimeError("osu! public map preview is unavailable. Try again later.")
        data = response.json()
        sets = data.get("beatmapsets") if isinstance(data, dict) else None
        if not isinstance(sets, list):
            raise RuntimeError("osu! public map search changed. Try again later.")
        return [{"id": item.get("id"), "artist": item.get("artist"), "title": item.get("title")}
                for item in sets[:5] if isinstance(item, dict)]
    except (httpx.HTTPError, ValueError):
        raise RuntimeError("osu! public map preview is unavailable. Try again later.") from None
    finally:
        if own_client:
            await client.aclose()
