"""Schwab OAuth 2.0 (authorization-code flow) and on-disk token management.

Schwab issues a 30-minute access token and a refresh token that stops working
7 days after the *initial* login; it cannot be extended programmatically.
So the lifecycle is:

1. ``schwab-mcp auth`` - one interactive browser login, token saved to disk.
2. The server refreshes the access token silently whenever it is about to expire.
3. After ~7 days the refresh token dies and you run ``schwab-mcp auth`` again.
   A running server notices the new token file, so no restart is needed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"

ACCESS_TOKEN_SKEW = 60.0
"""Refresh the access token this many seconds before it actually expires."""

REFRESH_TOKEN_LIFETIME = 7 * 24 * 3600.0
REFRESH_TOKEN_WARN_WINDOW = 24 * 3600.0

REAUTH_HINT = "Run `schwab-mcp auth` in a terminal to log in to Schwab again."


class AuthError(RuntimeError):
    """Authentication is missing, expired, or was rejected by Schwab."""


@dataclass
class Token:
    access_token: str
    refresh_token: str
    expires_at: float
    refresh_token_issued_at: float
    token_type: str = "Bearer"
    scope: str | None = None

    @classmethod
    def from_response(
        cls, data: dict[str, Any], *, now: float, refresh_token_issued_at: float | None = None
    ) -> Token:
        try:
            return cls(
                access_token=data["access_token"],
                refresh_token=data["refresh_token"],
                expires_at=now + float(data.get("expires_in", 1800)),
                refresh_token_issued_at=refresh_token_issued_at
                if refresh_token_issued_at is not None
                else now,
                token_type=data.get("token_type", "Bearer"),
                scope=data.get("scope"),
            )
        except KeyError as exc:
            raise AuthError(f"Schwab token response is missing {exc.args[0]!r}.") from exc

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Token:
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=float(data["expires_at"]),
            refresh_token_issued_at=float(data["refresh_token_issued_at"]),
            token_type=data.get("token_type", "Bearer"),
            scope=data.get("scope"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def refresh_token_expires_at(self) -> float:
        return self.refresh_token_issued_at + REFRESH_TOKEN_LIFETIME

    def access_token_fresh(self, now: float) -> bool:
        return now < self.expires_at - ACCESS_TOKEN_SKEW


class TokenStore:
    """JSON token file, written atomically and readable only by the current user."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> Token | None:
        if not self.path.exists():
            return None
        try:
            return Token.from_dict(json.loads(self.path.read_text()))
        except (ValueError, KeyError, TypeError) as exc:
            raise AuthError(f"Token file {self.path} is unreadable ({exc}). {REAUTH_HINT}") from exc

    def save(self, token: Token) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.path.parent, 0o700)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".token-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(token.to_dict(), fh, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def mtime(self) -> float | None:
        try:
            return self.path.stat().st_mtime
        except FileNotFoundError:
            return None


def new_state() -> str:
    return secrets.token_urlsafe(24)


def build_authorization_url(client_id: str, callback_url: str, state: str | None = None) -> str:
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": callback_url}
    if state:
        params["state"] = state
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def extract_code(redirected_url: str, callback_url: str, expected_state: str | None = None) -> str:
    """Pull the authorization code out of the URL Schwab redirected the browser to."""
    redirected_url = redirected_url.strip()
    got, want = urlparse(redirected_url), urlparse(callback_url)
    if (got.scheme, got.netloc) != (want.scheme, want.netloc):
        raise AuthError(
            f"That URL does not start with your callback URL ({callback_url}). Paste the full address "
            "from the browser bar after logging in."
        )
    query = parse_qs(got.query)
    if "error" in query:
        detail = query.get("error_description", query["error"])[0]
        raise AuthError(f"Schwab returned an error instead of a code: {detail}")
    if expected_state and query.get("state", [expected_state])[0] != expected_state:
        raise AuthError("OAuth state mismatch - the URL belongs to a different login attempt. Try again.")
    codes = query.get("code")
    if not codes or not codes[0]:
        raise AuthError("No `code` parameter found in that URL.")
    return codes[0]  # parse_qs already percent-decoded it (Schwab codes end in "%40" -> "@")


