"""Asynchronous data ingestion: provider adapters, rate limiting, event matching, queue.

Pipeline::

    adapters (REST, per-provider token bucket + backoff + ETag)
        -> normalise to OddsSnapshot / MatchState
        -> EventMatcher assigns a canonical event_key across providers
        -> asyncio.Queue[PollerMessage]  -> consumer (engine) in main.py

All network access is read-only against third-party data APIs. Nothing here
talks to a bookmaker account.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
import unicodedata
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from rapidfuzz import fuzz

from .models import EventInfo, MarketType, MatchState, OddsSnapshot, Selection
from .youth import youth_compatible

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rate limiting & backoff
# ---------------------------------------------------------------------------
class TokenBucket:
    """Async token bucket: ``rate`` tokens/second, burst ``capacity``."""

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self.rate = rate
        self.capacity = capacity if capacity is not None else max(1.0, rate)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        """Wait until ``tokens`` are available, then consume them."""
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                await asyncio.sleep((tokens - self._tokens) / self.rate)


@dataclass(frozen=True, slots=True)
class Backoff:
    """Exponential backoff with full jitter."""

    base: float = 0.5
    factor: float = 2.0
    max_delay: float = 30.0
    max_retries: int = 4

    def delay(self, attempt: int) -> float:
        """Delay before retry number ``attempt`` (0-based)."""
        return random.uniform(0, min(self.max_delay, self.base * self.factor**attempt))


class ProviderError(RuntimeError):
    """Non-retryable provider error (bad credentials, bad request, quota)."""


# ---------------------------------------------------------------------------
# Team / event matching
# ---------------------------------------------------------------------------
_STOP = {"fc", "cf", "afc", "sc", "ac", "as", "ssc", "cd", "ud", "sv", "fk", "sk", "if", "bk", "club", "de", "the", "calcio"}
_ALIASES = {
    "man utd": "manchester united",
    "man united": "manchester united",
    "man city": "manchester city",
    "spurs": "tottenham hotspur",
    "tottenham": "tottenham hotspur",
    "wolves": "wolverhampton wanderers",
    "inter": "internazionale",
    "inter milan": "internazionale",
    "psg": "paris saint germain",
    "paris sg": "paris saint germain",
    "bayern munich": "bayern munchen",
    "atletico madrid": "atletico",
    "atl madrid": "atletico",
    "nottm forest": "nottingham forest",
    "newcastle": "newcastle united",
    "west ham": "west ham united",
    "leeds": "leeds united",
    "brighton": "brighton hove albion",
    "brighton and hove albion": "brighton hove albion",
}


def normalize_team(name: str) -> str:
    """Lower-case, strip accents/punctuation/club suffixes and apply aliases."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = _ALIASES.get(s, s)
    tokens = [t for t in s.split() if t not in _STOP]
    s = " ".join(tokens) or s
    return _ALIASES.get(s, s)


def team_similarity(a: str, b: str) -> float:
    """0-100 similarity of two team names after normalisation.

    Youth / reserve guard (v4.5): if exactly one name carries a youth / reserve marker (``U21``, ``U23``,
    ``Jong``, ``Reserves``, `` B``, `` II`` ... - :func:`quantbet.youth.youth_reserve_hit`), or both do with
    different markers, the score is 0. ``token_set_ratio("arsenal u21", "arsenal")`` is 100, so without
    the guard a youth side was matched to the senior club's ratings / prices / events.
    """
    if not youth_compatible(a, b):
        return 0.0
    na, nb = normalize_team(a), normalize_team(b)
    if na == nb:
        return 100.0
    return float(max(fuzz.token_set_ratio(na, nb), fuzz.ratio(na, nb)))


