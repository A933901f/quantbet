"""Layer 2 – optional aggregator odds adapters (disabled by default).

A pluggable :class:`AggregatorOddsSource` interface turns *public* odds
widgets of stats/aggregator sites into :class:`~quantbet.models.OddsSnapshot`
objects so the direct +EV path can run. Only a SofaScore adapter is provided
(``/event/{id}/odds/{provider}/all``, undocumented and unverified). Which
bookmaker SofaScore shows depends on its region/provider id – you must set
``label`` yourself (e.g. ``"bet365"`` only if you have confirmed that the
provider id is Bet365 for your region). Flashscore / OddsPortal are **not**
implemented; their terms generally prohibit scraping.

No request is ever sent to a bookmaker domain (enforced by PoliteFetcher).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from datetime import datetime
from fractions import Fraction
from typing import Any, Protocol

from .models import MarketType, OddsSnapshot, Selection
from .polite_http import PoliteFetcher

log = logging.getLogger(__name__)


class AggregatorOddsSource(Protocol):
    """Interface for optional odds sources (Layer 2)."""

    name: str
    enabled: bool

    async def fetch_event_odds(
        self, source_event_id: str, event_key: str, home: str, away: str, league: str,
        commence_time: datetime | None, is_live: bool,
    ) -> list[OddsSnapshot]:
        """Return normalised snapshots for one event (empty list if unavailable)."""
        ...


def fractional_to_decimal(frac: str | None) -> float | None:
    """``"4/5"`` -> 1.8, ``"evs"`` -> 2.0; ``None`` on garbage."""
    if not frac:
        return None
    s = str(frac).strip().lower()
    if s in ("evs", "evens"):
        return 2.0
    try:
        return round(1.0 + float(Fraction(s)), 4)
    except (ValueError, ZeroDivisionError):
        try:
            v = float(s)
            return v if v > 1.0 else None
        except ValueError:
            return None


_SOFA_MARKETS: dict[str, MarketType] = {
    "full time": MarketType.H2H,
    "1x2": MarketType.H2H,
    "match goals": MarketType.TOTAL_GOALS,
    "total goals": MarketType.TOTAL_GOALS,
    "asian handicap": MarketType.ASIAN_HANDICAP,
    "corners 2-way": MarketType.TOTAL_CORNERS,
    "total corners": MarketType.TOTAL_CORNERS,
    "corners": MarketType.TOTAL_CORNERS,
}
_SOFA_CHOICES: dict[str, Selection] = {"1": Selection.HOME, "x": Selection.DRAW, "2": Selection.AWAY,
                                      "over": Selection.OVER, "under": Selection.UNDER}


def _line(group: Any) -> float | None:
    if group in (None, ""):
        return None
    try:
        parts = [float(x) for x in str(group).replace(" ", "").split(",")]
        return sum(parts) / len(parts)
    except ValueError:
        return None


def parse_sofascore_odds(
    payload: Mapping[str, Any], event_key: str, home: str, away: str, league: str,
    commence_time: datetime | None, label: str, source_event_id: str = "", now: float | None = None,
) -> list[OddsSnapshot]:
    """Parse SofaScore ``/event/{id}/odds/{provider}/all`` into snapshots.

    Asian handicap ``choiceGroup`` is taken as the *home* handicap; choices
    named ``"(-0.5) Home"``-style are mapped via their position (first = home).
    Suspended markets are skipped.
    """
    now = time.time() if now is None else now
    out: list[OddsSnapshot] = []
    for m in payload.get("markets") or []:
        mt = _SOFA_MARKETS.get(str(m.get("marketName", "")).strip().lower())
        if mt is None or m.get("suspended"):
            continue
        line = _line(m.get("choiceGroup"))
        if mt is not MarketType.H2H and line is None:
            continue
        live = bool(m.get("isLive"))
        choices = m.get("choices") or []
        for idx, ch in enumerate(choices):
            name = str(ch.get("name", "")).strip().lower()
            sel = _SOFA_CHOICES.get(name)
            if mt is MarketType.ASIAN_HANDICAP:
                sel = Selection.HOME if idx == 0 else Selection.AWAY
            if sel is None:
                continue
            price = fractional_to_decimal(ch.get("fractionalValue"))
            if price is None or price <= 1.0:
                continue
            out.append(OddsSnapshot(
                "sofascore", label, source_event_id, home, away, league, commence_time, mt,
                line if mt is not MarketType.H2H else None, sel, price, live, now, event_key=event_key,
            ))
    return out


class SofaScoreOddsSource:
    """SofaScore odds widget adapter (Layer 2). Disabled unless explicitly enabled."""

    name = "sofascore_odds"

    def __init__(self, fetcher: PoliteFetcher, base: str = "https://api.sofascore.com/api/v1",
                 provider_id: int = 1, label: str = "sofascore_provider", enabled: bool = False,
                 cache_ttl_s: float = 30.0) -> None:
        self.f = fetcher
        self.base = base.rstrip("/")
        self.provider_id = provider_id
        self.label = label
        self.enabled = enabled
        self.cache_ttl_s = cache_ttl_s

    async def fetch_event_odds(
        self, source_event_id: str, event_key: str, home: str, away: str, league: str,
        commence_time: datetime | None, is_live: bool,
    ) -> list[OddsSnapshot]:
        """Fetch and parse one event's odds (empty on block/error)."""
        if not self.enabled:
            return []
        url = f"{self.base}/event/{source_event_id}/odds/{self.provider_id}/all"
        r = await self.f.get_json(url, cache_ttl_s=self.cache_ttl_s)
        if not r.ok:
            return []
        return parse_sofascore_odds(r.data, event_key, home, away, league, commence_time, self.label, source_event_id)
