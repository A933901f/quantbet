"""Polite, cache-first HTTP layer for free public data sources.

Design rules (non-negotiable, enforced here):

* **Never** contacts a Bet365 domain (``ForbiddenHostError``).
* Honest, identifiable ``User-Agent`` (configurable via ``QB_USER_AGENT``); no
  TLS-fingerprint impersonation, no rotating user agents or proxies, no captcha
  solving. ``curl_cffi`` can be selected as a *plain* HTTP client – the
  ``impersonate`` parameter is never passed.
* Low request rate: minimum interval per resource (default 15 s), global
  concurrency (default 2), a global minimum gap between requests, random jitter.
* Local SQLite cache: fresh responses are served without a network call.
* HTTP 403 / 429 / 503 or an HTML challenge page => the *host* is backed off
  exponentially (Retry-After honoured) and the caller is told to fail over.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "quantbet-signal-engine/0.2 (personal, read-only stats; contact: set QB_USER_AGENT)"

FORBIDDEN_HOST_MARKERS: tuple[str, ...] = ("bet365",)
"""Any host containing one of these markers is refused (bet365.com, bet365.es, ...)."""

CHALLENGE_MARKERS: tuple[str, ...] = (
    "cf-chl",
    "challenge-platform",
    "just a moment",
    "attention required",
    "captcha",
    "are you a robot",
    "access denied",
)


class ForbiddenHostError(RuntimeError):
    """Raised when code tries to contact a forbidden (bookmaker) host."""


class SourceBlockedError(RuntimeError):
    """Raised/returned when a source answered 403/429/503 or a challenge page."""


def assert_allowed_url(url: str) -> str:
    """Return the host of ``url``; raise :class:`ForbiddenHostError` for Bet365 hosts."""
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        raise ValueError(f"invalid URL: {url!r}")
    if any(m in host for m in FORBIDDEN_HOST_MARKERS):
        raise ForbiddenHostError(f"refusing to contact {host}: bookmaker hosts are never queried")
    return host


def looks_like_challenge(status: int, content_type: str, body: str) -> bool:
    """Heuristic: HTML where JSON was expected and typical bot-challenge markers."""
    if "json" in (content_type or "").lower():
        return False
    low = body[:5000].lower()
    return any(m in low for m in CHALLENGE_MARKERS)


def _html_challenge(status: int, body: str) -> bool:
    """For HTML pages: a challenge page has challenge markers and no app payload (``__NEXT_DATA__``)."""
    if status < 400 and "__next_data__" in body.lower():
        return False
    low = body[:8000].lower()
    return any(m in low for m in CHALLENGE_MARKERS)


def page_title(body: str) -> str:
    """``<title>`` of an HTML error/challenge page, for logging (bs4 if installed)."""
    try:
        from bs4 import BeautifulSoup

        t = BeautifulSoup(body[:20000], "html.parser").title
        return (t.get_text(strip=True) if t else "")[:120]
    except Exception:  # noqa: BLE001 - optional dependency / malformed HTML
        low = body.lower()
        i, j = low.find("<title>"), low.find("</title>")
        return body[i + 7 : j].strip()[:120] if 0 <= i < j else ""


@dataclass(slots=True)
class FetchResult:
    """Outcome of :meth:`PoliteFetcher.get_json`."""

    url: str
    status: int
    data: Any | None
    from_cache: bool = False
    blocked: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True if JSON data is available (fresh or cached)."""
        return self.data is not None and not self.blocked