class EventMatcher:
    """Assigns one canonical ``event_key`` to the same fixture across providers.

    Two fixtures match if both team names score >= ``threshold`` and the
    kick-off times are within ``max_kickoff_diff`` (when both known).
    """

    def __init__(self, threshold: float = 85.0, max_kickoff_diff: timedelta = timedelta(minutes=20)) -> None:
        self.threshold = threshold
        self.max_kickoff_diff = max_kickoff_diff
        self.events: dict[str, EventInfo] = {}
        self._by_provider: dict[tuple[str, str], str] = {}

    @staticmethod
    def _make_key(home: str, away: str, ko: datetime | None) -> str:
        day = ko.strftime("%Y%m%d") if ko else "live"
        return f"{day}:{normalize_team(home).replace(' ', '_')}:{normalize_team(away).replace(' ', '_')}"

    def match(
        self,
        provider: str,
        provider_event_id: str,
        home: str,
        away: str,
        league: str,
        commence_time: datetime | None,
        is_live: bool = False,
    ) -> str:
        """Return (and remember) the canonical key for a provider's event."""
        pk = (provider, provider_event_id)
        if pk in self._by_provider:
            key = self._by_provider[pk]
            self.events[key].is_live = self.events[key].is_live or is_live
            return key
        best, best_score = None, 0.0
        for key, ev in self.events.items():
            if commence_time and ev.commence_time:
                if abs(commence_time - ev.commence_time) > self.max_kickoff_diff:
                    continue
            sc = min(team_similarity(home, ev.home), team_similarity(away, ev.away))
            if sc > best_score:
                best, best_score = key, sc
        if best is not None and best_score >= self.threshold:
            key = best
        else:
            key = self._make_key(home, away, commence_time)
            self.events[key] = EventInfo(key, home, away, league, commence_time, is_live)
        ev = self.events[key]
        ev.provider_ids[provider] = provider_event_id
        if ev.commence_time is None:
            ev.commence_time = commence_time
        self._by_provider[pk] = key
        return key

    def lookup(self, provider: str, provider_event_id: str) -> str | None:
        """Canonical key previously assigned to a provider's event id."""
        return self._by_provider.get((provider, provider_event_id))


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class PollerMessage:
    """Item placed on the engine queue."""

    kind: str  # "odds" | "stats"
    odds: list[OddsSnapshot] = field(default_factory=list)
    states: list[MatchState] = field(default_factory=list)
    provider: str = ""
    received_at: float = field(default_factory=time.time)


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _price(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x > 1.0 else None


class BaseAdapter(ABC):
    """Common HTTP plumbing: token bucket, timeouts, retries/backoff, ETag cache."""

    name: str = "base"
    kind: str = "odds"  # "odds" or "stats"

    def __init__(
        self,
        session: aiohttp.ClientSession,
        rate_per_s: float,
        interval_s: float,
        timeout_s: float = 4.0,
        backoff: Backoff | None = None,
    ) -> None:
        self.session = session
        self.bucket = TokenBucket(rate_per_s, max(1.0, rate_per_s * 2))
        self.interval_s = interval_s
        self.timeout = aiohttp.ClientTimeout(total=timeout_s)
        self.backoff = backoff or Backoff()
        self._etags: dict[str, tuple[str, Any]] = {}
        self.last_headers: dict[str, str] = {}

    async def get_json(
        self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> Any:
        """GET JSON with rate limiting, ETag/If-None-Match, retries and backoff."""
        cache_key = url + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()) if "key" not in k.lower() and "token" not in k.lower())
        hdrs = dict(headers or {})
        cached = self._etags.get(cache_key)
        if cached:
            hdrs["If-None-Match"] = cached[0]
        for attempt in range(self.backoff.max_retries + 1):
            await self.bucket.acquire()
            t0 = time.perf_counter()
            try:
                async with self.session.get(url, params=params, headers=hdrs, timeout=self.timeout) as resp:
                    self.last_headers = {k.lower(): v for k, v in resp.headers.items()}
                    latency_ms = (time.perf_counter() - t0) * 1000
                    if resp.status == 304 and cached:
                        return cached[1]
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        if etag := resp.headers.get("ETag"):
                            self._etags[cache_key] = (etag, data)
                        log.debug("fetch ok", extra={"provider": self.name, "url": url, "ms": round(latency_ms, 1)})
                        return data
                    if resp.status in (401, 403):
                        raise ProviderError(f"{self.name}: HTTP {resp.status} (check credentials/plan)")
                    if resp.status == 429 or resp.status >= 500:
                        retry_after = resp.headers.get("Retry-After")
                        delay = float(retry_after) if retry_after and retry_after.isdigit() else self.backoff.delay(attempt)
                        log.warning("retryable status", extra={"provider": self.name, "status": resp.status, "delay": delay})
                        await asyncio.sleep(delay)
                        continue
                    body = await resp.text()
                    raise ProviderError(f"{self.name}: HTTP {resp.status}: {body[:200]}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                delay = self.backoff.delay(attempt)
                log.warning("network error", extra={"provider": self.name, "error": repr(exc), "delay": delay})
                await asyncio.sleep(delay)
        raise ProviderError(f"{self.name}: retries exhausted for {url}")

    @abstractmethod
    async def fetch(self) -> list[OddsSnapshot] | list[MatchState]:
        """Fetch one round of data from the provider."""


def _snap(
    provider: str, bookmaker: str, eid: str, home: str, away: str, league: str, ko: datetime | None,
    market: MarketType, line: float | None, sel: Selection, price: float, live: bool,
    ts: float | None = None, liquidity: float | None = None, link: str | None = None,
) -> OddsSnapshot:
    return OddsSnapshot(
        provider, bookmaker.lower(), eid, home, away, league, ko, market, line, sel, price, live,
        ts if ts is not None else time.time(), liquidity, link,
    )


class OddsAPIAdapter(BaseAdapter):
    """The-Odds-API v4 (https://the-odds-api.com/liveapi/guides/v4/).

    ``GET /v4/sports/{sport}/odds?bookmakers=...&markets=h2h,spreads,totals&oddsFormat=decimal``.
    Quota cost = markets x regions (``bookmakers`` counts as 1 region per 10 books);
    remaining credits are read from the ``x-requests-remaining`` header.
    Note: The-Odds-API lists Pinnacle and Betfair Exchange (``pinnacle``,
    ``betfair_ex_eu``) but **no European bet365 key** (only ``bet365_au`` for
    AFL/NRL), so it serves as the sharp benchmark feed here.
    """

    name = "the_odds_api"
    BASE = "https://api.the-odds-api.com/v4"

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        sports: Sequence[str],
        bookmakers: Sequence[str] = ("pinnacle", "betfair_ex_eu"),
        markets: Sequence[str] = ("h2h", "spreads", "totals"),
        **kw: Any,
    ) -> None:
        super().__init__(session, **kw)
        self.api_key = api_key
        self.sports = list(sports)
        self.bookmakers = list(bookmakers)
        self.markets = list(markets)

    async def _fetch_sport(self, sport: str) -> list[OddsSnapshot]:
        data = await self.get_json(
            f"{self.BASE}/sports/{sport}/odds",
            params={
                "apiKey": self.api_key,
                "bookmakers": ",".join(self.bookmakers),
                "markets": ",".join(self.markets),
                "oddsFormat": "decimal",
                "dateFormat": "iso",
            },
        )
        out: list[OddsSnapshot] = []
        now = datetime.now(timezone.utc)
        for ev in data or []:
            home, away = ev["home_team"], ev["away_team"]
            ko = _parse_dt(ev.get("commence_time"))
            live = bool(ko and ko <= now)
            league = ev.get("sport_title", sport)
            for bm in ev.get("bookmakers", []):
                for mkt in bm.get("markets", []):
                    ts = _parse_dt(mkt.get("last_update"))
                    ts_f = ts.timestamp() if ts else None
                    for o in mkt.get("outcomes", []):
                        price = _price(o.get("price"))
                        if price is None:
                            continue
                        name = o.get("name", "")
                        key = mkt.get("key")
                        if key == "h2h":
                            sel = Selection.DRAW if name.lower() == "draw" else (
                                Selection.HOME if name == home else Selection.AWAY if name == away else None)
                            if sel:
                                out.append(_snap(self.name, bm["key"], ev["id"], home, away, league, ko, MarketType.H2H, None, sel, price, live, ts_f))
                        elif key == "spreads" and o.get("point") is not None:
                            pt = float(o["point"])
                            if name == home:
                                out.append(_snap(self.name, bm["key"], ev["id"], home, away, league, ko, MarketType.ASIAN_HANDICAP, pt, Selection.HOME, price, live, ts_f))
                            elif name == away:
                                out.append(_snap(self.name, bm["key"], ev["id"], home, away, league, ko, MarketType.ASIAN_HANDICAP, -pt, Selection.AWAY, price, live, ts_f))
                        elif key == "totals" and o.get("point") is not None:
                            sel = Selection.OVER if name.lower() == "over" else Selection.UNDER
                            out.append(_snap(self.name, bm["key"], ev["id"], home, away, league, ko, MarketType.TOTAL_GOALS, float(o["point"]), sel, price, live, ts_f))
        log.info("the_odds_api fetched", extra={"sport": sport, "snapshots": len(out), "remaining": self.last_headers.get("x-requests-remaining")})
        return out

    async def fetch(self) -> list[OddsSnapshot]:
        results = await asyncio.gather(*(self._fetch_sport(s) for s in self.sports), return_exceptions=True)
        out: list[OddsSnapshot] = []
        for r in results:
            if isinstance(r, BaseException):
                log.error("sport fetch failed", extra={"provider": self.name, "error": repr(r)})
            else:
                out.extend(r)
        return out


