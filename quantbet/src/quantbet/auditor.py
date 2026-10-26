"""Tipster auditing: CLV vs Pinnacle closing lines, significance tests, fade detection,
and live price verification of new tips.

Data inputs
-----------
* Tipster history: CSV with columns
  ``date,home,away,market,selection,line,odds,stake,result`` (result optional:
  win|half_win|push|half_loss|loss|void). Exports from Blogabet/OLBG/Reddit or a
  Telegram channel dump can be converted to this format.
* Closing odds: Football-Data.co.uk season CSVs (columns ``PSCH/PSCD/PSCA``
  Pinnacle closing 1X2, ``PC>2.5/PC<2.5`` closing O/U 2.5, ``AHCh/PCAHH/PCAHA``
  closing Asian handicap).

Live tips: :class:`TelethonTipListener` (optional, needs ``telethon`` and your
own Telegram API id/hash) parses new channel posts with :func:`parse_tip_text`
and hands them to :meth:`TipsterAuditor.verify_live_tip`, which only emits a
signal if the *current* Bet365 price still has EV >= threshold.
"""

from __future__ import annotations

import csv
import logging
import math
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path

import numpy as np
from scipy import stats

from .engine import SignalEngine, devig, expected_value, fair_odds, kelly_fraction, min_acceptable_odds
from .models import MarketKey, MarketType, Selection, Signal, Urgency
from .poller import team_similarity

log = logging.getLogger(__name__)

_PAYOUT = {"win": 1.0, "half_win": 0.5, "push": 0.0, "half_loss": -0.5, "loss": -1.0, "void": 0.0}


class TipsterVerdict(str, Enum):
    """Classification of a tipster after auditing."""

    WHITELIST = "WHITELIST"  # significant +CLV
    FADE = "FADE"  # significant negative yield over a large sample
    NEUTRAL = "NEUTRAL"
    INSUFFICIENT = "INSUFFICIENT_SAMPLE"


@dataclass(frozen=True, slots=True)
class TipsterPick:
    """One historical or live pick."""

    tipster: str
    day: date
    home: str
    away: str
    market: MarketType
    selection: Selection
    line: float | None
    odds: float
    stake: float = 1.0
    result: str | None = None


@dataclass(frozen=True, slots=True)
class ClosingRow:
    """Pinnacle closing prices for one match (Football-Data.co.uk)."""

    day: date
    home: str
    away: str
    h2h: tuple[float, float, float] | None
    ou25: tuple[float, float] | None
    ah_line: float | None
    ah: tuple[float, float] | None


@dataclass(frozen=True, slots=True)
class AuditResult:
    """Outcome of :meth:`TipsterAuditor.audit`."""

    tipster: str
    n_picks: int
    n_matched: int
    mean_clv: float  # mean of odds / no-vig closing odds (1.02 = +2%)
    clv_t: float
    clv_p_value: float  # one-sided H1: mean CLV > 1
    n_settled: int
    yield_pct: float
    yield_p_value_neg: float  # one-sided H1: yield < 0
    verdict: TipsterVerdict


def _parse_date(s: str) -> date:
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unparseable date {s!r}")


def _f(row: dict[str, str], key: str) -> float | None:
    v = row.get(key, "").strip()
    try:
        x = float(v)
    except ValueError:
        return None
    return x if x > 1.0 else None


def load_football_data_csv(path: Path | str) -> list[ClosingRow]:
    """Parse a Football-Data.co.uk CSV into :class:`ClosingRow` objects."""
    rows: list[ClosingRow] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            if not r.get("HomeTeam"):
                continue
            h, d, a = _f(r, "PSCH"), _f(r, "PSCD"), _f(r, "PSCA")
            o, u = _f(r, "PC>2.5"), _f(r, "PC<2.5")
            ah_h, ah_a = _f(r, "PCAHH"), _f(r, "PCAHA")
            try:
                ah_line = float(r.get("AHCh", "") or "nan")
            except ValueError:
                ah_line = math.nan
            rows.append(
                ClosingRow(
                    _parse_date(r["Date"]),
                    r["HomeTeam"],
                    r["AwayTeam"],
                    (h, d, a) if h and d and a else None,
                    (o, u) if o and u else None,
                    None if math.isnan(ah_line) else ah_line,
                    (ah_h, ah_a) if ah_h and ah_a else None,
                )
            )
    return rows


def load_picks_csv(path: Path | str, tipster: str) -> list[TipsterPick]:
    """Load a tipster history CSV (see module docstring for columns)."""
    picks: list[TipsterPick] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            line = r.get("line", "").strip()
            picks.append(
                TipsterPick(
                    tipster,
                    _parse_date(r["date"]),
                    r["home"],
                    r["away"],
                    MarketType(r["market"]),
                    Selection(r["selection"].lower()),
                    float(line) if line else None,
                    float(r["odds"]),
                    float(r.get("stake") or 1.0),
                    (r.get("result") or "").strip() or None,
                )
            )
    return picks