class ResponseCache:
    """Tiny synchronous SQLite cache keyed by URL (fast enough for a few req/min)."""

    def __init__(self, path: Path | str = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS http_cache (url TEXT PRIMARY KEY, status INTEGER, body TEXT, fetched_at REAL)"
        )
        self.db.commit()

    def get(self, url: str, max_age_s: float, now: float | None = None) -> tuple[int, Any, float] | None:
        """Cached (status, json, fetched_at) if younger than ``max_age_s``."""
        now = time.time() if now is None else now
        row = self.db.execute("SELECT status, body, fetched_at FROM http_cache WHERE url = ?", (url,)).fetchone()
        if row is None or now - row[2] > max_age_s:
            return None
        return int(row[0]), json.loads(row[1]), float(row[2])

    def put(self, url: str, status: int, data: Any, now: float | None = None) -> None:
        """Store a JSON response."""
        self.db.execute(
            "INSERT OR REPLACE INTO http_cache (url, status, body, fetched_at) VALUES (?, ?, ?, ?)",
            (url, status, json.dumps(data), time.time() if now is None else now),
        )
        self.db.commit()

    def close(self) -> None:
        """Close the database."""
        self.db.close()


@dataclass(slots=True)
class HostState:
    """Backoff bookkeeping per host."""

    failures: int = 0
    blocked_until: float = 0.0
    last_request: float = 0.0


