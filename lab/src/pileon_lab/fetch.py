"""Polite, cached, resumable XRPC fetching.

Every definitive response (2xx, and 4xx like "record not found") is cached in
SQLite keyed by URL, so an enrichment run can be interrupted and rerun without
re-fetching, and feature computation can run fully offline afterwards.
Transient failures (429, 5xx, network errors) are retried with backoff and
never cached. Each host gets its own request-rate budget.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlsplit

import httpx

if TYPE_CHECKING:
    from pathlib import Path

USER_AGENT = "pileon-lab/0.1 (github.com/Iteratrix/pileon)"
APPVIEW = "https://public.api.bsky.app"
PLC = "https://plc.directory"
_RETRIES = 7
_RETRYABLE = frozenset({429, 500, 502, 503, 504})
_STATUS_OK = 200
_STATUS_CLIENT_ERROR = 400
_STATUS_SERVER_ERROR = 500
NOT_FETCHED = 0
"""Status of an offline cache miss: the request was never made."""


class FetchError(RuntimeError):
    """A request that still failed after every retry."""


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: dict[str, Any] | None

    @property
    def ok(self) -> bool:
        return self.status == _STATUS_OK and self.body is not None


class Cache:
    """URL-keyed response cache in one SQLite file."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS responses ("
            "url TEXT PRIMARY KEY, status INTEGER NOT NULL, body TEXT, "
            "fetched_at REAL NOT NULL)"
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._pending = 0

    def get(self, url: str) -> Response | None:
        row = self._conn.execute(
            "SELECT status, body FROM responses WHERE url = ?", (url,)
        ).fetchone()
        if row is None:
            return None
        status, body = row
        return Response(status, json.loads(body) if body else None)

    def put(self, url: str, response: Response) -> None:
        body = json.dumps(response.body) if response.body is not None else None
        self._conn.execute(
            "INSERT OR REPLACE INTO responses VALUES (?, ?, ?, ?)",
            (url, response.status, body, time.time()),
        )
        self._pending += 1
        if self._pending >= 200:  # noqa: PLR2004
            self.commit()

    def commit(self) -> None:
        self._conn.commit()
        self._pending = 0

    def close(self) -> None:
        self.commit()
        self._conn.close()


class _Pacer:
    """Spaces requests to one host at most `rate` per second."""

    def __init__(self, rate: float) -> None:
        self._interval = 1.0 / rate
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self._interval
        if delay > 0:
            await asyncio.sleep(delay)


@dataclass
class Stats:
    fetched: int = 0
    cached: int = 0
    retried: int = 0
    missing: int = 0


class Fetcher:
    """Cached GETs with a request budget per host.

    With `offline=True` nothing touches the network: cache misses come back
    with status [`NOT_FETCHED`], so analysis code can run on exactly what an
    enrichment run collected.
    """

    def __init__(
        self,
        cache: Cache,
        *,
        rates: dict[str, float] | None = None,
        default_rate: float = 4.0,
        client: httpx.AsyncClient | None = None,
        offline: bool = False,
    ) -> None:
        self._cache = cache
        self._offline = offline
        self._rates = {"public.api.bsky.app": 8.0, "plc.directory": 5.0} | (rates or {})
        self._default_rate = default_rate
        self._pacers: dict[str, _Pacer] = {}
        self._client = client or httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": USER_AGENT}
        )
        self.stats = Stats()

    async def get(
        self, base: str, path: str, params: list[tuple[str, str]] | None = None
    ) -> Response:
        """Return the (possibly cached) response for `base + path + params`."""
        query = urlencode(params or [])
        url = f"{base}{path}" + (f"?{query}" if query else "")
        cached = self._cache.get(url)
        if cached is not None:
            self.stats.cached += 1
            return cached
        if self._offline:
            self.stats.missing += 1
            return Response(NOT_FETCHED, None)
        response = await self._fetch(url)
        self._cache.put(url, response)
        self.stats.fetched += 1
        return response

    async def xrpc(
        self, base: str, method: str, params: list[tuple[str, str]]
    ) -> Response:
        return await self.get(base, f"/xrpc/{method}", params)

    async def _fetch(self, url: str) -> Response:
        host = urlsplit(url).hostname or ""
        pacer = self._pacers.setdefault(
            host, _Pacer(self._rates.get(host, self._default_rate))
        )
        for attempt in range(_RETRIES):
            await pacer.wait()
            try:
                reply = await self._client.get(url)
            except httpx.TransportError:
                await self._backoff(attempt, None)
                continue
            if reply.status_code in _RETRYABLE:
                await self._backoff(attempt, reply.headers.get("retry-after"))
                continue
            if reply.status_code >= _STATUS_SERVER_ERROR:
                break
            return Response(reply.status_code, _json_or_none(reply))
        msg = f"GET {url} failed after {_RETRIES} attempts"
        raise FetchError(msg)

    async def _backoff(self, attempt: int, retry_after: str | None) -> None:
        self.stats.retried += 1
        delay = min(2.0**attempt, 60.0)
        if retry_after is not None and retry_after.isdigit():
            delay = max(delay, float(retry_after))
        await asyncio.sleep(delay)

    async def close(self) -> None:
        await self._client.aclose()
        self._cache.commit()


def _json_or_none(reply: httpx.Response) -> dict[str, Any] | None:
    try:
        body = reply.json()
    except json.JSONDecodeError:
        return None
    return body if isinstance(body, dict) else None


async def resolve_pds(fetcher: Fetcher, did: str) -> str | None:
    """Return the PDS endpoint for `did`, or `None` if it does not resolve."""
    if did.startswith("did:plc:"):
        response = await fetcher.get(PLC, f"/{did}")
    elif did.startswith("did:web:"):
        domain = did.removeprefix("did:web:")
        response = await fetcher.get(f"https://{domain}", "/.well-known/did.json")
    else:
        return None
    if not response.ok or response.body is None:
        return None
    for service in response.body.get("service", []):
        if service.get("id", "").endswith("#atproto_pds"):
            endpoint = service.get("serviceEndpoint")
            return endpoint if isinstance(endpoint, str) else None
    return None


def is_client_error(response: Response) -> bool:
    return _STATUS_CLIENT_ERROR <= response.status < _STATUS_SERVER_ERROR