_ODDS_IO_MARKETS = {
    "ml": MarketType.H2H,
    "spread": MarketType.ASIAN_HANDICAP,
    "totals": MarketType.TOTAL_GOALS,
    "corners totals": MarketType.TOTAL_CORNERS,
}


def parse_odds_api_io_markets(
    provider: str, bookie: str, eid: str, home: str, away: str, league: str, ko: datetime | None,
    live: bool, markets: list[dict[str, Any]], link: str | None = None,
) -> list[OddsSnapshot]:
    """Normalise Odds-API.io market objects (REST ``/odds`` or WebSocket ``updated``)."""
    out: list[OddsSnapshot] = []
    for m in markets:
        mt = _ODDS_IO_MARKETS.get(str(m.get("name", "")).lower())
        if mt is None:
            continue
        ts = _parse_dt(m.get("updatedAt"))
        ts_f = ts.timestamp() if ts else None
        for row in m.get("odds", []):
            liq = _price(row.get("max"))
            if mt is MarketType.H2H:
                pairs = ((Selection.HOME, "home"), (Selection.DRAW, "draw"), (Selection.AWAY, "away"))
                line = None
            elif mt is MarketType.ASIAN_HANDICAP:
                if row.get("hdp") is None:
                    continue
                pairs = ((Selection.HOME, "home"), (Selection.AWAY, "away"))
                line = float(row["hdp"])  # home handicap
            else:
                if row.get("hdp") is None:
                    continue
                pairs = ((Selection.OVER, "over"), (Selection.UNDER, "under"))
                line = float(row["hdp"])
            for sel, fld in pairs:
                price = _price(row.get(fld))
                if price is not None:
                    out.append(_snap(provider, bookie, eid, home, away, league, ko, mt, line, sel, price, live, ts_f, liq, link))
    return out


