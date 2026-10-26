"""Free live football statistics: ESPN (Tier 1, primary), SofaScore / FotMob (optional fallbacks).

Tier 1 ESPN lives in :mod:`quantbet.espn`; this module orchestrates the tiers:
ESPN scoreboard (+ summary for in-play matches) -> LiveMatchState ->
DeltaTracker synthetic momentum (when no graph momentum) -> optional
enrichers (Tier 2 API-Football, Tier 3 FotMob page) -> ``on_state``.
SofaScore and the FotMob JSON API are **disabled by default** (SofaScore
answers an honest client with HTTP 403; the old FotMob API path 404s).

**Important:** these are *undocumented* public JSON endpoints used by the
sites' own web apps. They can change or be withdrawn at any time and automated
access may be against the sites' terms of use – check the terms yourself before
running this. All URLs are configurable (:class:`FreeSourceEndpoints`). Access
goes through :class:`~quantbet.polite_http.PoliteFetcher` (honest User-Agent,
low rate, cache, backoff on 403/429/challenge, no evasion of any kind).

Parsed fields -> :class:`~quantbet.models.LiveMatchState` (Engine B compatible):
score, minute (incl. added time), period, corners, shots on target, shots
inside the box, red cards, xG, possession and the SofaScore *attack momentum*
(rolling 10-minute mean of ``/graph`` points, normalised to [-1, 1]).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import random
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .delta_tracker import DeltaTracker
from .espn import (DEFAULT_LEAGUES, LEAGUE_IDS, league_id_from_scoreboard, register_league_id, EspnEndpoints, EspnSummary, build_state_from_espn, parse_espn_scoreboard,
                   parse_espn_summary, split_all_scoreboard)
from .models import LiveMatchState
from .polite_http import FetchResult, PoliteFetcher
from .poller import EventMatcher, StatsTracker, team_similarity

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Endpoints (configurable; community-documented, unverified, may change)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FreeSourceEndpoints:
    """URL templates. Override via ``QB_SOFASCORE_BASE`` / ``QB_FOTMOB_*`` settings."""

    sofascore_base: str = "https://api.sofascore.com/api/v1"
    sofascore_live: str = "{base}/sport/football/events/live"
    sofascore_event: str = "{base}/event/{id}"
    sofascore_statistics: str = "{base}/event/{id}/statistics"
    sofascore_graph: str = "{base}/event/{id}/graph"
    sofascore_incidents: str = "{base}/event/{id}/incidents"
    fotmob_base: str = "https://www.fotmob.com/api/data"
    fotmob_matches: str = "{base}/matches?date={date}"
    fotmob_match_details: str = "{base}/matchDetails?matchId={id}"

    def sofa(self, name: str, event_id: str | int | None = None) -> str:
        """Build a SofaScore URL."""
        return getattr(self, f"sofascore_{name}").format(base=self.sofascore_base.rstrip("/"), id=event_id)

    def fotmob(self, name: str, **kw: Any) -> str:
        """Build a FotMob URL."""
        return getattr(self, f"fotmob_{name}").format(base=self.fotmob_base.rstrip("/"), **kw)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _num(v: Any) -> float | None:
    """Parse ``5``, ``"5"``, ``"55%"``, ``"1.23"``, ``"7/12 (58%)"`` -> float (first number)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", ".")
    num = ""
    for ch in s:
        if ch.isdigit() or (ch == "." and num and "." not in num) or (ch == "-" and not num):
            num += ch
        elif num:
            break
    try:
        return float(num) if num and num != "-" else None
    except ValueError:
        return None


def _int(v: Any) -> int | None:
    f = _num(v)
    return int(round(f)) if f is not None else None