_TIP_RE = re.compile(
    r"(?P<home>[\w .'&-]+?)\s+(?:vs?\.?|-|v)\s+(?P<away>[\w .'&-]+?)\s*[:|,-]?\s+"
    r"(?P<sel>over|under|home|away|draw|1|x|2)\s*(?P<line>[+-]?\d+(?:\.\d+)?)?\s*"
    r"(?P<kind>goals|corners|ah|asian handicap)?\s*@\s*(?P<odds>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def parse_tip_text(text: str, tipster: str, today: date | None = None) -> TipsterPick | None:
    """Parse free-text tips such as ``"Arsenal vs Chelsea Over 2.5 goals @ 1.95"``.

    Supported: 1X2 (``1/X/2/home/draw/away``), Over/Under goals or corners and
    Asian handicap (``home -0.75 ah @ 1.9``). Returns ``None`` if not parseable.
    """
    m = _TIP_RE.search(text)
    if not m:
        return None
    sel_raw = m["sel"].lower()
    sel_map = {"1": "home", "x": "draw", "2": "away"}
    sel = Selection(sel_map.get(sel_raw, sel_raw))
    kind = (m["kind"] or "").lower()
    line = float(m["line"]) if m["line"] else None
    if sel in (Selection.OVER, Selection.UNDER):
        market = MarketType.TOTAL_CORNERS if kind == "corners" else MarketType.TOTAL_GOALS
        if line is None:
            return None
    elif line is not None or kind in ("ah", "asian handicap"):
        market = MarketType.ASIAN_HANDICAP
        if line is None or sel is Selection.DRAW:
            return None
        line = line if sel is Selection.HOME else -line  # store as home handicap
    else:
        market = MarketType.H2H
    return TipsterPick(
        tipster, today or date.today(), m["home"].strip(), m["away"].strip(), market, sel, line, float(m["odds"])
    )


class TipsterAuditor:
    """Audits tipsters against no-vig Pinnacle closing lines."""

    def __init__(
        self,
        closing: Iterable[ClosingRow],
        devig_method: str = "power",
        min_sample_whitelist: int = 500,
        min_sample_fade: int = 1000,
        clv_threshold: float = 1.02,
        alpha: float = 0.05,
        team_match_threshold: float = 85.0,
    ) -> None:
        self.closing = list(closing)
        self.devig_method = devig_method
        self.min_sample_whitelist = min_sample_whitelist
        self.min_sample_fade = min_sample_fade
        self.clv_threshold = clv_threshold
        self.alpha = alpha
        self.team_match_threshold = team_match_threshold
        self._by_day: dict[date, list[ClosingRow]] = {}
        for r in self.closing:
            self._by_day.setdefault(r.day, []).append(r)

    def _find(self, pick: TipsterPick) -> ClosingRow | None:
        best, best_score = None, 0.0
        for delta in (0, -1, 1):
            d = date.fromordinal(pick.day.toordinal() + delta)
            for r in self._by_day.get(d, []):
                sc = min(team_similarity(pick.home, r.home), team_similarity(pick.away, r.away))
                if sc > best_score:
                    best, best_score = r, sc
        return best if best_score >= self.team_match_threshold else None

    def closing_fair_odds(self, pick: TipsterPick) -> float | None:
        """No-vig Pinnacle closing odds for the pick's selection, if available."""
        r = self._find(pick)
        if r is None:
            return None
        if pick.market is MarketType.H2H and r.h2h:
            p = devig(list(r.h2h), self.devig_method)
            idx = {Selection.HOME: 0, Selection.DRAW: 1, Selection.AWAY: 2}[pick.selection]
            return 1.0 / p[idx]
        if pick.market is MarketType.TOTAL_GOALS and r.ou25 and pick.line == 2.5:
            p = devig(list(r.ou25), self.devig_method)
            return 1.0 / p[0 if pick.selection is Selection.OVER else 1]
        if (
            pick.market is MarketType.ASIAN_HANDICAP
            and r.ah
            and r.ah_line is not None
            and pick.line is not None
            and abs(r.ah_line - pick.line) < 1e-9
        ):
            p = devig(list(r.ah), self.devig_method)
            return 1.0 / p[0 if pick.selection is Selection.HOME else 1]
        return None

    def audit(self, picks: list[TipsterPick]) -> AuditResult:
        """Compute CLV & yield statistics and classify the tipster."""
        tipster = picks[0].tipster if picks else "?"
        clvs: list[float] = []
        for p in picks:
            close = self.closing_fair_odds(p)
            if close:
                clvs.append(p.odds / close)
        returns = [
            _PAYOUT[p.result] * ((p.odds - 1.0) if _PAYOUT[p.result] > 0 else 1.0)
            for p in picks
            if p.result in _PAYOUT and p.result != "void"
        ]
        clv_arr, ret_arr = np.array(clvs), np.array(returns)
        if clv_arr.size >= 2 and clv_arr.std(ddof=1) > 0:
            t_res = stats.ttest_1samp(clv_arr, 1.0, alternative="greater")
            clv_t, clv_p = float(t_res.statistic), float(t_res.pvalue)
        else:
            clv_t, clv_p = 0.0, 1.0
        if ret_arr.size >= 2 and ret_arr.std(ddof=1) > 0:
            y_p = float(stats.ttest_1samp(ret_arr, 0.0, alternative="less").pvalue)
        else:
            y_p = 1.0
        mean_clv = float(clv_arr.mean()) if clv_arr.size else 0.0
        yld = float(ret_arr.mean() * 100.0) if ret_arr.size else 0.0
        if clv_arr.size >= self.min_sample_whitelist and mean_clv >= self.clv_threshold and clv_p < self.alpha:
            verdict = TipsterVerdict.WHITELIST
        elif ret_arr.size >= self.min_sample_fade and yld < 0 and y_p < self.alpha:
            verdict = TipsterVerdict.FADE
        elif clv_arr.size < self.min_sample_whitelist and ret_arr.size < self.min_sample_fade:
            verdict = TipsterVerdict.INSUFFICIENT
        else:
            verdict = TipsterVerdict.NEUTRAL
        return AuditResult(
            tipster, len(picks), int(clv_arr.size), mean_clv, clv_t, clv_p, int(ret_arr.size), yld, y_p, verdict
        )

    @staticmethod
    def verify_live_tip(
        pick: TipsterPick,
        engine: SignalEngine,
        event_key: str,
        ev_threshold: float = 0.025,
        min_edge_for_floor: float = 0.015,
    ) -> Signal | None:
        """Check the *current* Bet365 price for a whitelisted tipster's pick.

        ``p_true`` comes from the live sharp no-vig line (Engine A). Returns a
        ``VERIFIED_TIPSTER`` signal only if EV >= ``ev_threshold`` right now;
        otherwise the tip is suppressed (``None``).
        """
        mk = MarketKey(event_key, pick.market, pick.line)
        fair = engine.sharp_fair_probabilities(mk)
        odds = engine.current_target_odds(mk, pick.selection)
        if fair is None or odds is None:
            return None
        p = fair[0].get(pick.selection)
        if not p:
            return None
        ev = expected_value(p, odds)
        if ev < ev_threshold:
            log.info("tip suppressed", extra={"tipster": pick.tipster, "ev": ev, "odds": odds})
            return None
        info = engine.events.get(event_key)
        return Signal(
            urgency=Urgency.VERIFIED_TIPSTER,
            engine="TIPSTER",
            event_key=event_key,
            home=info.home if info else pick.home,
            away=info.away if info else pick.away,
            league=info.league if info else "",
            market=pick.market,
            line=pick.line,
            selection=pick.selection,
            bookmaker=engine.cfg.target_bookmaker,
            bookmaker_odds=odds,
            p_true=p,
            fair_odds=fair_odds(p),
            min_acceptable_odds=min_acceptable_odds(p, min_edge_for_floor),
            ev=ev,
            kelly_fraction=kelly_fraction(p, odds, engine.cfg.kelly_multiplier),
            is_live=bool(info and info.is_live),
            commence_time=info.commence_time if info else None,
            method=f"devig:{engine.cfg.devig_method}",
            sharp_source=fair[1],
            extra={"tipster": pick.tipster, "tipped_odds": pick.odds},
        )


class TelethonTipListener:
    """Optional live listener for public Telegram channels you have joined.

    Uses *your own* Telegram API credentials (https://my.telegram.org). Requires
    ``pip install telethon``. Only whitelisted channels are processed.
    """

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        channels: dict[str, str],  # channel username -> tipster name (whitelisted)
        on_pick: Callable[[TipsterPick], Awaitable[None]],
        session_name: str = "quantbet_tips",
    ) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        self.channels = channels
        self.on_pick = on_pick
        self.session_name = session_name

    async def run(self) -> None:
        """Connect and process new messages until disconnected."""
        from telethon import TelegramClient, events  # type: ignore[import-not-found]

        client = TelegramClient(self.session_name, self.api_id, self.api_hash)

        @client.on(events.NewMessage(chats=list(self.channels)))  # type: ignore[misc]
        async def _handler(event: object) -> None:
            chat = getattr(getattr(event, "chat", None), "username", "") or ""
            tipster = self.channels.get(chat, chat)
            pick = parse_tip_text(getattr(event, "raw_text", "") or "", tipster)
            if pick:
                await self.on_pick(pick)

        await client.start()
        await client.run_until_disconnected()
