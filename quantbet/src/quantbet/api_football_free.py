"""API-Football (api-sports.io) free tier - Tier 2, budgeted on-demand enrichment.

* ``GET https://v3.football.api-sports.io/fixtures?live=all`` (header
  ``x-apisports-key``) - one call lists every live fixture.
* ``GET /fixtures/statistics?fixture={id}`` - "Corner Kicks", "Shots on Goal",
  "Total Shots", "Shots insidebox", "Ball Possession", "Red Cards",
  "expected_goals" (when provided).

Free plan: 100 requests/day, 10/minute. This adapter enforces a **strict daily
budget** (default: use at most 90 of 100) tracked in SQLite per UTC day and
synchronised with the ``x-ratelimit-requests-remaining`` response header. It is
only called for matches in minute 55-85 that Tier 1 has flagged as interesting
(favourite level/trailing, high corner pace, red card) and is **off when
``API_FOOTBALL_KEY`` is not set**.

Caveat: API-Football's free plan restricts the seasons available (reported
since 2026 to exclude current-season fixtures for some users). If the API
answers with an ``errors`` object (plan/season/limit) the adapter disables
itself until the next UTC day and logs why - ESPN keeps running.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .engine import PreMatchRatings
from .models import LiveMatchState
from .poller import team_similarity

log = logging.getLogger(__name__)

BASE = "https://v3.football.api-sports.io"

SCHEMA = """
CREATE TABLE IF NOT EXISTS apifootball_calls (
    ts REAL NOT NULL,
    day TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    status INTEGER,
    remaining INTEGER
);
CREATE INDEX IF NOT EXISTS ix_apif_day ON apifootball_calls(day);
"""


def utc_day(ts: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if ts is None else ts, timezone.utc).strftime("%Y-%m-%d")


class ApiFootballBudget:
    """Strict per-UTC-day call budget persisted in SQLite."""

    def __init__(self, db_path: Path | str = ":memory:", daily_limit: int = 100, use_limit: int = 90) -> None:
        if use_limit > daily_limit:
            raise ValueError("use_limit must be <= daily_limit")
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.daily_limit = daily_limit
        self.use_limit = use_limit

    def used_today(self, now: float | None = None) -> int:
        """Calls spent today: max(own count, limit - last reported remaining)."""
        day = utc_day(now)
        n = int(self.db.execute("SELECT COUNT(*) FROM apifootball_calls WHERE day = ?", (day,)).fetchone()[0])
        row = self.db.execute(
            "SELECT remaining FROM apifootball_calls WHERE day = ? AND remaining IS NOT NULL ORDER BY ts DESC LIMIT 1", (day,)
        ).fetchone()
        if row is not None:
            n = max(n, self.daily_limit - int(row[0]))
        return n

    def remaining(self, now: float | None = None) -> int:
        return max(0, self.use_limit - self.used_today(now))

    def can_spend(self, n: int = 1, now: float | None = None) -> bool:
        return self.used_today(now) + n <= self.use_limit

    def record(self, endpoint: str, status: int | None, remaining: int | None, now: float | None = None) -> None:
        ts = time.time() if now is None else now
        self.db.execute(
            "INSERT INTO apifootball_calls(ts, day, endpoint, status, remaining) VALUES (?,?,?,?,?)",
            (ts, utc_day(ts), endpoint, status, remaining),
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()


@dataclass(frozen=True, slots=True)
class ApiFootballConfig:
    daily_limit: int = 100
    use_limit: int = 90
    min_minute: float = 55.0
    max_minute: float = 85.0
    live_ttl_s: float = 300.0  # one live=all call per 5 min at most (only while something is flagged)
    stats_ttl_s: float = 300.0  # per fixture
    min_gap_s: float = 7.0  # free plan: 10 requests / minute
    corner_pace_mult: float = 1.3  # corners/min >= 1.3 x expected -> "high corner pace"
    favourite_supremacy: float = 0.6
    match_threshold: float = 75.0


def interest_flags(state: LiveMatchState, ratings: PreMatchRatings | None, cfg: ApiFootballConfig | None = None) -> list[str]:
    """Why a match deserves a (budgeted) Tier-2 call; empty list = not interesting."""
    c = cfg or ApiFootballConfig()
    if not (c.min_minute <= state.minute <= c.max_minute) or state.status in ("halftime", "ended"):
        return []
    flags: list[str] = []
    if ratings is not None and abs(ratings.goal_supremacy) >= c.favourite_supremacy:
        diff = (state.home_goals - state.away_goals) * (1 if ratings.goal_supremacy > 0 else -1)
        if diff <= 0:
            flags.append("fav_level_or_trailing")
    exp_rate = (ratings.corner_total if ratings else 10.0) / 90.0
    if state.minute > 0 and (state.home_corners + state.away_corners) / state.minute >= c.corner_pace_mult * exp_rate:
        flags.append("high_corner_pace")
    if state.home_red_cards or state.away_red_cards:
        flags.append("red_card")
    return flags


def parse_live_fixtures(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``fixtures?live=all`` -> [{id, home, away, league, elapsed, extra, short, home_goals, away_goals}]."""
    out = []
    for r in payload.get("response") or []:
        try:
            fx, st = r["fixture"], r["fixture"].get("status") or {}
            out.append({
                "id": int(fx["id"]),
                "home": r["teams"]["home"]["name"],
                "away": r["teams"]["away"]["name"],
                "league": (r.get("league") or {}).get("name", ""),
                "elapsed": st.get("elapsed"),
                "extra": st.get("extra"),
                "short": st.get("short"),
                "home_goals": (r.get("goals") or {}).get("home"),
                "away_goals": (r.get("goals") or {}).get("away"),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _val(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().rstrip("%")
    try:
        return float(s)
    except ValueError:
        return None


def parse_fixture_statistics(payload: Mapping[str, Any], home: str, away: str) -> dict[str, tuple[float | None, float | None]]:
    """``fixtures/statistics`` -> {type: (home, away)} (teams matched by name, else order)."""
    teams = payload.get("response") or []
    if len(teams) < 2:
        return {}
    a0, a1 = teams[0], teams[1]
    n0 = str((a0.get("team") or {}).get("name") or "")
    if team_similarity(n0, away) > team_similarity(n0, home):
        a0, a1 = a1, a0
    h = {s.get("type"): _val(s.get("value")) for s in a0.get("statistics") or []}
    a = {s.get("type"): _val(s.get("value")) for s in a1.get("statistics") or []}
    return {k: (h.get(k), a.get(k)) for k in set(h) | set(a)}


class ApiFootballFree:
    """Budgeted Tier-2 enrichment. ``http_get`` (async url, headers -> (status, headers, json)) is injectable."""

    def __init__(
        self,
        api_key: str | None,
        db_path: Path | str = ":memory:",
        cfg: ApiFootballConfig | None = None,
        http_get: Callable[[str, dict[str, str]], Any] | None = None,
        user_agent: str = "quantbet",
    ) -> None:
        self.key = api_key or None
        self.cfg = cfg or ApiFootballConfig()
        self.budget = ApiFootballBudget(db_path, self.cfg.daily_limit, self.cfg.use_limit)
        self.http_get = http_get
        self.user_agent = user_agent
        self.disabled_until_day: str | None = None
        self.disabled_reason = ""
        self._live: tuple[float, list[dict[str, Any]]] | None = None
        self._stats: dict[int, tuple[float, dict[str, tuple[float | None, float | None]]]] = {}
        self._last_call = 0.0
        self._lock = asyncio.Lock()
        self._session: Any = None

    @property
    def enabled(self) -> bool:
        return bool(self.key) and (self.disabled_until_day is None or self.disabled_until_day != utc_day())

    async def _default_get(self, url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], Any]:
        import aiohttp

        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        async with self._session.get(url, headers=headers) as r:
            data = await r.json(content_type=None)
            return r.status, {k.lower(): v for k, v in r.headers.items()}, data

    async def _call(self, path: str, now: float | None = None) -> Any | None:
        if not self.enabled:
            return None
        if not self.budget.can_spend(1, now):
            log.info("api-football daily budget exhausted", extra={"used": self.budget.used_today(now), "limit": self.cfg.use_limit})
            return None
        async with self._lock:
            wait = self._last_call + self.cfg.min_gap_s - time.time()
            if wait > 0 and now is None:
                await asyncio.sleep(wait)
            self._last_call = time.time()
            headers = {"x-apisports-key": self.key or "", "User-Agent": self.user_agent, "Accept": "application/json"}
            try:
                status, rh, data = await (self.http_get or self._default_get)(BASE + path, headers)
            except Exception as exc:  # noqa: BLE001
                log.info("api-football request failed", extra={"error": repr(exc)})
                self.budget.record(path.split("?")[0], None, None, now)  # count it: may have been billed
                return None
        rem = rh.get("x-ratelimit-requests-remaining")
        self.budget.record(path.split("?")[0], status, int(rem) if rem and str(rem).isdigit() else None, now)
        errors = (data or {}).get("errors") if isinstance(data, dict) else None
        if status in (401, 403, 429, 499) or errors:
            self.disabled_until_day = utc_day(now)
            self.disabled_reason = f"HTTP {status}: {errors}"
            log.warning("api-football refused/limited - disabled for the rest of the UTC day", extra={"reason": self.disabled_reason})
            return None
        return data if status == 200 else None

    async def live_fixtures(self, now: float | None = None) -> list[dict[str, Any]]:
        t = time.time() if now is None else now
        if self._live and t - self._live[0] < self.cfg.live_ttl_s:
            return self._live[1]
        data = await self._call("/fixtures?live=all", now)
        if data is None:
            return self._live[1] if self._live else []
        self._live = (t, parse_live_fixtures(data))
        return self._live[1]

    def find_fixture(self, st: LiveMatchState, fixtures: list[dict[str, Any]]) -> dict[str, Any] | None:
        best, score = None, 0.0
        for f in fixtures:
            s = min(team_similarity(st.home, f["home"]), team_similarity(st.away, f["away"]))
            if s > score:
                best, score = f, s
        return best if score >= self.cfg.match_threshold else None

    async def statistics(self, fixture_id: int, home: str, away: str, now: float | None = None) -> dict[str, tuple[float | None, float | None]] | None:
        t = time.time() if now is None else now
        hit = self._stats.get(fixture_id)
        if hit and t - hit[0] < self.cfg.stats_ttl_s:
            return hit[1]
        data = await self._call(f"/fixtures/statistics?fixture={fixture_id}", now)
        if data is None:
            return hit[1] if hit else None
        stats = parse_fixture_statistics(data, home, away)
        self._stats[fixture_id] = (t, stats)
        return stats

    async def enrich(self, st: LiveMatchState, ratings: PreMatchRatings | None, now: float | None = None) -> LiveMatchState:
        """If flagged and within budget: add xG, shots inside box, (max) red cards. Mutates ``st``."""
        if not self.enabled or not interest_flags(st, ratings, self.cfg):
            return st
        fx = self.find_fixture(st, await self.live_fixtures(now))
        if fx is None:
            return st
        stats = await self.statistics(fx["id"], fx["home"], fx["away"], now)
        if not stats:
            return st
        xg = stats.get("expected_goals")
        if xg and xg[0] is not None and xg[1] is not None:
            st.home_xg, st.away_xg = xg
        ib = stats.get("Shots insidebox")
        if ib and ib[0] is not None and ib[1] is not None:
            st.home_shots_inside_box, st.away_shots_inside_box = int(ib[0]), int(ib[1])
        rc = stats.get("Red Cards")
        if rc:
            st.home_red_cards = max(st.home_red_cards, int(rc[0] or 0))
            st.away_red_cards = max(st.away_red_cards, int(rc[1] or 0))
        ck = stats.get("Corner Kicks")
        if ck and not st.stats_available and ck[0] is not None and ck[1] is not None:
            st.home_corners, st.away_corners, st.stats_available = int(ck[0]), int(ck[1]), True
        st.enrichments = (*st.enrichments, "api_football")
        return st

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
        self.budget.close()


__all__ = [
    "ApiFootballBudget",
    "ApiFootballConfig",
    "ApiFootballFree",
    "interest_flags",
    "parse_live_fixtures",
    "parse_fixture_statistics",
    "utc_day",
]