def _norm_key(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


# ---------------------------------------------------------------------------
# SofaScore parsers
# ---------------------------------------------------------------------------
SOFA_STATUS = {
    0: "not started",
    6: "1st half",
    7: "2nd half",
    31: "halftime",
    32: "awaiting extra time",
    41: "1st extra",
    42: "2nd extra",
    50: "penalties",
    60: "postponed",
    70: "canceled",
    80: "interrupted",
    90: "abandoned",
    100: "ended",
    110: "ended aet",
    120: "ended pen",
}


@dataclass(slots=True)
class SofaEvent:
    """One SofaScore live event (subset of ``/sport/football/events/live``)."""

    id: int
    home: str
    away: str
    league: str
    start: datetime | None
    status_code: int
    status: str
    home_score: int
    away_score: int
    period_start_ts: float | None
    period_initial_s: float | None
    injury_time1: float | None
    injury_time2: float | None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


def parse_sofascore_live_events(payload: Mapping[str, Any]) -> list[SofaEvent]:
    """Parse ``/sport/football/events/live``; skips malformed entries."""
    out: list[SofaEvent] = []
    for e in payload.get("events") or []:
        try:
            tour = e.get("tournament") or {}
            cat = (tour.get("category") or {}).get("name") or ""
            name = (tour.get("uniqueTournament") or {}).get("name") or tour.get("name") or ""
            league = f"{cat} - {name}" if cat else name
            st = e.get("status") or {}
            t = e.get("time") or {}
            start = e.get("startTimestamp")
            code = int(st.get("code", -1))
            out.append(
                SofaEvent(
                    id=int(e["id"]),
                    home=(e.get("homeTeam") or {})["name"],
                    away=(e.get("awayTeam") or {})["name"],
                    league=league,
                    start=datetime.fromtimestamp(start, timezone.utc) if start else None,
                    status_code=code,
                    status=SOFA_STATUS.get(code, str(st.get("description") or "").lower()),
                    home_score=_int((e.get("homeScore") or {}).get("current")) or 0,
                    away_score=_int((e.get("awayScore") or {}).get("current")) or 0,
                    period_start_ts=_num(t.get("currentPeriodStartTimestamp")),
                    period_initial_s=_num(t.get("initial")),
                    injury_time1=_num(t.get("injuryTime1")),
                    injury_time2=_num(t.get("injuryTime2")),
                    raw=e,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.debug("skip malformed sofascore event", extra={"error": repr(exc)})
    return out


def sofascore_clock(ev: SofaEvent, now: float | None = None) -> tuple[float, int, float | None]:
    """(model minute, period, added_time) from period start timestamp.

    Minute is continuous elapsed match time (e.g. 47.5 in first-half stoppage);
    ``added_time`` is minutes beyond 45/90 (displayed as "45+N" / "90+N").
    Half-time maps to (45, 2) so the whole second half remains.
    """
    now = time.time() if now is None else now
    code = ev.status_code
    if code == 31:
        return 45.0, 2, None
    if code in (100, 110, 120):
        return 90.0 + (ev.injury_time2 or 0.0), 2, ev.injury_time2
    if code not in (6, 7) or ev.period_start_ts is None:
        return 0.0, 1, None
    offset = 0.0 if code == 6 else 45.0
    if ev.period_initial_s is not None:
        offset = ev.period_initial_s / 60.0
    minute = offset + max(0.0, (now - ev.period_start_ts) / 60.0)
    period = 1 if code == 6 else 2
    end = 45.0 if period == 1 else 90.0
    added = minute - end if minute > end else None
    return minute, period, added


SOFA_STAT_KEYS: dict[str, tuple[str, ...]] = {
    # canonical -> accepted SofaScore "key" or "name" variants (normalised)
    "corners": ("cornerkicks", "corners"),
    "shots_on_target": ("shotsongoal", "shotsontarget"),
    "shots_inside_box": ("totalshotsinsidebox", "shotsinsidebox"),
    "xg": ("expectedgoals", "xg"),
    "possession": ("ballpossession", "possession"),
    "red_cards": ("redcards",),
    "big_chances": ("bigchancecreated", "bigchances"),
}


def parse_sofascore_statistics(payload: Mapping[str, Any], period: str = "ALL") -> dict[str, tuple[float | None, float | None]]:
    """Parse ``/event/{id}/statistics`` -> {canonical: (home, away)} for ``period``."""
    lookup = {v: k for k, vs in SOFA_STAT_KEYS.items() for v in vs}
    out: dict[str, tuple[float | None, float | None]] = {}
    blocks = payload.get("statistics") or []
    block = next((b for b in blocks if str(b.get("period", "")).upper() == period), blocks[0] if blocks else None)
    if not block:
        return out
    for g in block.get("groups") or []:
        for it in g.get("statisticsItems") or []:
            canon = lookup.get(_norm_key(str(it.get("key") or ""))) or lookup.get(_norm_key(str(it.get("name") or "")))
            if not canon or canon in out:
                continue
            h = it.get("homeValue", it.get("home"))
            a = it.get("awayValue", it.get("away"))
            out[canon] = (_num(h), _num(a))
    return out


def parse_momentum_points(
    points: Iterable[Mapping[str, Any]], current_minute: float, window_min: float = 10.0, scale: float = 100.0
) -> tuple[float | None, int]:
    """Rolling mean of momentum points within ``window_min`` before ``current_minute``.

    Values are divided by ``scale`` and clipped to [-1, 1] (positive = home
    pressure). Returns ``(None, 0)`` if no point lies in the window.
    """
    vals: list[float] = []
    for p in points:
        m, v = _num(p.get("minute")), _num(p.get("value"))
        if m is None or v is None:
            continue
        if current_minute - window_min < m <= current_minute + 1e-9:
            vals.append(v)
    if not vals:
        return None, 0
    mean = sum(vals) / len(vals) / scale
    return max(-1.0, min(1.0, mean)), len(vals)


def parse_sofascore_graph(payload: Mapping[str, Any], current_minute: float, window_min: float = 10.0, scale: float = 100.0) -> tuple[float | None, int]:
    """Attack momentum from ``/event/{id}/graph`` (``graphPoints``: minute/value)."""
    return parse_momentum_points(payload.get("graphPoints") or [], current_minute, window_min, scale)


@dataclass(frozen=True, slots=True)
class IncidentSummary:
    """Red cards and announced stoppage from ``/event/{id}/incidents``."""

    home_reds: int = 0
    away_reds: int = 0
    injury_time_1: float | None = None
    injury_time_2: float | None = None
    home_goals: int | None = None
    away_goals: int | None = None


def parse_sofascore_incidents(payload: Mapping[str, Any]) -> IncidentSummary:
    """Count red / second-yellow cards and read ``injuryTime`` announcements."""
    hr = ar = 0
    it1 = it2 = None
    hg = ag = None
    for inc in payload.get("incidents") or []:
        typ = str(inc.get("incidentType", "")).lower()
        if typ == "card":
            cls = str(inc.get("incidentClass", "")).lower()
            if cls in ("red", "yellowred"):
                if inc.get("isHome"):
                    hr += 1
                else:
                    ar += 1
        elif typ == "injurytime":
            t, length = _num(inc.get("time")), _num(inc.get("length"))
            if t is not None and length is not None:
                if t <= 45:
                    it1 = length
                else:
                    it2 = length
        elif typ == "goal" and hg is None:
            # incidents are newest-first; the first goal carries the current score
            hg, ag = _int(inc.get("homeScore")), _int(inc.get("awayScore"))
    return IncidentSummary(hr, ar, it1, it2, hg, ag)


def build_state_from_sofascore(
    ev: SofaEvent,
    stats: Mapping[str, tuple[float | None, float | None]] | None,
    incidents: IncidentSummary | None,
    momentum: tuple[float | None, int] | None,
    event_key: str,
    now: float | None = None,
    tracker: StatsTracker | None = None,
) -> LiveMatchState:
    """Assemble an Engine-B-compatible :class:`LiveMatchState` from parsed SofaScore parts."""
    now = time.time() if now is None else now
    minute, period, added = sofascore_clock(ev, now)
    stats = stats or {}
    inc = incidents or IncidentSummary()

    def pair(k: str) -> tuple[float | None, float | None]:
        return stats.get(k, (None, None))

    ch, ca = pair("corners")
    sh, sa = pair("shots_on_target")
    bh, ba = pair("shots_inside_box")
    xh, xa = pair("xg")
    ph, _ = pair("possession")
    rh, ra = pair("red_cards")
    announced = inc.injury_time_1 if period == 1 else inc.injury_time_2
    if announced is None:
        announced = ev.injury_time1 if period == 1 else ev.injury_time2
    st = LiveMatchState(
        event_key=event_key,
        minute=minute,
        home_goals=ev.home_score,
        away_goals=ev.away_score,
        home_corners=int(ch or 0),
        away_corners=int(ca or 0),
        home_red_cards=max(inc.home_reds, int(rh or 0)),
        away_red_cards=max(inc.away_reds, int(ra or 0)),
        period=period,
        announced_stoppage=announced,
        home_xg=xh,
        away_xg=xa,
        possession_home=ph,
        updated_at=now,
        source="sofascore",
        source_event_id=str(ev.id),
        home=ev.home,
        away=ev.away,
        league=ev.league,
        commence_time=ev.start,
        added_time=added,
        status=ev.status,
        home_shots_on_target=_int(sh),
        away_shots_on_target=_int(sa),
        home_shots_inside_box=_int(bh),
        away_shots_inside_box=_int(ba),
        attack_momentum=momentum[0] if momentum else None,
        momentum_points=momentum[1] if momentum else 0,
        stats_available=ch is not None and ca is not None,
        fetched_at=now,
    )
    if tracker is not None:
        st.home_shots_on_target_10 = tracker.rolling(event_key, "h_sot", minute, sh)
        st.away_shots_on_target_10 = tracker.rolling(event_key, "a_sot", minute, sa)
    return st


# ---------------------------------------------------------------------------
# FotMob parsers (fallback)
# ---------------------------------------------------------------------------
FOTMOB_STAT_KEYS: dict[str, tuple[str, ...]] = {
    "corners": ("corners",),
    "shots_on_target": ("shotsontarget", "ontarget"),
    "shots_inside_box": ("shotsinsidebox", "insidebox"),
    "shots": ("totalshots",),
    "xg": ("expectedgoals", "expectedgoalsxg", "xg"),
    "possession": ("ballpossesion", "ballpossession", "possession"),
    "red_cards": ("redcards",),
}


@dataclass(slots=True)
class FotmobMatch:
    """One FotMob fixture from the ``matches?date=`` listing."""

    id: int
    home: str
    away: str
    league: str
    started: bool
    finished: bool
    home_score: int
    away_score: int


def parse_fotmob_matches(payload: Mapping[str, Any]) -> list[FotmobMatch]:
    """Parse FotMob ``matches?date=YYYYMMDD`` (``leagues[].matches[]``)."""
    out: list[FotmobMatch] = []
    for lg in payload.get("leagues") or []:
        lname = lg.get("name") or ""
        for m in lg.get("matches") or []:
            try:
                st = m.get("status") or {}
                out.append(
                    FotmobMatch(
                        int(m["id"]),
                        m["home"]["name"],
                        m["away"]["name"],
                        lname,
                        bool(st.get("started")),
                        bool(st.get("finished")),
                        _int(m["home"].get("score")) or 0,
                        _int(m["away"].get("score")) or 0,
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
    return out


def parse_fotmob_minute(status: Mapping[str, Any]) -> tuple[float, int, float | None]:
    """(minute, period, added) from FotMob ``header.status.liveTime``.

    ``short`` looks like ``"67'"``, ``"45+2'"``, ``"90+3'"`` or ``"HT"``; ``long``
    like ``"66:12"`` (elapsed mm:ss) is used for sub-minute precision.
    """
    lt = status.get("liveTime") or {}
    short = str(lt.get("short") or "").replace("\u2019", "'").replace("\u200e", "").replace("\u200f", "").strip()
    if short.upper() in ("HT", "PAUSE"):
        return 45.0, 2, None
    if status.get("finished"):
        return 90.0, 2, None
    base_s, _, plus_s = short.rstrip("'").partition("+")
    base = _num(base_s) or 0.0
    plus = _num(plus_s)
    period = 1 if base <= 45 else 2
    if plus is not None:
        return base + plus, period, plus
    long = str(lt.get("long") or "")
    if ":" in long:
        mm, _, ss = long.partition(":")
        if _num(mm) is not None:
            base = (_num(mm) or 0.0) + (_num(ss) or 0.0) / 60.0
    return base, period, None


def parse_fotmob_match_details(
    payload: Mapping[str, Any], event_key: str, now: float | None = None, momentum_scale: float = 100.0, tracker: StatsTracker | None = None
) -> LiveMatchState:
    """Parse FotMob ``matchDetails`` into :class:`LiveMatchState`."""
    now = time.time() if now is None else now
    general = payload.get("general") or {}
    header = payload.get("header") or {}
    teams = header.get("teams") or []
    status = header.get("status") or {}
    home = (general.get("homeTeam") or {}).get("name") or (teams[0].get("name") if teams else "")
    away = (general.get("awayTeam") or {}).get("name") or (teams[1].get("name") if len(teams) > 1 else "")
    hs = _int(teams[0].get("score")) if teams else None
    as_ = _int(teams[1].get("score")) if len(teams) > 1 else None
    minute, period, added = parse_fotmob_minute(status)
    content = payload.get("content") or {}
    lookup = {v: k for k, vs in FOTMOB_STAT_KEYS.items() for v in vs}
    stats: dict[str, tuple[float | None, float | None]] = {}
    periods = ((content.get("stats") or {}).get("Periods") or {})
    for grp in ((periods.get("All") or {}).get("stats") or []):
        for it in grp.get("stats") or []:
            canon = lookup.get(_norm_key(str(it.get("key") or ""))) or lookup.get(_norm_key(str(it.get("title") or "")))
            vals = it.get("stats")
            if canon and canon not in stats and isinstance(vals, list) and len(vals) == 2:
                if vals[0] is None and vals[1] is None:
                    continue  # group header rows carry [null, null]
                stats[canon] = (_num(vals[0]), _num(vals[1]))
    reds_h = reds_a = 0
    for ev in ((content.get("matchFacts") or {}).get("events") or {}).get("events") or []:
        if str(ev.get("type", "")).lower() == "card" and str(ev.get("card", "")).lower() in ("red", "yellowred"):
            if ev.get("isHome"):
                reds_h += 1
            else:
                reds_a += 1
    reds_h = max(reds_h, _int(status.get("numberOfHomeRedCards")) or 0, _int(stats.get("red_cards", (None, None))[0]) or 0)
    reds_a = max(reds_a, _int(status.get("numberOfAwayRedCards")) or 0, _int(stats.get("red_cards", (None, None))[1]) or 0)
    mom_pts = (((content.get("momentum") or {}).get("main") or {}).get("data")) or []
    mom = parse_momentum_points(mom_pts, minute, 10.0, momentum_scale)
    ch, ca = stats.get("corners", (None, None))
    sh, sa = stats.get("shots_on_target", (None, None))
    bh, ba = stats.get("shots_inside_box", (None, None))
    xh, xa = stats.get("xg", (None, None))
    ph, _ = stats.get("possession", (None, None))
    st = LiveMatchState(
        event_key=event_key,
        minute=minute,
        home_goals=hs or 0,
        away_goals=as_ or 0,
        home_corners=int(ch or 0),
        away_corners=int(ca or 0),
        home_red_cards=reds_h,
        away_red_cards=reds_a,
        period=period,
        announced_stoppage=added if added is not None else _num((status.get("liveTime") or {}).get("addedTime")),
        home_xg=xh,
        away_xg=xa,
        possession_home=ph,
        updated_at=now,
        source="fotmob",
        source_event_id=str(general.get("matchId") or ""),
        home=home,
        away=away,
        league=general.get("leagueName") or "",
        added_time=added,
        status="halftime" if str((status.get("liveTime") or {}).get("short", "")).upper() == "HT"
        else ("ended" if status.get("finished") else ("1st half" if period == 1 else "2nd half")),
        home_shots_on_target=_int(sh),
        away_shots_on_target=_int(sa),
        home_shots_inside_box=_int(bh),
        away_shots_inside_box=_int(ba),
        attack_momentum=mom[0],
        momentum_points=mom[1],
        stats_available=ch is not None and ca is not None,
        fetched_at=now,
    )
    if tracker is not None:
        st.home_shots_on_target_10 = tracker.rolling(event_key, "h_sot", minute, sh)
        st.away_shots_on_target_10 = tracker.rolling(event_key, "a_sot", minute, sa)
    return st


# ---------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FreePollerConfig:
    """Scheduling of the free poller (all intervals in seconds)."""

    live_list_interval_s: float = 60.0
    stats_interval_s: float = 30.0  # >= 15 s per match per source
    graph_interval_s: float = 60.0
    incidents_interval_s: float = 60.0
    max_events: int = 6
    league_filter: tuple[str, ...] = ()  # substrings; empty = all leagues
    momentum_window_min: float = 10.0
    momentum_scale: float = 100.0
    use_espn: bool = True
    use_sofascore: bool = False  # 403s an honest client; opt-in only, never impersonated
    use_fotmob: bool = False  # legacy FotMob JSON API (unverified path)
    espn_leagues: tuple[str, ...] = DEFAULT_LEAGUES
    learn_league_ids: bool = True  # one cached request per slug missing from espn.LEAGUE_IDS
    espn_scoreboard_interval_s: float = 60.0  # per league while something is live / about to start
    espn_idle_interval_s: float = 600.0  # per league when nothing is live within the next 15 min
    espn_summary_interval_s: float = 60.0  # per in-play match
    use_espn_summary: bool = True
    synthetic_momentum: bool = True
    # Single-endpoint mode: one soccer/all/scoreboard every espn_all_interval_s + U(jitter) s,
    # filtered in memory by league (event uid "l:<id>"). Falls back to per-league scoreboards
    # staggered >= espn_stagger_s apart if the "all" payload lacks a league or live stats.
    espn_use_all: bool = True
    espn_all_interval_s: float = 60.0
    espn_jitter_s: tuple[float, float] = (2.0, 5.0)
    espn_stagger_s: float = 15.0
    # /summary only ONCE per match (first sighting / pre-kick-off) for the DraftKings prior;
    # never re-polled in play. Cached in SQLite when summary_db_path is set.
    espn_summary_once: bool = True
    espn_summary_lookahead_h: float = 4.0
    espn_summaries_per_cycle: int = 1
    summary_db_path: str | None = None
    # --- v4.3-global (all off by default = v4.2 behaviour) ---
    espn_all_limit: int | None = None  # ?limit=N on soccer/all/scoreboard (ESPN default is 100 events!)
    auto_discover: bool = False  # also ingest live events of unlisted leagues (pseudo slug "espn.<id>")
    telemetry_gate_minute: float | None = None  # Live Stats Integrity Gate (30.0 in v4.3-global)
    fallback_listed_only: bool = False  # per-league fallback only for listed leagues, keep the other all-feed events


class FreeStatsPoller:
    """Polls SofaScore (and FotMob when SofaScore is blocked/unavailable).

    ``on_state`` is awaited (or called) with each new :class:`LiveMatchState`.
    """

    def __init__(
        self,
        fetcher: PoliteFetcher,
        on_state: Callable[[LiveMatchState], Any],
        matcher: EventMatcher | None = None,
        endpoints: FreeSourceEndpoints | None = None,
        cfg: FreePollerConfig | None = None,
        espn_endpoints: EspnEndpoints | None = None,
        delta: DeltaTracker | None = None,
        ratings_hint: Callable[[LiveMatchState], Any] | None = None,
        enrichers: Iterable[Callable[[LiveMatchState], Any]] | None = None,
    ) -> None:
        self.f = fetcher
        self.on_state = on_state
        self.matcher = matcher or EventMatcher()
        self.ep = endpoints or FreeSourceEndpoints()
        self.cfg = cfg or FreePollerConfig()
        self.tracker = StatsTracker(self.cfg.momentum_window_min)
        self.espn = espn_endpoints or EspnEndpoints()
        self.delta = delta or DeltaTracker()
        self.ratings_hint = ratings_hint  # LiveMatchState -> PreMatchRatings | None (corner expectation)
        self.enrichers: list[Callable[[LiveMatchState], Any]] = list(enrichers or [])
        self.priors: dict[str, tuple[Any, str]] = {}  # event_key -> (SharpPrices, provider) from ESPN pickcenter
        self.espn_summaries: dict[str, EspnSummary] = {}
        self._espn_next: dict[str, float] = {}
        self.finals: dict[str, dict[str, Any]] = {}  # event_key -> FT result (for paper grading)
        self.scheduled: dict[str, dict[str, Any]] = {}  # event_key -> latest status of every target event
        self._summary_done: set[str] = set()
        self._sdb = None
        if self.cfg.summary_db_path:
            import sqlite3

            self._sdb = sqlite3.connect(self.cfg.summary_db_path)
            self._sdb.execute("CREATE TABLE IF NOT EXISTS espn_summary_once (event_id TEXT PRIMARY KEY, league TEXT,"
                              " fetched_at REAL, body TEXT)")
            self._sdb.commit()
        self.last_source = ""
        self._fotmob_ids: dict[str, int] = {}
        self.stats_cache: dict[str, dict[str, tuple[float | None, float | None]]] = {}
        self.incident_cache: dict[str, IncidentSummary] = {}
        self.momentum_cache: dict[str, tuple[float | None, int]] = {}
        self.last_seen: dict[str, dict[str, Any]] = {}  # event_key -> {"match", "minute", "ts"} (gap reports)
        self.wake = asyncio.Event()  # set by request_refresh() to skip the remaining timer wait
        self.n_polls = 0
        # v4.3-global hooks (optional): slug -> sort rank (lower first when max_events binds) and
        # (pseudo slug, name, espn id, header slug) callback when a summary names an auto-discovered league.
        self.league_priority: Callable[[str], int] | None = None
        self.on_league_meta: Callable[[str, str, str | None, str | None], Any] | None = None
        self.telemetry_skips: dict[str, dict[str, Any]] = {}  # event_key -> {"reason", "minute", "n", "match", "league"}
        self.telemetry_ok_keys: set[str] = set()
        self._auto_seen: set[str] = set()

    def note_gap(self, gap: Any) -> list[str]:
        """Heartbeat detected a polling gap (> 180 s): every tracked match restarts from a fresh baseline
        on its next snapshot (``delta_reset: post_gap``); see :mod:`quantbet.delta_tracker`."""
        dur = float(getattr(gap, "duration_s", gap) or 0.0)
        keys = self.delta.note_global_gap(dur)
        self.tracker.reset()
        log.info("post-gap delta reset armed", extra={"gap_s": round(dur, 1), "matches": len(keys)})
        return keys

    def request_refresh(self) -> None:
        """Poll now instead of waiting for the timer (requests still go through PoliteFetcher pacing/backoff)."""
        self.wake.set()

    def live_snapshot(self) -> dict[str, dict[str, Any]]:
        """Matches currently in play with their last known minute."""
        out = {}
        for k, v in self.scheduled.items():
            if v.get("state") == "in":
                ls = self.last_seen.get(k, {})
                out[k] = {"match": f"{v.get('home')} v {v.get('away')}", "minute": ls.get("minute")}
        return out

    def _want(self, league: str) -> bool:
        lf = self.cfg.league_filter
        return not lf or any(x.lower() in league.lower() for x in lf)

    def _key(self, source: str, sid: str | int, home: str, away: str, league: str, ko: datetime | None) -> str:
        return self.matcher.match(source, str(sid), home, away, league, ko)

    async def _emit(self, st: LiveMatchState) -> None:
        self.last_seen[st.event_key] = {"match": f"{st.home} v {st.away}", "minute": st.minute, "ts": time.time()}
        res = self.on_state(st)
        if asyncio.iscoroutine(res):
            await res

    def prior_for(self, event_key: str) -> tuple[Any, str] | None:
        """ESPN pickcenter pre-match lines for :class:`~quantbet.engine.RatingsResolver` ``prior_lookup``."""
        return self.priors.get(event_key)

    def apply_synthetic_momentum(self, st: LiveMatchState, now: float | None = None) -> LiveMatchState:
        """Feed the DeltaTracker; set velocity / 10-min SOT and - if no graph momentum - ``attack_momentum``."""
        now = time.time() if now is None else now
        corners_ok = st.stats_available
        home = {"sot": st.home_shots_on_target, "shots": st.home_shots, "corners": st.home_corners if corners_ok else None}
        away = {"sot": st.away_shots_on_target, "shots": st.away_shots, "corners": st.away_corners if corners_ok else None}
        corner_total = None
        if self.ratings_hint is not None:
            with contextlib.suppress(Exception):
                r = self.ratings_hint(st)
                corner_total = getattr(r, "corner_total", None)
        rd = self.delta.update(st.event_key, st.minute, home, away, status=st.status, source=st.source,
                               wall=now, corner_total=corner_total)
        if rd.reset_reason == "post_gap":
            # post-gap delta reset: the rolling 10-min counters must not span the gap either
            self.tracker.reset(st.event_key)
        st.velocity = dict(rd.velocity)
        if rd.mult:
            # single live-pressure path into Engine B (no SOT ratio on top -> no double count)
            st.momentum_mult_home, st.momentum_mult_away = rd.mult["home"], rd.mult["away"]
            st.momentum_mult_total = rd.mult["total"]
            st.activity_10m = dict(rd.activity_10m)
        if st.attack_momentum is None and rd.momentum is not None and self.cfg.synthetic_momentum:
            st.attack_momentum = rd.momentum
            st.momentum_points = rd.n_snapshots
            st.momentum_source = "synthetic"
        elif st.attack_momentum is not None and not st.momentum_source:
            st.momentum_source = "graph"
        return st

    async def _enrich(self, st: LiveMatchState) -> LiveMatchState:
        for fn in self.enrichers:
            try:
                res = fn(st)
                if asyncio.iscoroutine(res):
                    res = await res
                st = res or st
            except Exception:  # noqa: BLE001 - enrichment must never break Tier 1
                log.exception("enricher failed", extra={"event": st.event_key})
        return st

    # -- ESPN summary once per match ------------------------------------------
    def _load_summary(self, ev_id: str) -> Any | None:
        if self._sdb is None:
            return None
        row = self._sdb.execute("SELECT body FROM espn_summary_once WHERE event_id=?", (ev_id,)).fetchone()
        if not row:
            return None
        import json as _json

        with contextlib.suppress(Exception):
            return _json.loads(row[0])
        return None

    def _store_summary(self, ev_id: str, league: str, data: Any) -> None:
        if self._sdb is None:
            return
        import json as _json

        self._sdb.execute("INSERT OR REPLACE INTO espn_summary_once VALUES (?,?,?,?)",
                          (ev_id, league, time.time(), _json.dumps(data)))
        self._sdb.commit()

    def _take_prior(self, key: str, data: Any, league: str | None = None) -> None:
        if league and league.startswith("espn.") and isinstance(data, Mapping):
            with contextlib.suppress(Exception):
                hl = (data.get("header") or {}).get("league") or {}
                name = str(hl.get("name") or "")
                if name:
                    from .espn import LEAGUE_NAMES

                    LEAGUE_NAMES[league] = name
                    if self.on_league_meta is not None:
                        self.on_league_meta(league, name, str(hl.get("id") or "") or None, hl.get("slug"))
        with contextlib.suppress(Exception):
            summ = parse_espn_summary(data)
            self.espn_summaries[key] = summ
            if summ.prior is not None and key not in self.priors:
                self.priors[key] = (summ.prior, f"espn_{summ.prior_provider.lower() or 'pickcenter'}")

    async def _summaries_once(self, cands: list[tuple[str, Any]]) -> None:
        """Fetch /summary for at most ``espn_summaries_per_cycle`` not-yet-seen matches (live first)."""
        budget = self.cfg.espn_summaries_per_cycle
        for key, ev in cands:
            if key in self._summary_done:
                continue
            cached = self._load_summary(ev.id)
            if cached is not None:
                self._take_prior(key, cached, ev.league)
                self._summary_done.add(key)
                continue
            if budget <= 0:
                continue
            budget -= 1
            lg = "all" if ev.league.startswith("espn.") else ev.league  # soccer/all/summary works for any league
            sr = await self.f.get_json(self.espn.summary(lg, ev.id), cache_ttl_s=24 * 3600)
            if sr.ok:
                self._store_summary(ev.id, ev.league, sr.data)
                self._take_prior(key, sr.data, ev.league)
                self._summary_done.add(key)
            elif sr.blocked or sr.status in (403, 429, 503):
                return
            elif sr.status in (400, 404):
                self._summary_done.add(key)  # no summary for this event - do not retry

    def _note_event(self, key: str, ev: Any) -> None:
        self.scheduled[key] = {"league": ev.league, "home": ev.home, "away": ev.away, "state": ev.state,
                               "start": ev.start.isoformat() if ev.start else None, "status": ev.status_name}
        if ev.state == "post" and key not in self.finals:
            hs, as_ = ev.home_stats, ev.away_stats
            self.finals[key] = {
                "home_goals": ev.home_score, "away_goals": ev.away_score,
                "home_corners": hs.get("wonCorners"), "away_corners": as_.get("wonCorners"),
                "status": ev.status_name, "home": ev.home, "away": ev.away, "league": ev.league,
                "recorded_at": time.time(),
            }

    def telemetry_gate(self, key: str, ev: Any) -> bool:
        """v4.3 Live Stats Integrity Gate for one live ESPN event (True = pass). Skips are logged once per
        match and reason as ``skipped: no_live_telemetry`` and counted in ``telemetry_skips``."""
        gm = self.cfg.telemetry_gate_minute
        if gm is None:
            return True
        from .global_layer import SKIP_NO_TELEMETRY, telemetry_check

        minute = ev.clock[0]
        ok, why = telemetry_check(ev.home_stats, ev.away_stats, minute, gm)
        if ok:
            if why == "ok":
                self.telemetry_ok_keys.add(key)
            return True
        rec = self.telemetry_skips.get(key)
        if rec is None or rec.get("reason") != why:
            log.info(SKIP_NO_TELEMETRY, extra={"event": key, "match": f"{ev.home} v {ev.away}", "league": ev.league,
                                               "league_name": ev.league_name, "minute": minute, "why": why})
            rec = {"reason": why, "n": 0, "match": f"{ev.home} v {ev.away}", "league": ev.league}
            self.telemetry_skips[key] = rec
        rec["n"] = int(rec.get("n", 0)) + 1
        rec["minute"] = minute
        return False

    async def _process_events(self, events: list[Any], now_: float, states: list[LiveMatchState]) -> None:
        c = self.cfg
        wall = time.time()
        cands: list[tuple[float, str, Any]] = []
        live_n = 0
        if self.league_priority is not None:  # listed tiers first, auto-discovered last (stable)
            pr = self.league_priority
            events = sorted(events, key=lambda e: (pr(e.league), 0 if e.live else 1))
        for ev in events:
            if not self._want(ev.league_name):
                continue
            auto = ev.league.startswith("espn.")
            if auto and ev.state == "post" and ev.id not in self._auto_seen:
                continue  # finished auto-discovered matches we never tracked live
            key = self._key("espn", ev.id, ev.home, ev.away, ev.league_name, ev.start)
            if auto:
                self._auto_seen.add(ev.id)
            self._note_event(key, ev)
            soon = ev.state == "pre" and ev.start is not None and (ev.start.timestamp() - wall) < c.espn_summary_lookahead_h * 3600
            if c.use_espn_summary and (ev.live or soon) and not (auto and key in self.telemetry_skips):
                cands.append((0.0 if ev.live else ev.start.timestamp(), key, ev))  # type: ignore[union-attr]
            if not ev.live or live_n >= c.max_events:
                continue
            if not self.telemetry_gate(key, ev):
                continue
            live_n += 1
            summ = None
            if c.use_espn_summary and not c.espn_summary_once:
                sr = await self.f.get_json(self.espn.summary(ev.league, ev.id), cache_ttl_s=c.espn_summary_interval_s)
                if sr.ok:
                    self._take_prior(key, sr.data)
                summ = self.espn_summaries.get(key)
            st = build_state_from_espn(ev, summ, key, now_)  # summary-once: live state from scoreboard only
            self.apply_synthetic_momentum(st, now_)
            st = await self._enrich(st)
            states.append(st)
        if c.use_espn_summary and c.espn_summary_once and cands:
            if self.league_priority is not None:
                pr = self.league_priority
                cands.sort(key=lambda x: (x[0], pr(x[2].league)))
            else:
                cands.sort(key=lambda x: x[0])
            await self._summaries_once([(k, e) for _, k, e in cands])

    async def learn_league_ids(self) -> list[str]:
        """Once per run: learn the ``soccer/all`` league id of configured slugs not in LEAGUE_IDS.

        One per-league scoreboard request per unknown slug (cached 12 h). Slugs ESPN does not
        know are dropped with a warning. Returns the dropped slugs.
        """
        c = self.cfg
        if getattr(self, "_ids_learnt", False):
            return []
        self._ids_learnt = True
        known = set(LEAGUE_IDS.values())
        dropped: list[str] = []
        for lg in [x for x in c.espn_leagues if x not in known]:
            r = await self.f.get_json(self.espn.scoreboard(lg), cache_ttl_s=12 * 3600)
            lid = league_id_from_scoreboard(r.data) if r.ok and isinstance(r.data, dict) else None
            if lid:
                register_league_id(lid, lg)
                log.info("espn league id learnt", extra={"league": lg, "id": lid})
            elif r.ok or r.status in (400, 404):
                dropped.append(lg)
                log.warning("espn has no such league slug - dropped", extra={"league": lg, "status": r.status})
            else:  # transient / refused: keep the slug, retry next run
                log.info("espn league id lookup failed", extra={"league": lg, "status": r.status})
                if r.blocked or r.status in (403, 429, 503):
                    break
        if dropped:
            self.cfg = dataclasses.replace(c, espn_leagues=tuple(x for x in c.espn_leagues if x not in dropped))
        return dropped

    async def poll_espn_once(self, now: float | None = None) -> list[LiveMatchState] | None:
        """One ESPN cycle; ``None`` if ESPN refused/failed (=> fail over)."""
        c = self.cfg
        if c.espn_use_all and c.learn_league_ids and not getattr(self, "_ids_learnt", False):
            await self.learn_league_ids()
            c = self.cfg
            c = self.cfg
        wall = time.time()
        now_ = wall if now is None else now
        states: list[LiveMatchState] = []
        if c.espn_use_all:
            url = self.espn.scoreboard("all")
            if c.espn_all_limit:
                url += ("&" if "?" in url else "?") + f"limit={int(c.espn_all_limit)}"
            r = await self.f.get_json(url, cache_ttl_s=c.espn_all_interval_s * 0.8)
            if not r.ok:
                log.info("espn all-scoreboard unavailable", extra={"status": r.status, "error": r.error})
                return None
            if c.espn_all_limit and len((r.data or {}).get("events") or []) >= int(c.espn_all_limit):
                log.warning("espn all-scoreboard hit its limit - events may be missing", extra={"limit": c.espn_all_limit})
            parts = split_all_scoreboard(r.data, c.espn_leagues, auto_discover=c.auto_discover)
            events: list[Any] = []
            for lg, payload in parts.items():
                events.extend(parse_espn_scoreboard(payload, lg))
            listed = set(c.espn_leagues)
            live_wo_stats = [e for e in events if e.live and not e.home_stats and not e.away_stats
                             and (not c.fallback_listed_only or e.league in listed)]
            if live_wo_stats and not getattr(self, "_warned_all", False):
                log.warning("espn 'all' payload lacks live stats - per-league fallback", extra={"n": len(live_wo_stats)})
                self._warned_all = True
            if not live_wo_stats:
                await self._process_events(events, now_, states)
                return states
            fallback = sorted({e.league for e in live_wo_stats})
            if c.fallback_listed_only:  # keep every other league's all-feed events; refetch only the stats-less ones
                await self._process_events([e for e in events if e.league not in fallback], now_, states)
        else:
            fallback = list(c.espn_leagues)
        any_ok = tried = 0
        for i, lg in enumerate(fallback):
            if self._espn_next.get(lg, 0.0) > wall:
                continue
            if tried:
                await asyncio.sleep(c.espn_stagger_s)  # stagger per-league calls
            tried += 1
            r = await self.f.get_json(self.espn.scoreboard(lg), cache_ttl_s=c.espn_scoreboard_interval_s * 0.9)
            if not r.ok:
                log.info("espn scoreboard unavailable", extra={"league": lg, "status": r.status, "error": r.error})
                if r.blocked or r.status in (403, 429, 503):
                    break
                continue
            any_ok += 1
            events = parse_espn_scoreboard(r.data, lg)
            soon = any(
                e.live or (e.state == "pre" and e.start is not None and (e.start.timestamp() - wall) < 15 * 60)
                for e in events
            )
            self._espn_next[lg] = wall + (c.espn_scoreboard_interval_s if soon else c.espn_idle_interval_s)
            await self._process_events(events, now_, states)
        if tried == 0:
            return states
        return states if any_ok else None

    async def poll_sofascore_once(self, now: float | None = None) -> list[LiveMatchState] | None:
        """One SofaScore cycle; ``None`` if SofaScore is blocked/unavailable (=> fail over)."""
        c = self.cfg
        lst = await self.f.get_json(self.ep.sofa("live"), cache_ttl_s=c.live_list_interval_s)
        if not lst.ok:
            return None
        events = [e for e in parse_sofascore_live_events(lst.data) if e.status_code in (6, 7, 31) and self._want(e.league)]
        states: list[LiveMatchState] = []
        for ev in events[: c.max_events]:
            key = self._key("sofascore", ev.id, ev.home, ev.away, ev.league, ev.start)
            r = await self.f.get_json(self.ep.sofa("statistics", ev.id), cache_ttl_s=c.stats_interval_s)
            if r.ok:
                self.stats_cache[key] = parse_sofascore_statistics(r.data)
            elif r.blocked:
                return states or None
            minute = sofascore_clock(ev, now)[0]
            g = await self.f.get_json(self.ep.sofa("graph", ev.id), cache_ttl_s=c.graph_interval_s)
            if g.ok:
                self.momentum_cache[key] = parse_sofascore_graph(g.data, minute, c.momentum_window_min, c.momentum_scale)
            i = await self.f.get_json(self.ep.sofa("incidents", ev.id), cache_ttl_s=c.incidents_interval_s)
            if i.ok:
                self.incident_cache[key] = parse_sofascore_incidents(i.data)
            st = build_state_from_sofascore(
                ev, self.stats_cache.get(key), self.incident_cache.get(key), self.momentum_cache.get(key), key, now, self.tracker
            )
            states.append(st)
        return states

    async def poll_fotmob_once(self, now: float | None = None, day: datetime | None = None) -> list[LiveMatchState] | None:
        """One FotMob cycle (fallback); ``None`` if FotMob is blocked/unavailable."""
        c = self.cfg
        d = (day or datetime.now(timezone.utc)).strftime("%Y%m%d")
        lst = await self.f.get_json(self.ep.fotmob("matches", date=d), cache_ttl_s=c.live_list_interval_s)
        if not lst.ok:
            return None
        live = [m for m in parse_fotmob_matches(lst.data) if m.started and not m.finished and self._want(m.league)]
        states: list[LiveMatchState] = []
        for m in live[: c.max_events]:
            key = self._key("fotmob", m.id, m.home, m.away, m.league, None)
            r = await self.f.get_json(self.ep.fotmob("match_details", id=m.id), cache_ttl_s=c.stats_interval_s)
            if r.blocked:
                return states or None
            if r.ok:
                st = parse_fotmob_match_details(r.data, key, now, c.momentum_scale, self.tracker)
                st.league = st.league or m.league
                states.append(st)
        return states

    async def poll_once(self, now: float | None = None) -> list[LiveMatchState]:
        """ESPN first; SofaScore then FotMob (if enabled) on failure; emits and returns the states."""
        states: list[LiveMatchState] | None = None
        self.last_source = ""
        if self.cfg.use_espn:
            states = await self.poll_espn_once(now)
            if states is None:
                log.warning("espn unavailable/blocked - failing over")
            else:
                self.last_source = "espn"
        if states is None and self.cfg.use_sofascore:
            states = await self.poll_sofascore_once(now)
            if states is None:
                log.warning("sofascore unavailable/blocked - failing over to fotmob")
            else:
                self.last_source = "sofascore"
                for st in states:
                    self.apply_synthetic_momentum(st, now)
        if states is None and self.cfg.use_fotmob:
            states = await self.poll_fotmob_once(now)
            if states is None:
                log.warning("fotmob unavailable/blocked - no live stats this cycle")
            else:
                self.last_source = "fotmob"
                for st in states:
                    self.apply_synthetic_momentum(st, now)
        for st in states or []:
            await self._emit(st)
        return states or []

    async def run(self, stop: asyncio.Event) -> None:
        """Loop until ``stop`` is set."""
        while not stop.is_set():
            t0 = time.time()
            self.wake.clear()
            try:
                await self.poll_once()
                self.n_polls += 1
            except Exception:  # noqa: BLE001
                log.exception("free stats poll failed")
            if self.cfg.use_espn and self.cfg.espn_use_all:
                lo, hi = self.cfg.espn_jitter_s
                period = self.cfg.espn_all_interval_s + random.uniform(lo, hi)
            else:
                period = self.cfg.espn_scoreboard_interval_s if self.cfg.use_espn else self.cfg.stats_interval_s
            wait = max(1.0, period - (time.time() - t0))
            ts, tw = asyncio.ensure_future(stop.wait()), asyncio.ensure_future(self.wake.wait())
            try:
                await asyncio.wait({ts, tw}, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in (ts, tw):
                    t.cancel()
            if self.wake.is_set() and not stop.is_set():
                log.info("forced scoreboard refresh (gap recovery)")


def match_fotmob_to_event(home: str, away: str, matches: Iterable[FotmobMatch], threshold: float = 80.0) -> FotmobMatch | None:
    """Find the FotMob fixture for a SofaScore/odds event by fuzzy team names."""
    best, score = None, 0.0
    for m in matches:
        s = min(team_similarity(home, m.home), team_similarity(away, m.away))
        if s > score:
            best, score = m, s
    return best if score >= threshold else None


__all__ = [
    "FreeSourceEndpoints",
    "SofaEvent",
    "parse_sofascore_live_events",
    "sofascore_clock",
    "parse_sofascore_statistics",
    "parse_sofascore_graph",
    "parse_momentum_points",
    "parse_sofascore_incidents",
    "IncidentSummary",
    "build_state_from_sofascore",
    "FotmobMatch",
    "parse_fotmob_matches",
    "parse_fotmob_minute",
    "parse_fotmob_match_details",
    "FreePollerConfig",
    "FreeStatsPoller",
    "match_fotmob_to_event",
    "FetchResult",
]
