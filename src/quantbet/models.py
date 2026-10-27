"""Shared domain models used across poller, engine, risk, database and notifier."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class MarketType(str, Enum):
    """Football markets supported by the engine."""

    H2H = "1X2"
    ASIAN_HANDICAP = "ASIAN_HANDICAP"
    TOTAL_GOALS = "TOTAL_GOALS"
    TOTAL_CORNERS = "TOTAL_CORNERS"


class Selection(str, Enum):
    """Canonical outcome names (always relative to the *home/away* of the event)."""

    HOME = "home"
    DRAW = "draw"
    AWAY = "away"
    OVER = "over"
    UNDER = "under"


class Urgency(str, Enum):
    """Urgency tag shown at the top of each notification."""

    STEAM = "STEAM"
    LIVE_MODEL = "LIVE MODEL"
    VERIFIED_TIPSTER = "VERIFIED TIPSTER"
    VALUE = "SHARP VALUE"
    MIN_ODDS = "LIVE MODEL · MIN ODDS"  # model-only alert: check Bet365 against the floor


class BookRole(str, Enum):
    """Role of a bookmaker in the pipeline."""

    TARGET = "target"  # the soft book we bet into (Bet365)
    SHARP = "sharp"  # benchmark book/exchange (Pinnacle, Betfair Exchange)
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class MarketKey:
    """Identifies one market (all outcomes) of one event, independent of bookmaker.

    ``line`` is the handicap/total. For Asian handicap it is the *home* handicap.
    """

    event_key: str
    market: MarketType
    line: float | None = None

    def selections(self) -> tuple[Selection, ...]:
        """Outcomes that make up a complete book for this market."""
        if self.market is MarketType.H2H:
            return (Selection.HOME, Selection.DRAW, Selection.AWAY)
        if self.market is MarketType.ASIAN_HANDICAP:
            return (Selection.HOME, Selection.AWAY)
        return (Selection.OVER, Selection.UNDER)


@dataclass(frozen=True, slots=True)
class OddsSnapshot:
    """One normalised price from one bookmaker at one point in time."""

    provider: str
    bookmaker: str
    provider_event_id: str
    home: str
    away: str
    league: str
    commence_time: datetime | None
    market: MarketType
    line: float | None
    selection: Selection
    price: float
    is_live: bool = False
    timestamp: float = field(default_factory=time.time)
    liquidity: float | None = None
    deep_link: str | None = None
    event_key: str = ""  # canonical key, filled in by the EventMatcher

    def market_key(self) -> MarketKey:
        """Market identity of this snapshot (requires ``event_key`` to be set)."""
        return MarketKey(self.event_key, self.market, self.line)


@dataclass(slots=True)
class MatchState:
    """Live match state from the statistics feed (API-Football / Sportmonks)."""

    event_key: str
    minute: float
    home_goals: int = 0
    away_goals: int = 0
    home_corners: int = 0
    away_corners: int = 0
    home_red_cards: int = 0
    away_red_cards: int = 0
    period: int = 1  # 1 = first half, 2 = second half
    announced_stoppage: float | None = None
    # Rolling 10-minute counts (computed by the poller's StatsTracker).
    home_dangerous_attacks_10: float | None = None
    away_dangerous_attacks_10: float | None = None
    home_shots_on_target_10: float | None = None
    away_shots_on_target_10: float | None = None
    home_xg: float | None = None
    away_xg: float | None = None
    possession_home: float | None = None
    updated_at: float = field(default_factory=time.time)

    @property
    def score(self) -> str:
        """Scoreline formatted as ``H-A``."""
        return f"{self.home_goals}-{self.away_goals}"


@dataclass(slots=True)
class LiveMatchState(MatchState):
    """MatchState enriched with the free-source (ESPN / SofaScore / FotMob) fields.

    It *is* a :class:`MatchState`, so Engine B accepts it unchanged. Extra fields:

    * ``attack_momentum`` – rolling 10-minute mean of SofaScore "attack momentum"
      graph values (or FotMob momentum), normalised to [-1, 1]; positive = home pressure.
    * cumulative shots on target / inside box, xG, kick-off and data-source metadata.
    """

    source: str = ""
    source_event_id: str = ""
    home: str = ""
    away: str = ""
    league: str = ""
    league_slug: str = ""  # ESPN slug (e.g. "arg.1"; auto-discovered "espn.<id>") - v4.3-global tiers/baselines
    commence_time: datetime | None = None
    added_time: float | None = None  # minutes shown as "+N" (e.g. 90+3 -> 3)
    status: str = ""  # "1st half" | "halftime" | "2nd half" | "ended" | ...
    home_shots_on_target: int | None = None
    away_shots_on_target: int | None = None
    home_shots_inside_box: int | None = None
    away_shots_inside_box: int | None = None
    attack_momentum: float | None = None
    momentum_points: int = 0  # number of graph points used for the rolling average
    stats_available: bool = False  # True if corners/shots were parsed from a stats payload
    fetched_at: float = field(default_factory=time.time)
    # ESPN / delta-tracker / enrichment extras (all optional).
    home_shots: int | None = None  # cumulative total shots
    away_shots: int | None = None
    home_yellow_cards: int | None = None
    away_yellow_cards: int | None = None
    momentum_source: str = ""  # "graph" (SofaScore/FotMob) | "synthetic" (DeltaTracker) | ""
    velocity: dict[str, float] = field(default_factory=dict)  # per-minute rates, last ~10 match minutes
    enrichments: tuple[str, ...] = ()
    # DeltaTracker activity multipliers (Engine B uses ONLY these for live pressure when set).
    momentum_mult_home: float | None = None
    momentum_mult_away: float | None = None
    momentum_mult_total: float | None = None
    activity_10m: dict[str, float] = field(default_factory=dict)  # e.g. ("api_football", "fotmob_page")

    @property
    def total_corners(self) -> int:
        """Current total corners."""
        return self.home_corners + self.away_corners

    @property
    def total_goals(self) -> int:
        """Current total goals."""
        return self.home_goals + self.away_goals


@dataclass(slots=True)
class EventInfo:
    """Canonical event metadata maintained by the EventMatcher."""

    event_key: str
    home: str
    away: str
    league: str
    commence_time: datetime | None
    is_live: bool = False
    provider_ids: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Signal:
    """A +EV opportunity ready for risk sizing, logging and notification."""

    urgency: Urgency
    engine: str  # "A" (sharp de-vig), "B" (live model) or "TIPSTER"
    event_key: str
    home: str
    away: str
    league: str
    market: MarketType
    line: float | None
    selection: Selection
    bookmaker: str
    bookmaker_odds: float
    p_true: float  # effective probability: EV = p_true * odds - 1
    fair_odds: float
    min_acceptable_odds: float
    ev: float
    kelly_fraction: float  # already multiplied by the Kelly multiplier
    is_live: bool = False
    minute: float | None = None
    score: str | None = None
    commence_time: datetime | None = None
    method: str = ""
    sharp_source: str | None = None
    stake_units: float = 0.0
    stake_currency: float = 0.0
    currency: str = "EUR"
    high_limit_risk: bool = False
    risk_notes: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    signal_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def describe_market(self) -> str:
        """Human readable market/line, e.g. ``Over 10.5 Corners``."""
        sel = self.selection.value.capitalize()
        if self.market is MarketType.H2H:
            return {"home": f"{self.home} to win", "draw": "Draw", "away": f"{self.away} to win"}[
                self.selection.value
            ]
        if self.market is MarketType.ASIAN_HANDICAP:
            assert self.line is not None
            line = self.line if self.selection is Selection.HOME else -self.line
            team = self.home if self.selection is Selection.HOME else self.away
            return f"Asian Handicap {team} {line:+g}"
        unit = "Goals" if self.market is MarketType.TOTAL_GOALS else "Corners"
        return f"{sel} {self.line:g} {unit}"