class PoliteFetcher:
    """Rate-limited, cache-first JSON fetcher shared by all free-source adapters.

    ``transport`` (for tests) is an async callable ``(url, headers) -> (status,
    content_type, text, headers)``; by default aiohttp (or plain curl_cffi when
    ``backend="curl_cffi"``) is used.
    """

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        cache: ResponseCache | None = None,
        min_interval_s: float = 15.0,
        global_concurrency: int = 2,
        global_min_gap_s: float = 1.0,
        jitter_s: float = 3.0,
        backoff_base_s: float = 60.0,
        backoff_max_s: float = 3600.0,
        timeout_s: float = 10.0,
        backend: str = "aiohttp",
        transport: Any | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.cache = cache or ResponseCache()
        self.min_interval_s = min_interval_s
        self.sem = asyncio.Semaphore(max(1, global_concurrency))
        self.global_min_gap_s = global_min_gap_s
        self.jitter_s = jitter_s
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s
        self.timeout_s = timeout_s
        self.backend = backend
        self._transport = transport
        self._session: Any | None = None
        self.hosts: dict[str, HostState] = {}
        self._last_by_url: dict[str, float] = {}
        self._last_global = 0.0
        self._gap_lock = asyncio.Lock()
        self.extra_headers = dict(extra_headers or {})
        self.n_requests = 0
        self.refusals: dict[str, int] = {}  # "host status" -> count (403/429/503/challenge)
        self.ok_by_host: dict[str, int] = {}

    # -- lifecycle ----------------------------------------------------------
    async def __aenter__(self) -> PoliteFetcher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session is not None:
            close = getattr(self._session, "close", None)
            if close is not None:
                res = close()
                if asyncio.iscoroutine(res):
                    await res
            self._session = None

    # -- state --------------------------------------------------------------
    def host_state(self, host: str) -> HostState:
        """Backoff state of a host."""
        return self.hosts.setdefault(host, HostState())

    def is_blocked(self, url_or_host: str, now: float | None = None) -> bool:
        """True while a host is in backoff."""
        host = urlsplit(url_or_host).hostname or url_or_host
        now = time.time() if now is None else now
        return self.host_state(host.lower()).blocked_until > now

    def _register_block(self, host: str, status: int, retry_after: float | None, reason: str) -> float:
        st = self.host_state(host)
        st.failures += 1
        k = f"{host} {status}"
        self.refusals[k] = self.refusals.get(k, 0) + 1
        delay = min(self.backoff_max_s, self.backoff_base_s * 2 ** (st.failures - 1))
        if retry_after:
            delay = max(delay, min(retry_after, self.backoff_max_s))
        delay += random.uniform(0, self.jitter_s)
        st.blocked_until = time.time() + delay
        log.warning(
            "source refused request - backing off and failing over",
            extra={"host": host, "status": status, "reason": reason, "backoff_s": round(delay, 1), "failures": st.failures},
        )
        return delay

    # -- transport ----------------------------------------------------------
    async def _default_transport(self, url: str, headers: dict[str, str]) -> tuple[int, str, str, dict[str, str]]:
        if self.backend == "curl_cffi":
            from curl_cffi.requests import AsyncSession  # plain client, *no* impersonate

            if self._session is None:
                self._session = AsyncSession()
            r = await self._session.get(url, headers=headers, timeout=self.timeout_s)
            hdrs = {k.lower(): v for k, v in r.headers.items()}
            return r.status_code, hdrs.get("content-type", ""), r.text, hdrs
        import aiohttp

        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout_s))
        async with self._session.get(url, headers=headers) as resp:
            text = await resp.text()
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, resp.headers.get("Content-Type", ""), text, hdrs

    async def _pace(self, url: str) -> None:
        """Enforce per-URL min interval and the global min gap (+ jitter)."""
        async with self._gap_lock:
            now = time.time()
            wait_url = self._last_by_url.get(url, 0.0) + self.min_interval_s - now
            wait_gap = self._last_global + self.global_min_gap_s - now
            wait = max(0.0, wait_url, wait_gap)
            if wait > 0 or self.jitter_s > 0:
                await asyncio.sleep(wait + random.uniform(0, self.jitter_s * 0.25))
            self._last_global = time.time()
            self._last_by_url[url] = self._last_global

    # -- public -------------------------------------------------------------
    async def get_json(self, url: str, cache_ttl_s: float = 15.0, headers: dict[str, str] | None = None) -> FetchResult:
        """GET JSON politely. Never raises for HTTP errors; inspect the result."""
        return await self._get(url, cache_ttl_s, headers, as_json=True)

    async def get_text(self, url: str, cache_ttl_s: float = 60.0, headers: dict[str, str] | None = None) -> FetchResult:
        """GET an HTML/text page politely (same pacing, cache and backoff; redirects followed
        by the HTTP client). ``data`` is the body text."""
        return await self._get(url, cache_ttl_s, headers, as_json=False)

    async def _get(self, url: str, cache_ttl_s: float, headers: dict[str, str] | None, as_json: bool) -> FetchResult:
        host = assert_allowed_url(url)
        cached = self.cache.get(url, cache_ttl_s)
        if cached is not None:
            return FetchResult(url, cached[0], cached[1], from_cache=True)
        if self.is_blocked(host):
            return FetchResult(url, 0, None, blocked=True, error="host in backoff")
        accept = "application/json" if as_json else "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"
        hdrs = {"User-Agent": self.user_agent, "Accept": accept, **self.extra_headers, **(headers or {})}
        async with self.sem:
            await self._pace(url)
            self.n_requests += 1
            try:
                transport = self._transport or self._default_transport
                status, ctype, text, rh = await transport(url, hdrs)
            except Exception as exc:  # noqa: BLE001 - network errors are data here
                log.info("fetch failed", extra={"host": host, "error": repr(exc)})
                return FetchResult(url, 0, None, error=repr(exc))
        self.host_state(host).last_request = time.time()
        challenge = looks_like_challenge(status, ctype, text) if as_json else _html_challenge(status, text)
        if status in (403, 429, 503) or challenge:
            ra = None
            try:
                ra = float(rh.get("retry-after", "")) if rh.get("retry-after") else None
            except ValueError:
                ra = None
            if challenge:
                title = page_title(text)
                reason = f"challenge page ({title})" if title else "challenge page"
            else:
                reason = f"HTTP {status}"
            self._register_block(host, status, ra, reason)
            return FetchResult(url, status, None, blocked=True, error=reason)
        if status >= 400:
            return FetchResult(url, status, None, error=f"HTTP {status}")
        if as_json:
            try:
                data = json.loads(text)
            except ValueError:
                return FetchResult(url, status, None, error="invalid JSON")
        else:
            data = text
        self.host_state(host).failures = 0
        self.cache.put(url, status, data)
        return FetchResult(url, status, data)
