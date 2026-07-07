"""OAuth2 client_credentials token-fetch + cache + refresh.

Reusable for any payment provider that uses short-lived bearer tokens
issued via the OAuth2 `client_credentials` grant. Today this is just
Nomba; if a third provider needs OAuth, drop it in the same way.

Design:
  * Token state lives on the instance (not module-level). Each
    provider has its own client_id, so we don't want a global cache
    accidentally returning Token A when asked for Token B.
  * `asyncio.Lock` per-instance serializes concurrent fetches. The
    `get_token()` path is "fast path": cache hit → return. Lock is
    only acquired when we need to refresh.
  * Tokens are considered fresh until 60s before their reported
    `expiresAt` (Nomba docs recommend refreshing 5 min early, but
    a 60s buffer is plenty for in-process use and avoids races
    where a token expires mid-request).

Errors:
  * `OAuth2Error` is raised on any 4xx from the token endpoint.
    The caller should let it bubble up to the user (a misconfigured
    `client_id` / `client_secret` is a deployment bug, not a runtime
    fault).
  * Network errors are NOT caught here — `httpx.HTTPError` bubbles up
    so the calling code (the provider) can decide whether to retry
    or surface the error.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

logger = logging.getLogger(__name__)


class OAuth2Error(RuntimeError):
    """Token endpoint returned a non-2xx or the response was malformed."""


@dataclass
class _CachedToken:
    access_token: str
    refresh_token: str
    expires_at: datetime  # timezone-aware


# Refresh this many seconds before the token's reported expiry.
# 60s gives in-flight requests plenty of time to finish; if a token
# expires mid-call the provider's 401 path triggers a one-shot
# refresh via `force_refresh()`.
_REFRESH_LEEWAY_SECONDS = 60


class OAuth2ClientCredentials:
    """OAuth2 client_credentials token manager.

    Usage:
        oauth = OAuth2ClientCredentials(
            token_url="https://api.nomba.com/v1/auth/token/issue",
            client_id="...", client_secret="...", account_id="...",
        )
        token = await oauth.get_token(http)
        # http is the provider's shared httpx.AsyncClient

    Threading: not thread-safe. Asyncio-safe via a per-instance lock.
    Process-local only (no Redis/DB cache) — each replica fetches its
    own token on cold start.
    """

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        client_secret: str,
        account_id: str,
        timeout: float = 30.0,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._account_id = account_id
        self._timeout = timeout
        self._cached: _CachedToken | None = None
        self._lock = asyncio.Lock()

    @property
    def account_id(self) -> str:
        """Header value sent on every Nomba call (the parent business UUID)."""
        return self._account_id

    @property
    def client_id(self) -> str:
        return self._client_id

    def _is_fresh(self, token: _CachedToken) -> bool:
        now = datetime.now(tz=UTC)
        expires_with_leeway = token.expires_at.timestamp() - _REFRESH_LEEWAY_SECONDS
        return now.timestamp() < expires_with_leeway

    async def get_token(self, http: httpx.AsyncClient) -> str:
        """Return a valid access token. Refreshes on demand.

        Concurrency: if 10 callers ask for a token simultaneously while
        the cache is empty, the lock ensures only one of them fetches;
        the other 9 wait and pick up the freshly-cached token.
        """
        async with self._lock:
            if self._cached is not None and self._is_fresh(self._cached):
                return self._cached.access_token
            await self._fetch_locked(http)
            assert self._cached is not None
            return self._cached.access_token

    async def force_refresh(self, http: httpx.AsyncClient) -> str:
        """Discard the cached token and fetch a new one.

        Called by the provider when a request returns 401. We assume
        the current token is compromised / revoked; we don't try to
        reuse the refresh token (Nomba's flow uses the refresh token
        to get a new access token without re-sending client_secret;
        but if 401 happens, the simplest path is to re-auth from
        scratch)."""
        async with self._lock:
            self._cached = None
            await self._fetch_locked(http)
            assert self._cached is not None
            return self._cached.access_token

    async def _fetch_locked(self, http: httpx.AsyncClient) -> None:
        """Fetch a new token from the provider. MUST be called with
        `self._lock` held."""
        logger.info("OAuth2: fetching new access token from %s", self._token_url)
        try:
            resp = await http.post(
                self._token_url,
                headers={
                    "Content-Type": "application/json",
                    "accountId": self._account_id,
                },
                json={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            #raise OAuth2Error(f"OAuth2 transport error: {exc}") from exc
            logger.exception("OAuth2 transport error reaching %s", self._token_url)
            raise OAuth2Error(
                f"OAuth2 transport error reaching {self._token_url}: {type(exc).__name__}: {exc!r}"
            ) from exc

        if resp.status_code != 200:
            # Surface a clear message; body is provider-specific.
            raise OAuth2Error(
                f"OAuth2 token endpoint returned {resp.status_code}: {resp.text[:200]}"
            )

        try:
            payload = resp.json()
            data = payload["data"]
            access = data["access_token"]
            refresh = data.get("refresh_token", "")
            # Nomba returns ISO 8601 with 'Z' suffix; Python's
            # datetime.fromisoformat in 3.12 handles 'Z' natively.
            expires_at = datetime.fromisoformat(data["expiresAt"].replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError) as exc:
            raise OAuth2Error(
                f"OAuth2 token response missing or malformed: {exc}"
            ) from exc

        self._cached = _CachedToken(
            access_token=access,
            refresh_token=refresh,
            expires_at=expires_at,
        )
        logger.info("OAuth2: token acquired, expires_at=%s", expires_at.isoformat())


__all__ = ["OAuth2ClientCredentials", "OAuth2Error"]