async def _token_request(
    http: httpx.AsyncClient, client_id: str, client_secret: str, data: dict[str, str]
) -> dict[str, Any]:
    try:
        resp = await http.post(
            TOKEN_URL, data=data, auth=(client_id, client_secret), headers={"Accept": "application/json"}
        )
    except httpx.HTTPError as exc:
        raise AuthError(f"Could not reach Schwab's token endpoint: {exc}") from exc
    if resp.is_error:
        try:
            body = resp.json()
            detail = body.get("error_description") or body.get("error") or resp.text
        except ValueError:
            detail = resp.text
        raise AuthError(f"Schwab token endpoint returned HTTP {resp.status_code}: {detail}")
    return resp.json()


async def exchange_code(
    http: httpx.AsyncClient,
    client_id: str,
    client_secret: str,
    code: str,
    callback_url: str,
    *,
    now: float | None = None,
) -> Token:
    data = await _token_request(
        http,
        client_id,
        client_secret,
        {"grant_type": "authorization_code", "code": code, "redirect_uri": callback_url},
    )
    return Token.from_response(data, now=time.time() if now is None else now)


class TokenManager:
    """Hands out a valid access token, refreshing (and persisting) it when needed."""

    def __init__(
        self,
        store: TokenStore,
        client_id: str,
        client_secret: str,
        http: httpx.AsyncClient,
        clock: Callable[[], float] = time.time,
    ):
        self.store = store
        self.client_id = client_id
        self.client_secret = client_secret
        self.http = http
        self.clock = clock
        self._token: Token | None = None
        self._seen_mtime: float | None = None
        self._lock = asyncio.Lock()

    def _reload_if_changed(self) -> None:
        # Picks up a token written by a fresh `schwab-mcp auth` without a server restart.
        mtime = self.store.mtime()
        if self._token is None or mtime != self._seen_mtime:
            self._token = self.store.load()
            self._seen_mtime = mtime

    def _require_token(self) -> Token:
        self._reload_if_changed()
        if self._token is None:
            raise AuthError(f"Not logged in to Schwab (no token at {self.store.path}). {REAUTH_HINT}")
        return self._token

    async def get_access_token(self) -> str:
        token = self._require_token()
        if token.access_token_fresh(self.clock()):
            return token.access_token
        async with self._lock:
            token = self._require_token()  # another task may have refreshed meanwhile
            if not token.access_token_fresh(self.clock()):
                token = await self._refresh(token)
            return token.access_token

    async def force_refresh(self) -> None:
        """Refresh now, e.g. after Schwab rejected an access token we believed valid."""
        async with self._lock:
            await self._refresh(self._require_token())

    async def _refresh(self, token: Token) -> Token:
        now = self.clock()
        if now >= token.refresh_token_expires_at:
            raise AuthError(f"The Schwab refresh token is older than 7 days and has expired. {REAUTH_HINT}")
        logger.info("Refreshing Schwab access token")
        try:
            data = await _token_request(
                self.http,
                self.client_id,
                self.client_secret,
                {"grant_type": "refresh_token", "refresh_token": token.refresh_token},
            )
        except AuthError as exc:
            raise AuthError(
                f"{exc}. The refresh token was probably revoked or expired. {REAUTH_HINT}"
            ) from exc
        # The 7-day clock is anchored to the interactive login, not to refreshes.
        new = Token.from_response(data, now=now, refresh_token_issued_at=token.refresh_token_issued_at)
        self.store.save(new)
        self._token, self._seen_mtime = new, self.store.mtime()
        return new

    def status(self) -> dict[str, Any]:
        now = self.clock()
        try:
            token = self._require_token()
        except AuthError as exc:
            return {"authenticated": False, "token_path": str(self.store.path), "detail": str(exc)}
        seconds_left = token.refresh_token_expires_at - now
        status: dict[str, Any] = {
            "authenticated": seconds_left > 0,
            "token_path": str(self.store.path),
            "logged_in_at": _iso(token.refresh_token_issued_at),
            "access_token_expires_in_seconds": max(0, round(token.expires_at - now)),
            "login_expires_at": _iso(token.refresh_token_expires_at),
            "login_expires_in_hours": round(max(0.0, seconds_left) / 3600, 1),
        }
        if seconds_left <= 0:
            status["warning"] = f"Login has expired. {REAUTH_HINT}"
        elif seconds_left < REFRESH_TOKEN_WARN_WINDOW:
            status["warning"] = f"Login expires in less than a day. {REAUTH_HINT}"
        return status


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
