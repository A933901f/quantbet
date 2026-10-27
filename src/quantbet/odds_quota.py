"""Layer 1 – The-Odds-API free tier (500 credits/month) quota manager.

* Only the quota-costing ``/v4/sports/{sport}/odds`` endpoint is budgeted;
  ``/sports`` and ``/events`` are free per the v4 docs and are used to plan.
* Cost = markets x regions; ``bookmakers=`` with up to 10 books counts as one
  region; empty responses cost nothing (all per the v4 docs).
* Every response is cached in SQLite with its credit cost, and the
  ``x-requests-remaining / x-requests-used`` headers are recorded, so restarts
  never waste credits.
* Pacing: the monthly budget (default 500, minus a reserve) is spread over the
  remaining days; a sport is only refreshed when one of its fixtures kicks off
  inside the priority window (default 3 h – "closing-ish" Pinnacle snapshots),
  and never more often than ``min_refresh_s``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .engine import SharpPrices
from .models import MarketType, OddsSnapshot, Selection
from .polite_http import assert_allowed_url
from .poller import EventMatcher, team_similarity

log = logging.getLogger(__name__)

API = "https://api.the-odds-api.com/v4"

SCHEMA = """
CREATE TABLE IF NOT EXISTS odds_cache (
    sport TEXT NOT NULL, fetched_at REAL NOT NULL, cost INTEGER NOT NULL,
    remaining INTEGER, used INTEGER, body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_odds_cache ON odds_cache(sport, fetched_at);
CREATE TABLE IF NOT EXISTS events_cache (sport TEXT PRIMARY KEY, fetched_at REAL NOT NULL, body TEXT NOT NULL);
"""


def month_start(now: datetime) -> datetime:
    """First instant of the UTC month (quota resets monthly)."""
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def days_left_in_month(now: datetime) -> float:
    """Days (fractional) until the next month starts."""
    nxt = (month_start(now) + timedelta(days=32)).replace(day=1)
    return max((nxt - now).total_seconds() / 86400.0, 1e-6)


@dataclass(slots=True)
class QuotaConfig:
    """Budget and priority rules."""

    monthly_budget: int = 500
    reserve: int = 20
    markets: tuple[str, ...] = ("h2h", "totals")
    bookmakers: tuple[str, ...] = ("pinnacle", "betfair_ex_eu", "williamhill", "unibet_eu", "marathonbet", "onexbet")
    sports: tuple[str, ...] = ("soccer_epl", "soccer_germany_bundesliga", "soccer_italy_serie_a", "soccer_spain_la_liga")
    priority_window_h: float = 3.0
    min_refresh_s: float = 3 * 3600.0
    events_refresh_s: float = 6 * 3600.0

    @property
    def cost_per_call(self) -> int:
        """markets x regions (10 bookmakers = 1 region)."""
        regions = max(1, -(-len(self.bookmakers) // 10))
        return len(self.markets) * regions


class OddsApiQuotaManager:
    """Budgeted, cached access to The-Odds-API free tier.

    ``http_get`` (async ``(url) -> (status, headers, json)``) is injectable for tests.
    """

    def __init__(self, api_key: str | None, db_path: Path | str = ":memory:", cfg: QuotaConfig | None = None,
                 http_get: Callable[[str], Any] | None = None, matcher: EventMatcher | None = None,
                 user_agent: str = "quantbet") -> None:
        self.key = api_key
        self.cfg = cfg or QuotaConfig()
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.http_get = http_get
        self.matcher = matcher or EventMatcher()
        self.user_agent = user_agent
        self._session: Any = None

    # -- accounting ---------------------------------------------------------
    def used_this_month(self, now: datetime | None = None) -> int:
        """Credits spent this month (from the API header when known, else summed costs)."""
        now = now or datetime.now(timezone.utc)
        ts = month_start(now).timestamp()
        row = self.db.execute(
            "SELECT used FROM odds_cache WHERE fetched_at >= ? AND used IS NOT NULL ORDER BY fetched_at DESC LIMIT 1", (ts,)
        ).fetchone()
        if row and row[0] is not None:
            return int(row[0])
        s = self.db.execute("SELECT COALESCE(SUM(cost),0) FROM odds_cache WHERE fetched_at >= ?", (ts,)).fetchone()[0]
        return int(s)

    def remaining(self, now: datetime | None = None) -> int:
        """Credits left this month (budget - used)."""
        return max(0, self.cfg.monthly_budget - self.used_this_month(now))

    def daily_allowance(self, now: datetime | None = None) -> float:
        """Credits that may be spent per day to last until month end (after reserve)."""
        now = now or datetime.now(timezone.utc)
        return max(0.0, self.remaining(now) - self.cfg.reserve) / days_left_in_month(now)

    def spent_today(self, now: datetime | None = None) -> int:
        """Credits spent since 00:00 UTC."""
        now = now or datetime.now(timezone.utc)
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return int(self.db.execute("SELECT COALESCE(SUM(cost),0) FROM odds_cache WHERE fetched_at >= ?", (day0,)).fetchone()[0])

    def last_fetch(self, sport: str) -> tuple[float, list[dict[str, Any]]] | None:
        """(fetched_at, payload) of the newest cached odds response for a sport."""
        row = self.db.execute(
            "SELECT fetched_at, body FROM odds_cache WHERE sport = ? ORDER BY fetched_at DESC LIMIT 1", (sport,)
        ).fetchone()
        return (float(row[0]), json.loads(row[1])) if row else None

    def can_spend(self, now: datetime | None = None) -> bool:
        """True if one more call fits the monthly and today's paced budget."""
        now = now or datetime.now(timezone.utc)
        c = self.cfg.cost_per_call
        if self.remaining(now) - c < self.cfg.reserve:
            return False
        return self.spent_today(now) + c <= max(self.daily_allowance(now), float(c))

    def should_refresh(self, sport: str, kickoffs: Sequence[datetime], now: datetime | None = None) -> bool:
        """Priority rule: a fixture starts within the window and the cache is old enough."""
        now = now or datetime.now(timezone.utc)
        soon = [k for k in kickoffs if timedelta(0) <= k - now <= timedelta(hours=self.cfg.priority_window_h)]
        if not soon:
            return False
        last = self.last_fetch(sport)
        if last and now.timestamp() - last[0] < self.cfg.min_refresh_s:
            return False
        return self.can_spend(now)

    # -- HTTP -----------------------------------------------------------------
    async def _get(self, url: str) -> tuple[int, dict[str, str], Any]:
        assert_allowed_url(url)
        if self.http_get is not None:
            return await self.http_get(url)
        import aiohttp

        if self._session is None:
            self._session = aiohttp.ClientSession(headers={"User-Agent": self.user_agent})
        async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
            data = await r.json(content_type=None) if r.status == 200 else None
            return r.status, {k.lower(): v for k, v in r.headers.items()}, data

    async def close(self) -> None:
        """Close the HTTP session and DB."""
        if self._session is not None:
            await self._session.close()
            self._session = None
        self.db.close()

    async def events(self, sport: str, now: float | None = None) -> list[dict[str, Any]]:
        """Fixtures of a sport (``/events`` costs 0 credits); cached ``events_refresh_s``."""
        now = time.time() if now is None else now
        row = self.db.execute("SELECT fetched_at, body FROM events_cache WHERE sport = ?", (sport,)).fetchone()
        if row and now - row[0] < self.cfg.events_refresh_s:
            return json.loads(row[1])
        if not self.key:
            return json.loads(row[1]) if row else []
        status, _, data = await self._get(f"{API}/sports/{sport}/events?apiKey={self.key}")
        if status != 200 or not isinstance(data, list):
            log.warning("the-odds-api events failed", extra={"sport": sport, "status": status})
            return json.loads(row[1]) if row else []
        self.db.execute("INSERT OR REPLACE INTO events_cache VALUES (?, ?, ?)", (sport, now, json.dumps(data)))
        self.db.commit()
        return data

    async def fetch_odds(self, sport: str, force: bool = False, now: datetime | None = None) -> list[dict[str, Any]] | None:
        """Spend credits on ``/odds`` if the priority/budget rules allow; returns payload or None."""
        now = now or datetime.now(timezone.utc)
        if not self.key:
            return None
        if not force:
            evs = await self.events(sport, now.timestamp())
            kos = []
            for e in evs:
                with_ko = e.get("commence_time")
                if with_ko:
                    kos.append(datetime.fromisoformat(with_ko.replace("Z", "+00:00")))
            if not self.should_refresh(sport, kos, now):
                return None
        elif not self.can_spend(now):
            log.warning("the-odds-api budget exhausted", extra={"remaining": self.remaining(now)})
            return None
        c = self.cfg
        url = (f"{API}/sports/{sport}/odds?apiKey={self.key}&markets={','.join(c.markets)}"
               f"&bookmakers={','.join(c.bookmakers)}&oddsFormat=decimal&dateFormat=iso")
        status, hdrs, data = await self._get(url)
        if status != 200 or not isinstance(data, list):
            log.warning("the-odds-api odds failed", extra={"sport": sport, "status": status})
            return None

        def _hi(k: str) -> int | None:
            try:
                return int(float(hdrs.get(k, "")))
            except ValueError:
                return None

        cost = _hi("x-requests-last")
        cost = cost if cost is not None else (c.cost_per_call if data else 0)
        self.db.execute("INSERT INTO odds_cache VALUES (?, ?, ?, ?, ?, ?)",
                        (sport, now.timestamp(), cost, _hi("x-requests-remaining"), _hi("x-requests-used"), json.dumps(data)))
        self.db.commit()
        log.info("the-odds-api credits", extra={"sport": sport, "cost": cost, "remaining_hdr": _hi("x-requests-remaining"),
                                                "budget_remaining": self.remaining(now)})
        return data

    # -- normalisation ------------------------------------------------------
    def snapshots(self, sport: str, payload: list[dict[str, Any]] | None = None, fetched_at: float | None = None) -> list[OddsSnapshot]:
        """Convert a cached payload to OddsSnapshots (timestamp = provider last_update)."""
        if payload is None:
            last = self.last_fetch(sport)
            if not last:
                return []
            fetched_at, payload = last
        out: list[OddsSnapshot] = []
        for ev in payload:
            ko = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
            home, away = ev["home_team"], ev["away_team"]
            key = self.matcher.match("the_odds_api", ev["id"], home, away, sport, ko)
            for bm in ev.get("bookmakers") or []:
                for mk in bm.get("markets") or []:
                    lu = mk.get("last_update") or bm.get("last_update")
                    ts = datetime.fromisoformat(lu.replace("Z", "+00:00")).timestamp() if lu else (fetched_at or time.time())
                    mt = {"h2h": MarketType.H2H, "totals": MarketType.TOTAL_GOALS}.get(mk.get("key"))
                    if mt is None:
                        continue
                    for o in mk.get("outcomes") or []:
                        name = o.get("name", "")
                        if mt is MarketType.H2H:
                            sel = Selection.HOME if name == home else Selection.AWAY if name == away else Selection.DRAW if name.lower() == "draw" else None
                            line = None
                        else:
                            sel = Selection.OVER if name.lower() == "over" else Selection.UNDER if name.lower() == "under" else None
                            line = o.get("point")
                        if sel is None or not o.get("price"):
                            continue
                        out.append(OddsSnapshot("the_odds_api", bm["key"], ev["id"], home, away, sport, ko, mt,
                                                line, sel, float(o["price"]), False, ts, event_key=key))
        return out

    def sharp_prices(self, home: str, away: str, kickoff: datetime | None = None, bookmaker: str = "pinnacle",
                     sports: Sequence[str] | None = None, threshold: float = 80.0) -> SharpPrices | None:
        """Cached Pinnacle 1X2 (+ main total) for a fixture, for :class:`~quantbet.engine.RatingsResolver`."""
        for sport in sports or self.cfg.sports:
            last = self.last_fetch(sport)
            if not last:
                continue
            fetched_at, payload = last
            for ev in payload:
                if min(team_similarity(home, ev["home_team"]), team_similarity(away, ev["away_team"])) < threshold:
                    continue
                bm = next((b for b in ev.get("bookmakers") or [] if b.get("key") == bookmaker), None)
                if not bm:
                    continue
                h = d = a = None
                tl = ov = un = None
                for mk in bm.get("markets") or []:
                    if mk.get("key") == "h2h":
                        for o in mk["outcomes"]:
                            if o["name"] == ev["home_team"]:
                                h = o["price"]
                            elif o["name"] == ev["away_team"]:
                                a = o["price"]
                            else:
                                d = o["price"]
                    elif mk.get("key") == "totals":
                        for o in mk["outcomes"]:
                            tl = o.get("point")
                            if o["name"].lower() == "over":
                                ov = o["price"]
                            else:
                                un = o["price"]
                if h and d and a:
                    return SharpPrices(float(h), float(d), float(a), tl, ov, un, fetched_at)
        return None
