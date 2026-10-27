"""FotMob match-page enricher - Tier 3 (xG, shots inside box, momentum graph).

Plain, honest GET of the public match page (``https://www.fotmob.com/match/{id}``,
redirects followed) and parsing of the Next.js ``<script id="__NEXT_DATA__">``
JSON at ``props.pageProps`` (``general``, ``header``, ``content.stats``,
``content.momentum``). Match ids come from the public search suggest endpoint
``https://apigw.fotmob.com/searchapi/suggest?term=...``.

Verified 2026-10-25 with an honest User-Agent: both returned HTTP 200 (page
~0.9 MB with full stats incl. ``expected_goals``). This is undocumented page
structure - it can change at any time and automated use may be against
FotMob's terms. **Disabled by default** (``QB_ENABLE_FOTMOB_PAGE=true`` to opt
in); when enabled it only enriches *flagged* matches and at most every
``min_interval_s`` (default 300 s) per match. No evasion of any kind: if the
page is blocked, the PoliteFetcher backs off and the engine runs on ESPN only.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

from .free_stats_poller import parse_fotmob_match_details
from .models import LiveMatchState
from .polite_http import PoliteFetcher
from .poller import team_similarity

log = logging.getLogger(__name__)


def extract_next_data(html: str) -> dict[str, Any] | None:
    """Return the parsed ``__NEXT_DATA__`` JSON of a Next.js page (or ``None``)."""
    if not html:
        return None
    try:
        from bs4 import BeautifulSoup

        tag = BeautifulSoup(html, "html.parser").find("script", id="__NEXT_DATA__")
        raw = tag.string if tag else None
    except Exception:  # noqa: BLE001 - bs4 missing / malformed HTML
        raw = None
    if raw is None:
        i = html.find('id="__NEXT_DATA__"')
        if i < 0:
            return None
        j, k = html.find(">", i), html.find("</script>", i)
        raw = html[j + 1 : k] if 0 <= j < k else None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def page_props(next_data: dict[str, Any] | None) -> dict[str, Any] | None:
    pp = ((next_data or {}).get("props") or {}).get("pageProps")
    return pp if isinstance(pp, dict) and pp.get("content") is not None else None


def parse_fotmob_page(html: str, event_key: str = "", now: float | None = None) -> LiveMatchState | None:
    """Match page HTML -> :class:`LiveMatchState` (``source="fotmob"``) or ``None`` if structure differs."""
    pp = page_props(extract_next_data(html))
    if pp is None:
        return None
    return parse_fotmob_match_details(pp, event_key, now)


@dataclass(slots=True)
class SuggestHit:
    id: str
    home: str
    away: str
    league: str
    date: datetime | None
    score: float


def parse_suggest(payload: dict[str, Any], home: str, away: str, kickoff: datetime | None = None, threshold: float = 70.0) -> SuggestHit | None:
    """Pick the fixture from ``searchapi/suggest`` matching both team names (and date if given)."""
    best: SuggestHit | None = None
    for grp in payload.get("matchSuggest") or []:
        for opt in grp.get("options") or []:
            p = opt.get("payload") or {}
            try:
                d = datetime.fromisoformat(str(p.get("matchDate")).replace("Z", "+00:00")) if p.get("matchDate") else None
            except ValueError:
                d = None
            if kickoff and d and abs((d - kickoff).total_seconds()) > 36 * 3600:
                continue
            sc = min(team_similarity(home, str(p.get("homeName") or "")), team_similarity(away, str(p.get("awayName") or "")))
            if sc >= threshold and (best is None or sc > best.score):
                best = SuggestHit(str(p.get("id")), p.get("homeName", ""), p.get("awayName", ""), p.get("leagueName", ""), d, sc)
    return best


@dataclass(frozen=True, slots=True)
class FotmobPageConfig:
    page_tpl: str = "https://www.fotmob.com/match/{id}"
    suggest_tpl: str = "https://apigw.fotmob.com/searchapi/suggest?term={term}&lang=en"
    min_interval_s: float = 300.0
    suggest_ttl_s: float = 6 * 3600.0


class FotmobPageEnricher:
    """Adds xG / shots inside box / FotMob momentum to an existing (ESPN) state."""

    def __init__(self, fetcher: PoliteFetcher, cfg: FotmobPageConfig | None = None) -> None:
        self.f = fetcher
        self.cfg = cfg or FotmobPageConfig()
        self._ids: dict[str, str | None] = {}
        self._last: dict[str, tuple[float, LiveMatchState | None]] = {}

    async def find_match_id(self, st: LiveMatchState) -> str | None:
        if st.event_key in self._ids:
            return self._ids[st.event_key]
        term = quote(st.home)
        r = await self.f.get_json(self.cfg.suggest_tpl.format(term=term), cache_ttl_s=self.cfg.suggest_ttl_s)
        hit = parse_suggest(r.data, st.home, st.away, st.commence_time) if r.ok and isinstance(r.data, dict) else None
        if r.ok or not r.blocked:
            self._ids[st.event_key] = hit.id if hit else None  # remember misses too (no retry storm)
        return hit.id if hit else None

    async def enrich(self, st: LiveMatchState, now: float | None = None) -> LiveMatchState:
        """Mutates and returns ``st``; no-op if not due, unmatched, blocked or unparsable."""
        now = time.time() if now is None else now
        last = self._last.get(st.event_key)
        fm = last[1] if last else None
        if last is None or now - last[0] >= self.cfg.min_interval_s:
            mid = await self.find_match_id(st)
            if mid is None:
                return st
            r = await self.f.get_text(self.cfg.page_tpl.format(id=mid), cache_ttl_s=self.cfg.min_interval_s * 0.9)
            fm = parse_fotmob_page(r.data, st.event_key, now) if r.ok else None
            if r.ok and fm is None:
                log.warning("fotmob page structure not recognised - enrichment skipped", extra={"match_id": mid})
            self._last[st.event_key] = (now, fm)
        if fm is None:
            return st
        if fm.home_xg is not None:
            st.home_xg, st.away_xg = fm.home_xg, fm.away_xg
        if fm.home_shots_inside_box is not None:
            st.home_shots_inside_box, st.away_shots_inside_box = fm.home_shots_inside_box, fm.away_shots_inside_box
        if fm.attack_momentum is not None and fm.momentum_points > 0 and abs(fm.minute - st.minute) <= 6:
            st.attack_momentum, st.momentum_points, st.momentum_source = fm.attack_momentum, fm.momentum_points, "graph"
        st.home_red_cards = max(st.home_red_cards, fm.home_red_cards)
        st.away_red_cards = max(st.away_red_cards, fm.away_red_cards)
        st.enrichments = (*st.enrichments, "fotmob_page")
        return st


__all__ = ["xg_confirm", "fotmob_dominance", "extract_next_data", "page_props", "parse_fotmob_page", "parse_suggest", "SuggestHit", "FotmobPageConfig", "FotmobPageEnricher"]


def xg_confirm(selection: str, minute: float, home_xg: float | None, away_xg: float | None,
               sib_home: int | None, sib_away: int | None, lambda_pre_total: float | None,
               sib_baseline_90: float = 15.0, tol: float = 0.10) -> str:
    """Confirm rule for a model-only alert with FotMob xG / shots inside the box.

    Pace = value so far / minute * 90, compared with the pre-match expectation
    (``lambda_pre_total`` goals; ``sib_baseline_90`` combined shots in the box).
    Over: ``confirmed`` if any available pace >= (1 - tol) x expectation,
    ``contradicted`` if all available paces are below it. Under: mirrored at
    (1 + tol). ``unavailable`` if FotMob gave neither metric.
    """
    m = max(float(minute), 1.0)
    ratios: list[float] = []
    if home_xg is not None and away_xg is not None and lambda_pre_total:
        ratios.append((home_xg + away_xg) / m * 90.0 / lambda_pre_total)
    if sib_home is not None and sib_away is not None and sib_baseline_90:
        ratios.append((sib_home + sib_away) / m * 90.0 / sib_baseline_90)
    if not ratios:
        return "unavailable"
    if selection == "over":
        return "confirmed" if any(r >= 1 - tol for r in ratios) else "contradicted"
    return "confirmed" if all(r <= 1 + tol for r in ratios) else "contradicted"


def fotmob_dominance(side: str, home_xg: float | None, away_xg: float | None,
                     sib_home: int | None, sib_away: int | None) -> str:
    """Dominance check for late corner Overs (v3 rule 4).

    ``confirmed`` if ``side`` (the chasing favourite, else the side with the higher live
    activity multiplier) leads the opponent in **both** xG and shots inside the box in the
    latest FotMob match totals; ``contradicted`` otherwise; ``unavailable`` if either metric
    is missing (the caller suppresses the alert in that case).
    """
    if None in (home_xg, away_xg, sib_home, sib_away):
        return "unavailable"
    mine_xg, opp_xg = (home_xg, away_xg) if side == "home" else (away_xg, home_xg)
    mine_sib, opp_sib = (sib_home, sib_away) if side == "home" else (sib_away, sib_home)
    return "confirmed" if (mine_xg > opp_xg and mine_sib > opp_sib) else "contradicted"
