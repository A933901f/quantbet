"""ESPN public site API - Tier 1 (primary) free live-stats source.

Endpoints (unofficial, undocumented, used by ESPN's own web/app front-ends;
they can change or be withdrawn at any time and automated use may be against
ESPN's terms - check them yourself):

* ``GET {base}/{league}/scoreboard`` - all fixtures of the day for one league,
  incl. status/clock **and** per-team ``statistics`` (wonCorners, totalShots,
  shotsOnTarget, possessionPct, foulsCommitted ...) plus ``details`` (goals,
  penalties, red cards with ``redCard: true`` and team id).
* ``GET {base}/{league}/summary?event={id}`` - ``boxscore.teams[].statistics``
  (full box score incl. redCards/yellowCards), ``header.competitions[0].status``,
  ``keyEvents`` (type.type: goal, yellow-card, red-card, halftime, ...) and
  ``pickcenter`` (DraftKings pre-match lines - informational prior only, never
  presented as Bet365).

Field names below were verified against real responses on 2026-10-25 (see
``tests/fixtures/espn_*.json``). ESPN has **no xG, no dangerous attacks and no
shots-inside-box**; momentum is therefore synthesised by
:class:`~quantbet.delta_tracker.DeltaTracker` from corner/shot deltas.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from .engine import SharpPrices
from .models import LiveMatchState

log = logging.getLogger(__name__)

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
DEFAULT_LEAGUES: tuple[str, ...] = (
    "eng.1", "esp.1", "ger.1", "ita.1", "fra.1", "uefa.champions", "uefa.europa",
)
LEAGUE_NAMES: dict[str, str] = {
    # slug -> name used for league defaults / ratings lookups
    "eng.1": "English Premier League",
    "esp.1": "Spanish La Liga",
    "ger.1": "German Bundesliga",
    "ita.1": "Italian Serie A",
    "fra.1": "French Ligue 1",
    "uefa.champions": "UEFA Champions League",
    "uefa.europa": "UEFA Europa League",
    "uefa.europa.conf": "UEFA Conference League",
    "eng.league_cup": "English Carabao Cup",
    "eng.fa": "English FA Cup",
    "esp.copa_del_rey": "Spanish Copa del Rey",
    "ger.dfb_pokal": "German DFB Pokal",
    "ita.coppa_italia": "Italian Coppa Italia",
    "fra.coupe_de_france": "French Coupe de France",
}

# Cup/European slugs used by the v4 live target list. Ids of the first two were verified
# 2026-10-25 against site.api.espn.com; the rest are learnt at startup (one cached request each).
CUP_LEAGUES: tuple[str, ...] = ("uefa.champions", "uefa.europa", "uefa.europa.conf", "eng.league_cup", "eng.fa",
                                "esp.copa_del_rey", "ger.dfb_pokal", "ita.coppa_italia", "fra.coupe_de_france")


@dataclass(frozen=True, slots=True)
class EspnEndpoints:
    """URL templates (override base via ``QB_ESPN_BASE``)."""

    base: str = ESPN_BASE
    scoreboard_tpl: str = "{base}/{league}/scoreboard"
    summary_tpl: str = "{base}/{league}/summary?event={id}"

    def scoreboard(self, league: str) -> str:
        return self.scoreboard_tpl.format(base=self.base.rstrip("/"), league=league)

    def summary(self, league: str, event_id: str | int) -> str:
        return self.summary_tpl.format(base=self.base.rstrip("/"), league=league, id=event_id)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _f(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    return float(m.group()) if m else None


def _i(v: Any) -> int | None:
    x = _f(v)
    return int(round(x)) if x is not None else None


def _dt(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def stats_list_to_dict(stats: Iterable[Mapping[str, Any]] | None) -> dict[str, float]:
    """``[{"name": "wonCorners", "displayValue": "3"}, ...]`` -> ``{"wonCorners": 3.0}``."""
    out: dict[str, float] = {}
    for s in stats or []:
        name = s.get("name")
        val = _f(s.get("displayValue", s.get("value")))
        if name and val is not None:
            out[str(name)] = val
    return out


# ---------------------------------------------------------------------------
# status / clock
# ---------------------------------------------------------------------------
HALFTIME_NAMES = {"STATUS_HALFTIME", "STATUS_HALF_TIME"}
_DISPLAY_RE = re.compile(r"^\s*(\d+)\s*'?\s*(?:\+\s*(\d+)\s*'?)?")


def espn_clock(status: Mapping[str, Any]) -> tuple[float, int, float | None, str]:
    """(model minute, period, added_time, status label) from an ESPN ``status`` object.

    * ``displayClock`` ``"45'+2'"`` / ``"90'+4'"`` -> minute 47 / 94, added 2 / 4.
    * otherwise ``clock`` (elapsed seconds, cumulative over halves; 2580 -> 43.0).
    * STATUS_HALFTIME -> (45, 2, None, "halftime") so the whole 2nd half remains.
    * state ``post`` -> (90 + added, 2, added, "ended"); ``pre`` -> (0, 1, None, "not started").
    """
    st = status.get("type") or {}
    name = str(st.get("name") or "").upper()
    state = str(st.get("state") or "").lower()
    period = _i(status.get("period")) or 1
    disp = str(status.get("displayClock") or "")
    m = _DISPLAY_RE.match(disp)
    base = float(m.group(1)) if m else None
    plus = float(m.group(2)) if m and m.group(2) else None
    if name in HALFTIME_NAMES or "halftime" in str(st.get("description") or "").lower().replace(" ", ""):
        return 45.0, 2, None, "halftime"
    if state == "post" or st.get("completed"):
        return 90.0 + (plus or 0.0), 2, plus, "ended"
    if state == "pre":
        return 0.0, 1, None, "not started"
    if base is not None and plus is not None:
        minute = base + plus
    else:
        clk = _f(status.get("clock"))
        minute = clk / 60.0 if clk is not None and clk > 0 else (base or 0.0)
    if period >= 3:
        label = "extra time"
    elif name in ("STATUS_FIRST_HALF",) or period == 1:
        label = "1st half"
    else:
        label = "2nd half"
    end = 45.0 if period == 1 else 90.0
    added = plus if plus is not None else (minute - end if minute > end else None)
    return minute, period, added, label


# ---------------------------------------------------------------------------
# scoreboard
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class EspnEvent:
    """One fixture from ``/{league}/scoreboard``."""

    id: str
    league: str  # slug, e.g. "eng.1"
    league_name: str
    home: str
    away: str
    home_id: str
    away_id: str
    home_score: int
    away_score: int
    status_name: str
    state: str  # pre | in | post
    status: Mapping[str, Any]
    start: datetime | None
    home_stats: dict[str, float] = field(default_factory=dict)
    away_stats: dict[str, float] = field(default_factory=dict)
    home_reds: int = 0
    away_reds: int = 0

    @property
    def live(self) -> bool:
        return self.state == "in"

    @property
    def clock(self) -> tuple[float, int, float | None, str]:
        return espn_clock(self.status)


def _count_reds_from_details(details: Iterable[Mapping[str, Any]] | None, home_id: str, away_id: str) -> tuple[int, int]:
    h = a = 0
    for d in details or []:
        txt = str((d.get("type") or {}).get("text") or "").lower()
        if d.get("redCard") or txt in ("red card", "yellow-red card", "second yellow card"):
            tid = str((d.get("team") or {}).get("id") or "")
            if tid == home_id:
                h += 1
            elif tid == away_id:
                a += 1
    return h, a


# ESPN league ids (``uid`` "s:600~l:{id}~e:...") -> slug; verified against per-league
# scoreboards 25 Oct 2026 (``leagues[0].id``).
LEAGUE_IDS: dict[str, str] = {
    "700": "eng.1", "740": "esp.1", "720": "ger.1", "730": "ita.1", "710": "fra.1",
    "775": "uefa.champions", "776": "uefa.europa",
    "20296": "uefa.europa.conf", "3920": "eng.league_cup",  # verified 2026-10-25
}


def league_id_from_scoreboard(payload: Mapping[str, Any]) -> str | None:
    """League id (``leagues[0].id``) of a per-league scoreboard payload, or None if ESPN has no such league."""
    lgs = payload.get("leagues") if isinstance(payload, Mapping) else None
    if not lgs or not isinstance(lgs[0], Mapping):
        return None
    lid = lgs[0].get("id")
    return str(lid) if lid not in (None, "") else None


def register_league_id(league_id: str, slug: str) -> None:
    LEAGUE_IDS[str(league_id)] = slug


def league_slug_of(event: Mapping[str, Any]) -> str | None:
    """League slug of an event in the ``soccer/all`` scoreboard (from ``uid`` ``l:<id>``)."""
    uid = str(event.get("uid") or "")
    for part in uid.split("~"):
        if part.startswith("l:"):
            return LEAGUE_IDS.get(part[2:])
    return None


def league_id_of(event: Mapping[str, Any]) -> str | None:
    """ESPN league id (``uid`` ``l:<id>``) of a ``soccer/all`` event."""
    for part in str(event.get("uid") or "").split("~"):
        if part.startswith("l:"):
            return part[2:]
    return None


def split_all_scoreboard(payload: Mapping[str, Any], leagues: Sequence[str],
                         auto_discover: bool = False, live_only_auto: bool = True) -> dict[str, dict[str, Any]]:
    """Split the ``soccer/all/scoreboard`` payload into per-league pseudo scoreboards.

    The ``all`` payload has ``leagues: [{}]`` (no slug/name), so the league comes from
    each event's ``uid``. Returns ``{slug: {"leagues": [{"slug", "name"}], "events": [...]}}``
    for the wanted ``leagues`` only - plus, with ``auto_discover`` (v4.3-global), events of
    unlisted leagues under the pseudo slug ``espn.<league id>`` (live events only by default).
    """
    want = set(leagues)
    out: dict[str, dict[str, Any]] = {}
    for e in payload.get("events") or []:
        slug = league_slug_of(e)
        if slug is None or slug not in want:
            if not auto_discover:
                continue
            lid = league_id_of(e)
            if lid is None:
                continue
            if live_only_auto and ((e.get("status") or {}).get("type") or {}).get("state") not in ("in", "post"):
                continue
            slug = f"espn.{lid}"
            if slug not in LEAGUE_NAMES:
                LEAGUE_NAMES[slug] = f"ESPN league {lid}"
        out.setdefault(slug, {"leagues": [{"slug": slug, "name": LEAGUE_NAMES.get(slug, slug)}], "events": []})["events"].append(e)
    return out


def parse_espn_scoreboard(payload: Mapping[str, Any], league: str | None = None) -> list[EspnEvent]:
    """Parse one league scoreboard; malformed events are skipped."""
    lg = (payload.get("leagues") or [{}])[0]
    slug = league or lg.get("slug") or ""
    lname = lg.get("name") or LEAGUE_NAMES.get(slug, slug)
    out: list[EspnEvent] = []
    for e in payload.get("events") or []:
        try:
            comp = (e.get("competitions") or [{}])[0]
            cs = {c.get("homeAway"): c for c in comp.get("competitors") or []}
            h, a = cs["home"], cs["away"]
            status = e.get("status") or comp.get("status") or {}
            hid, aid = str((h.get("team") or {}).get("id") or h.get("id") or ""), str((a.get("team") or {}).get("id") or a.get("id") or "")
            hr, ar = _count_reds_from_details(comp.get("details"), hid, aid)
            out.append(
                EspnEvent(
                    id=str(e["id"]),
                    league=slug,
                    league_name=lname,
                    home=h["team"]["displayName"],
                    away=a["team"]["displayName"],
                    home_id=hid,
                    away_id=aid,
                    home_score=_i(h.get("score")) or 0,
                    away_score=_i(a.get("score")) or 0,
                    status_name=str((status.get("type") or {}).get("name") or ""),
                    state=str((status.get("type") or {}).get("state") or ""),
                    status=status,
                    start=_dt(e.get("date") or comp.get("date")),
                    home_stats=stats_list_to_dict(h.get("statistics")),
                    away_stats=stats_list_to_dict(a.get("statistics")),
                    home_reds=hr,
                    away_reds=ar,
                )
            )
        except (KeyError, TypeError, IndexError) as exc:
            log.debug("skip malformed espn event", extra={"error": repr(exc)})
    return out


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------
RED_KEY_TYPES = {"red-card", "yellow-red-card", "second-yellow-card", "yellow-red"}


@dataclass(slots=True)
class EspnSummary:
    """Parsed ``/{league}/summary?event=``."""

    event_id: str
    home: str
    away: str
    home_stats: dict[str, float]
    away_stats: dict[str, float]
    status: Mapping[str, Any]
    home_score: int | None
    away_score: int | None
    home_reds_events: int = 0
    away_reds_events: int = 0
    key_events: list[dict[str, Any]] = field(default_factory=list)
    prior: SharpPrices | None = None
    prior_provider: str = ""


def parse_espn_key_events(key_events: Iterable[Mapping[str, Any]] | None, home: str, away: str) -> tuple[list[dict[str, Any]], int, int]:
    """Normalise ``keyEvents`` -> ([{type, minute, period, team, text}], home_reds, away_reds)."""
    out: list[dict[str, Any]] = []
    hr = ar = 0
    for k in key_events or []:
        t = str((k.get("type") or {}).get("type") or "").lower()
        team = str((k.get("team") or {}).get("displayName") or "")
        clk = k.get("clock") or {}
        disp = str(clk.get("displayValue") or "")
        m = _DISPLAY_RE.match(disp)
        minute = (float(m.group(1)) + (float(m.group(2)) if m.group(2) else 0.0)) if m else (_f(clk.get("value")) or 0.0) / 60.0
        out.append({"type": t, "minute": minute, "period": (k.get("period") or {}).get("number"), "team": team, "text": k.get("text") or ""})
        if t in RED_KEY_TYPES:
            if team and team == home:
                hr += 1
            elif team and team == away:
                ar += 1
    return out, hr, ar


def american_to_decimal(v: Any) -> float | None:
    """``-275`` -> 1.364, ``+550`` -> 6.5, ``"EVEN"`` -> 2.0."""
    if isinstance(v, str) and v.strip().upper() in ("EVEN", "EV"):
        return 2.0
    x = _f(v)
    if x is None or x == 0 or -100 < x < 100:
        return None
    return round(1 + (x / 100.0 if x > 0 else 100.0 / -x), 4)


def parse_espn_pickcenter(pickcenter: Iterable[Mapping[str, Any]] | None, which: str = "close") -> tuple[SharpPrices | None, str]:
    """Pre-match 1X2 (+ total) from ``pickcenter`` (DraftKings on ESPN) as a ratings prior.

    Uses ``moneyline.{home,draw,away}.{close|open}.odds`` (fallback ``homeTeamOdds.moneyLine``
    etc.) and ``total.{over,under}.close`` with ``overUnder``. Returns (prices, provider).
    These are a *US soft book's* lines - a weaker prior than Pinnacle, never Bet365.
    """
    for pc in pickcenter or []:
        prov = str((pc.get("provider") or {}).get("name") or "")
        ml = pc.get("moneyline") or {}

        def leg(side: str, src: Mapping[str, Any] = ml) -> float | None:
            node = (src.get(side) or {}).get(which) or (src.get(side) or {}).get("close") or {}
            return american_to_decimal(node.get("odds"))

        h = leg("home") or american_to_decimal((pc.get("homeTeamOdds") or {}).get("moneyLine"))
        d = leg("draw") or american_to_decimal((pc.get("drawOdds") or {}).get("moneyLine"))
        a = leg("away") or american_to_decimal((pc.get("awayTeamOdds") or {}).get("moneyLine"))
        if not (h and d and a):
            continue
        line = _f(pc.get("overUnder"))
        tot = pc.get("total") or {}
        over = american_to_decimal(((tot.get("over") or {}).get(which) or {}).get("odds")) or american_to_decimal(pc.get("overOdds"))
        under = american_to_decimal(((tot.get("under") or {}).get(which) or {}).get("odds")) or american_to_decimal(pc.get("underOdds"))
        return SharpPrices(h, d, a, line, over, under, time.time()), prov
    return None, ""


def parse_espn_summary(payload: Mapping[str, Any]) -> EspnSummary:
    """Parse the summary endpoint (box score, status, key events, pickcenter prior)."""
    teams = {t.get("homeAway"): t for t in (payload.get("boxscore") or {}).get("teams") or []}
    header = payload.get("header") or {}
    comp = (header.get("competitions") or [{}])[0]
    cs = {c.get("homeAway"): c for c in comp.get("competitors") or []}
    home = ((teams.get("home") or {}).get("team") or {}).get("displayName") or ((cs.get("home") or {}).get("team") or {}).get("displayName") or ""
    away = ((teams.get("away") or {}).get("team") or {}).get("displayName") or ((cs.get("away") or {}).get("team") or {}).get("displayName") or ""
    kev, hr, ar = parse_espn_key_events(payload.get("keyEvents"), home, away)
    prior, prov = parse_espn_pickcenter(payload.get("pickcenter"))
    return EspnSummary(
        event_id=str(header.get("id") or comp.get("id") or ""),
        home=home,
        away=away,
        home_stats=stats_list_to_dict((teams.get("home") or {}).get("statistics")),
        away_stats=stats_list_to_dict((teams.get("away") or {}).get("statistics")),
        status=comp.get("status") or {},
        home_score=_i((cs.get("home") or {}).get("score")),
        away_score=_i((cs.get("away") or {}).get("score")),
        home_reds_events=hr,
        away_reds_events=ar,
        key_events=kev,
        prior=prior,
        prior_provider=prov,
    )


# ---------------------------------------------------------------------------
# -> LiveMatchState
# ---------------------------------------------------------------------------
def build_state_from_espn(
    ev: EspnEvent, summary: EspnSummary | None, event_key: str, now: float | None = None
) -> LiveMatchState:
    """Merge scoreboard (+ optional summary) into a :class:`LiveMatchState` (``source="espn"``).

    Summary box-score stats override scoreboard stats when present; red cards
    are ``max(boxscore redCards, keyEvents red-card, scoreboard details redCard)``.
    Momentum is left ``None`` - the poller fills it from the DeltaTracker.
    """
    now = time.time() if now is None else now
    status = ev.status
    if summary is not None and (summary.status.get("type") or {}).get("state") == ev.state and summary.status.get("displayClock"):
        status = summary.status  # equally fresh; prefer the scoreboard otherwise
    minute, period, added, label = espn_clock(status)
    hs = dict(ev.home_stats)
    as_ = dict(ev.away_stats)
    if summary is not None:
        hs.update(summary.home_stats)
        as_.update(summary.away_stats)
    reds_h = max(ev.home_reds, int(hs.get("redCards", 0)), summary.home_reds_events if summary else 0)
    reds_a = max(ev.away_reds, int(as_.get("redCards", 0)), summary.away_reds_events if summary else 0)
    ch, ca = hs.get("wonCorners"), as_.get("wonCorners")

    def _opt(d: Mapping[str, float], k: str) -> int | None:
        return int(d[k]) if k in d else None

    return LiveMatchState(
        event_key=event_key,
        minute=minute,
        home_goals=ev.home_score,
        away_goals=ev.away_score,
        home_corners=int(ch or 0),
        away_corners=int(ca or 0),
        home_red_cards=reds_h,
        away_red_cards=reds_a,
        period=period,
        announced_stoppage=added,
        possession_home=hs.get("possessionPct"),
        updated_at=now,
        source="espn",
        source_event_id=ev.id,
        home=ev.home,
        away=ev.away,
        league=ev.league_name,
        league_slug=ev.league,
        commence_time=ev.start,
        added_time=added,
        status=label,
        home_shots_on_target=_opt(hs, "shotsOnTarget"),
        away_shots_on_target=_opt(as_, "shotsOnTarget"),
        home_shots=_opt(hs, "totalShots"),
        away_shots=_opt(as_, "totalShots"),
        home_yellow_cards=_opt(hs, "yellowCards"),
        away_yellow_cards=_opt(as_, "yellowCards"),
        stats_available=ch is not None and ca is not None,
        fetched_at=now,
    )


__all__ = [
    "ESPN_BASE",
    "DEFAULT_LEAGUES",
    "LEAGUE_NAMES",
    "LEAGUE_IDS",
    "CUP_LEAGUES",
    "league_id_from_scoreboard",
    "register_league_id",
    "league_slug_of",
    "split_all_scoreboard",
    "league_id_of",
    "EspnEndpoints",
    "EspnEvent",
    "EspnSummary",
    "espn_clock",
    "parse_espn_scoreboard",
    "parse_espn_summary",
    "parse_espn_key_events",
    "parse_espn_pickcenter",
    "american_to_decimal",
    "stats_list_to_dict",
    "build_state_from_espn",
]