class OddsApiIoAdapter(BaseAdapter):
    """Odds-API.io v3 REST (https://docs.odds-api.io). Bet365 + Betfair Exchange.

    Lists events via ``GET /events?sport=football&status=pending,live`` (refreshed
    every ``events_refresh_s``) and prices them in batches of 10 with
    ``GET /odds/multi`` (1 request per batch). ``Spread.hdp`` is the home handicap.
    """

    name = "odds_api_io"
    BASE = "https://api.odds-api.io/v3"

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        bookmakers: Sequence[str] = ("Bet365", "Betfair Exchange"),
        markets: Sequence[str] = ("ML", "Spread", "Totals", "Corners Totals"),
        leagues: Sequence[str] | None = None,
        live_only: bool = False,
        events_refresh_s: float = 120.0,
        max_events: int = 60,
        **kw: Any,
    ) -> None:
        super().__init__(session, **kw)
        self.api_key = api_key
        self.bookmakers = list(bookmakers)
        self.markets = list(markets)
        self.leagues = list(leagues) if leagues else None
        self.live_only = live_only
        self.events_refresh_s = events_refresh_s
        self.max_events = max_events
        self._events: list[dict[str, Any]] = []
        self._events_at = 0.0

    async def _refresh_events(self) -> None:
        params: dict[str, Any] = {
            "apiKey": self.api_key,
            "sport": "football",
            "status": "live" if self.live_only else "pending,live",
            "to": (datetime.now(timezone.utc) + timedelta(hours=36)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bookmaker": self.bookmakers[0],
        }
        evs = await self.get_json(f"{self.BASE}/events", params=params)
        if self.leagues:
            evs = [e for e in evs if e.get("league", {}).get("slug") in self.leagues]
        evs.sort(key=lambda e: (e.get("status") != "live", e.get("date", "")))
        self._events = evs[: self.max_events]
        self._events_at = time.time()

    async def _fetch_batch(self, ids: list[str]) -> list[OddsSnapshot]:
        data = await self.get_json(
            f"{self.BASE}/odds/multi",
            params={
                "apiKey": self.api_key,
                "eventIds": ",".join(ids),
                "bookmakers": ",".join(self.bookmakers),
                "markets": ",".join(self.markets),
            },
        )
        out: list[OddsSnapshot] = []
        for ev in data or []:
            live = ev.get("status") == "live"
            league = (ev.get("league") or {}).get("name", "")
            ko = _parse_dt(ev.get("date"))
            for bookie, mkts in (ev.get("bookmakers") or {}).items():
                link = (ev.get("urls") or {}).get(bookie)
                out.extend(parse_odds_api_io_markets(self.name, bookie, str(ev["id"]), ev["home"], ev["away"], league, ko, live, mkts, link))
        return out

    async def fetch(self) -> list[OddsSnapshot]:
        if time.time() - self._events_at > self.events_refresh_s or not self._events:
            await self._refresh_events()
        ids = [str(e["id"]) for e in self._events]
        batches = [ids[i : i + 10] for i in range(0, len(ids), 10)]
        results = await asyncio.gather(*(self._fetch_batch(b) for b in batches), return_exceptions=True)
        out: list[OddsSnapshot] = []
        for r in results:
            if isinstance(r, BaseException):
                log.error("batch failed", extra={"provider": self.name, "error": repr(r)})
            else:
                out.extend(r)
        return out


class BetsAPIAdapter(BaseAdapter):
    """BetsAPI / b365api Bet365 feed (https://betsapi.com/docs/).

    In-play: ``GET /v1/bet365/inplay_filter?sport_id=1`` then
    ``GET /v3/bet365/prematch?FI=...`` per fixture for structured Asian
    lines / goals / corners odds. TODO(credentials): requires a Bet365 API
    package token (``BETSAPI_TOKEN``). The section/field names parsed below
    follow BetsAPI's documented prematch JSON; verify against a sample response
    from your plan, as BetsAPI occasionally renames market keys.
    """

    name = "betsapi"
    BASE = "https://api.betsapi.com"

    _SECTIONS: dict[tuple[str, str], MarketType] = {
        ("asian_lines", "asian_handicap"): MarketType.ASIAN_HANDICAP,
        ("asian_lines", "goal_line"): MarketType.TOTAL_GOALS,
        ("goals", "goals_over_under"): MarketType.TOTAL_GOALS,
        ("corners", "asian_total_corners"): MarketType.TOTAL_CORNERS,
        ("corners", "corners_2_way"): MarketType.TOTAL_CORNERS,
        ("main", "full_time_result"): MarketType.H2H,
    }

    def __init__(self, session: aiohttp.ClientSession, token: str, max_fixtures: int = 40, **kw: Any) -> None:
        super().__init__(session, **kw)
        self.token = token
        self.max_fixtures = max_fixtures

    def _parse_prematch(self, fi: str, home: str, away: str, league: str, ko: datetime | None, live: bool, res: dict[str, Any]) -> list[OddsSnapshot]:
        out: list[OddsSnapshot] = []
        for (sec, mkey), mt in self._SECTIONS.items():
            market = ((res.get(sec) or {}).get("sp") or {}).get(mkey) or {}
            for o in market.get("odds", []):
                price = _price(o.get("odds"))
                if price is None:
                    continue
                header = str(o.get("header", "")).lower()
                if mt is MarketType.H2H:
                    sel = {"1": Selection.HOME, "x": Selection.DRAW, "2": Selection.AWAY}.get(str(o.get("name", "")).lower())
                    if sel:
                        out.append(_snap(self.name, "bet365", fi, home, away, league, ko, mt, None, sel, price, live))
                    continue
                raw_line = o.get("handicap") or o.get("name")
                try:  # split lines like "2.0,2.5" -> 2.25
                    parts = [float(x) for x in str(raw_line).replace(" ", "").split(",")]
                    line = sum(parts) / len(parts)
                except ValueError:
                    continue
                if mt is MarketType.ASIAN_HANDICAP:
                    if header == "1":
                        out.append(_snap(self.name, "bet365", fi, home, away, league, ko, mt, line, Selection.HOME, price, live))
                    elif header == "2":
                        out.append(_snap(self.name, "bet365", fi, home, away, league, ko, mt, -line, Selection.AWAY, price, live))
                else:
                    sel = Selection.OVER if header.startswith("over") else Selection.UNDER if header.startswith("under") else None
                    if sel:
                        out.append(_snap(self.name, "bet365", fi, home, away, league, ko, mt, line, sel, price, live))
        return out

    async def fetch(self) -> list[OddsSnapshot]:
        data = await self.get_json(f"{self.BASE}/v1/bet365/inplay_filter", params={"token": self.token, "sport_id": 1})
        fixtures = (data or {}).get("results", [])[: self.max_fixtures]

        async def one(fx: dict[str, Any]) -> list[OddsSnapshot]:
            fi = str(fx.get("id"))
            r = await self.get_json(f"{self.BASE}/v3/bet365/prematch", params={"token": self.token, "FI": fi})
            res = ((r or {}).get("results") or [{}])[0]
            ko = datetime.fromtimestamp(int(fx["time"]), timezone.utc) if fx.get("time") else None
            return self._parse_prematch(fi, fx["home"]["name"], fx["away"]["name"], (fx.get("league") or {}).get("name", ""), ko, True, res)

        out: list[OddsSnapshot] = []
        for r in await asyncio.gather(*(one(f) for f in fixtures), return_exceptions=True):
            if isinstance(r, BaseException):
                log.error("betsapi fixture failed", extra={"error": repr(r)})
            else:
                out.extend(r)
        return out


class StatsTracker:
    """Turns cumulative live counters into rolling 10-minute counts per team."""

    def __init__(self, window_min: float = 10.0) -> None:
        self.window_min = window_min
        self._hist: dict[tuple[str, str], deque[tuple[float, float]]] = {}

    def reset(self, event_key: str | None = None) -> None:
        """Drop the rolling history of one match (or all) - used by the post-gap delta reset."""
        for k in [k for k in self._hist if event_key is None or k[0] == event_key]:
            del self._hist[k]

    def rolling(self, event_key: str, stat: str, minute: float, cumulative: float | None) -> float | None:
        """Count in the last ``window_min`` match minutes (scaled if history is shorter)."""
        if cumulative is None:
            return None
        dq = self._hist.setdefault((event_key, stat), deque())
        if dq and minute < dq[-1][0]:
            dq.clear()
        dq.append((minute, cumulative))
        while len(dq) > 1 and minute - dq[1][0] >= self.window_min:
            dq.popleft()
        m0, c0 = dq[0]
        span = minute - m0
        if span <= 0:
            return cumulative * self.window_min / max(minute, self.window_min) if minute > 0 else None
        return (cumulative - c0) * self.window_min / max(span, 1e-9) if span < self.window_min else cumulative - c0


class APIFootballAdapter(BaseAdapter):
    """API-Football v3 live stats (https://www.api-football.com/documentation-v3).

    ``GET /fixtures?live=all`` (score, minute, red cards via events) and
    ``GET /fixtures/statistics?fixture=ID`` (corners, shots on goal). API-Football
    does not provide "dangerous attacks"; momentum then uses shots on target only.
    """

    name = "api_football"
    kind = "stats"
    BASE = "https://v3.football.api-sports.io"

    def __init__(self, session: aiohttp.ClientSession, api_key: str, matcher: EventMatcher, league_ids: Sequence[int] | None = None, **kw: Any) -> None:
        super().__init__(session, **kw)
        self.api_key = api_key
        self.matcher = matcher
        self.league_ids = set(league_ids or [])
        self.tracker = StatsTracker()

    @property
    def _headers(self) -> dict[str, str]:
        return {"x-apisports-key": self.api_key}

    async def fetch(self) -> list[MatchState]:
        data = await self.get_json(f"{self.BASE}/fixtures", params={"live": "all"}, headers=self._headers)
        fixtures = [f for f in (data or {}).get("response", []) if not self.league_ids or f["league"]["id"] in self.league_ids]

        async def one(f: dict[str, Any]) -> MatchState:
            fx, teams, goals = f["fixture"], f["teams"], f["goals"]
            key = self.matcher.match(self.name, str(fx["id"]), teams["home"]["name"], teams["away"]["name"], f["league"]["name"], _parse_dt(fx.get("date")), True)
            st = fx.get("status") or {}
            minute = float(st.get("elapsed") or 0) + float(st.get("extra") or 0)
            period = 1 if st.get("short") in ("1H", "HT") else 2
            reds = {"home": 0, "away": 0}
            for e in f.get("events") or []:
                if e.get("type") == "Card" and "Red" in str(e.get("detail")):
                    reds["home" if e["team"]["id"] == teams["home"]["id"] else "away"] += 1
            stats_resp = await self.get_json(f"{self.BASE}/fixtures/statistics", params={"fixture": fx["id"]}, headers=self._headers)
            vals: dict[str, dict[str, float]] = {"home": {}, "away": {}}
            for block in (stats_resp or {}).get("response", []):
                side = "home" if block["team"]["id"] == teams["home"]["id"] else "away"
                for s in block.get("statistics", []):
                    v = s.get("value")
                    if isinstance(v, str) and v.endswith("%"):
                        v = v[:-1]
                    try:
                        vals[side][s["type"]] = float(v) if v is not None else 0.0
                    except (TypeError, ValueError):
                        continue
            sot_h = self.tracker.rolling(key, "sot_h", minute, vals["home"].get("Shots on Goal"))
            sot_a = self.tracker.rolling(key, "sot_a", minute, vals["away"].get("Shots on Goal"))
            return MatchState(
                key, minute, int(goals["home"] or 0), int(goals["away"] or 0),
                int(vals["home"].get("Corner Kicks", 0)), int(vals["away"].get("Corner Kicks", 0)),
                reds["home"], reds["away"], period, None, None, None, sot_h, sot_a,
                vals["home"].get("expected_goals"), vals["away"].get("expected_goals"),
                vals["home"].get("Ball Possession"),
            )

        out: list[MatchState] = []
        for r in await asyncio.gather(*(one(f) for f in fixtures), return_exceptions=True):
            if isinstance(r, BaseException):
                log.error("api_football fixture failed", extra={"error": repr(r)})
            else:
                out.append(r)
        return out


class SportmonksAdapter(BaseAdapter):
    """Sportmonks Football API v3 in-play (https://docs.sportmonks.com/v3).

    ``GET /v3/football/livescores/inplay?include=participants;scores;statistics;periods``.
    Statistic ``type_id`` values are configurable; defaults follow Sportmonks'
    type list (34 corners, 44 dangerous attacks, 86 shots on target, 83 red
    cards, 45 possession) and should be verified with ``GET /v3/core/types``.
    """

    name = "sportmonks"
    kind = "stats"
    BASE = "https://api.sportmonks.com/v3/football"
    DEFAULT_TYPES = {"corners": 34, "dangerous_attacks": 44, "shots_on_target": 86, "red_cards": 83, "possession": 45}

    def __init__(self, session: aiohttp.ClientSession, token: str, matcher: EventMatcher, type_ids: dict[str, int] | None = None, **kw: Any) -> None:
        super().__init__(session, **kw)
        self.token = token
        self.matcher = matcher
        self.types = type_ids or dict(self.DEFAULT_TYPES)
        self.tracker = StatsTracker()

    async def fetch(self) -> list[MatchState]:
        data = await self.get_json(
            f"{self.BASE}/livescores/inplay",
            params={"api_token": self.token, "include": "participants;scores;statistics;periods;league"},
        )
        out: list[MatchState] = []
        inv = {v: k for k, v in self.types.items()}
        for fx in (data or {}).get("data", []):
            parts = {p["meta"]["location"]: p for p in fx.get("participants", []) if p.get("meta")}
            if "home" not in parts or "away" not in parts:
                continue
            key = self.matcher.match(self.name, str(fx["id"]), parts["home"]["name"], parts["away"]["name"], (fx.get("league") or {}).get("name", ""), _parse_dt(fx.get("starting_at", "").replace(" ", "T") + "Z" if fx.get("starting_at") else None), True)
            goals = {"home": 0, "away": 0}
            for s in fx.get("scores", []):
                if s.get("description") == "CURRENT":
                    goals[s["score"]["participant"]] = int(s["score"]["goals"])
            vals: dict[str, dict[str, float]] = {"home": {}, "away": {}}
            for st in fx.get("statistics", []):
                name = inv.get(st.get("type_id"))
                loc = st.get("location")
                if name and loc in vals:
                    vals[loc][name] = float((st.get("data") or {}).get("value") or 0)
            minute, period = 0.0, 1
            for p in fx.get("periods", []):
                if p.get("ticking"):
                    minute = float(p.get("minutes") or 0) + (45.0 if p.get("sort_order") == 2 and (p.get("minutes") or 0) < 45 else 0.0)
                    period = int(p.get("sort_order") or 1)
            out.append(
                MatchState(
                    key, minute, goals["home"], goals["away"],
                    int(vals["home"].get("corners", 0)), int(vals["away"].get("corners", 0)),
                    int(vals["home"].get("red_cards", 0)), int(vals["away"].get("red_cards", 0)), period, None,
                    self.tracker.rolling(key, "da_h", minute, vals["home"].get("dangerous_attacks")),
                    self.tracker.rolling(key, "da_a", minute, vals["away"].get("dangerous_attacks")),
                    self.tracker.rolling(key, "sot_h", minute, vals["home"].get("shots_on_target")),
                    self.tracker.rolling(key, "sot_a", minute, vals["away"].get("shots_on_target")),
                    possession_home=vals["home"].get("possession"),
                )
            )
        return out


# ---------------------------------------------------------------------------
# WebSocket consumer (Odds-API.io push feed, optional add-on)
# ---------------------------------------------------------------------------
class OddsApiIoWebSocket:
    """Consumer for ``wss://api.odds-api.io/v3/ws`` (WebSocket add-on required).

    Tracks ``seq`` and reconnects with ``lastSeq`` for compacted replay; backs off
    on close code 1013. Event metadata (teams, kick-off) is resolved from the
    REST adapter's event list, since update frames carry only the event id.
    """

    URL = "wss://api.odds-api.io/v3/ws"

    def __init__(self, api_key: str, markets: Sequence[str], rest: OddsApiIoAdapter, out: asyncio.Queue[PollerMessage], matcher: EventMatcher, stop: asyncio.Event) -> None:
        self.api_key = api_key
        self.markets = list(markets)
        self.rest = rest
        self.out = out
        self.matcher = matcher
        self.stop = stop
        self.last_seq: int | None = None

    async def run(self) -> None:
        """Connect/reconnect until ``stop`` is set."""
        import websockets  # local import keeps the dependency optional

        attempt = 0
        while not self.stop.is_set():
            params = f"?apiKey={self.api_key}&markets={','.join(self.markets)}&channels=odds"
            if self.last_seq is not None:
                params += f"&lastSeq={self.last_seq}"
            try:
                async with websockets.connect(self.URL + params, compression="deflate", max_size=2**24) as ws:
                    attempt = 0
                    log.info("odds-api.io websocket connected")
                    async for raw in ws:
                        if self.stop.is_set():
                            break
                        self._handle(json.loads(raw))
            except Exception as exc:  # noqa: BLE001 - reconnect on any failure
                delay = min(30.0, 2.0 * 2**attempt) + random.random()
                attempt += 1
                log.warning("websocket reconnect", extra={"error": repr(exc), "delay": delay})
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

    def _handle(self, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        if seq := msg.get("seq"):
            self.last_seq = max(self.last_seq or 0, int(seq))
        if mtype == "resync_required":
            self.last_seq = None
            return
        if mtype not in ("created", "updated"):
            return
        eid = str(msg.get("id"))
        meta = next((e for e in self.rest._events if str(e.get("id")) == eid), None)
        if meta is None:
            return
        snaps = parse_odds_api_io_markets(
            "odds_api_io_ws", str(msg.get("bookie", "")), eid, meta["home"], meta["away"],
            (meta.get("league") or {}).get("name", ""), _parse_dt(meta.get("date")),
            meta.get("status") == "live", msg.get("markets") or [], msg.get("url"),
        )
        key = self.matcher.match("odds_api_io", eid, meta["home"], meta["away"], (meta.get("league") or {}).get("name", ""), _parse_dt(meta.get("date")), meta.get("status") == "live")
        snaps = [_with_key(s, key) for s in snaps]
        try:
            self.out.put_nowait(PollerMessage("odds", odds=snaps, provider="odds_api_io_ws"))
        except asyncio.QueueFull:
            log.warning("queue full, dropping websocket frame")


def _with_key(s: OddsSnapshot, key: str) -> OddsSnapshot:
    return OddsSnapshot(s.provider, s.bookmaker, s.provider_event_id, s.home, s.away, s.league, s.commence_time, s.market, s.line, s.selection, s.price, s.is_live, s.timestamp, s.liquidity, s.deep_link, key)


# ---------------------------------------------------------------------------
# Poller orchestrator
# ---------------------------------------------------------------------------
class Poller:
    """Runs all adapters concurrently on their own intervals and feeds a queue."""

    def __init__(self, adapters: Sequence[BaseAdapter], matcher: EventMatcher, queue: asyncio.Queue[PollerMessage], stop: asyncio.Event) -> None:
        self.adapters = list(adapters)
        self.matcher = matcher
        self.queue = queue
        self.stop = stop

    def _key_odds(self, provider: str, snaps: list[OddsSnapshot]) -> list[OddsSnapshot]:
        out = []
        for s in snaps:
            key = self.matcher.match(provider, s.provider_event_id, s.home, s.away, s.league, s.commence_time, s.is_live)
            out.append(_with_key(s, key))
        return out

    async def _loop(self, adapter: BaseAdapter) -> None:
        while not self.stop.is_set():
            t0 = time.perf_counter()
            try:
                data = await adapter.fetch()
                if adapter.kind == "stats":
                    msg = PollerMessage("stats", states=list(data), provider=adapter.name)  # type: ignore[arg-type]
                else:
                    msg = PollerMessage("odds", odds=self._key_odds(adapter.name, list(data)), provider=adapter.name)  # type: ignore[arg-type]
                await self.queue.put(msg)
                log.info("poll ok", extra={"provider": adapter.name, "items": len(data), "ms": round((time.perf_counter() - t0) * 1000, 1)})
            except ProviderError as exc:
                log.error("provider error", extra={"provider": adapter.name, "error": str(exc)})
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("unexpected poll failure", extra={"provider": adapter.name})
            elapsed = time.perf_counter() - t0
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=max(0.0, adapter.interval_s - elapsed))
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        """Run until ``stop`` is set; cancels cleanly."""
        tasks = [asyncio.create_task(self._loop(a), name=f"poll:{a.name}") for a in self.adapters]
        try:
            await self.stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
