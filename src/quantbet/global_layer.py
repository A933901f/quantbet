"""v4.3-global layer: league registry + liquidity tiers, live-stats integrity gate, slug baselines, tier sizing.

The v4.2 maths (Overs only, +6% lift gate, minute windows, diff>=3 block, 5% match cap, 1 goals + 1 corners
alert per match, best line per family, quarter-Kelly, EV >= +2.5%, abort floor +1.5%) is untouched: rules
``v4.3-global`` reuses the v4.2 live/pricing fields verbatim (see ``engine._LIVE_RULE_FIELDS``). Everything
here only (a) chooses which matches are fed to Engine B, (b) supplies league baselines/priors keyed by ESPN
slug instead of league-name substrings, and (c) scales stakes by liquidity tier. Its parameters are frozen
separately (``frozen_sets["v4.3-global"]["global"]`` in config/engine_b_params.json).

Liquidity tiers (config/leagues.json):

* ``TIER_1`` top-5 Europe, UCL/UEL, MLS (usa.1), Brasileirão (bra.1): quarter-Kelly (x1.0).
* ``TIER_2`` second tiers / mid leagues (eng.2, esp.2, ger.2, ita.2, fra.2, ned.1, mex.1, arg.1, Nordics, aus.1,
  jpn.1, + por.1, bel.1, tur.1, sco.1, aut.1, sui.1, gre.1, UECL, Libertadores, top-5 domestic cups): x1.0.
* ``TIER_3`` eng.3, thin markets (bra.2, col.1, chi.1, chn.1, Sudamericana, AFC CL) and anything auto-discovered:
  x0.5 (eighth-Kelly) and capped at 15 units.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .engine import (FreeSignalEngine, PreMatchRatings, RatingsResolver, devig, ratings_from_prices)
from .models import MatchState, Signal

log = logging.getLogger(__name__)

TIERS = ("TIER_1", "TIER_2", "TIER_3")
TIER_LABELS = {"TIER_1": "TIER 1 - MAJOR LEAGUE", "TIER_2": "TIER 2 - MID LIQUIDITY", "TIER_3": "TIER 3 - SMALL LEAGUE"}
# User-supplied corner baselines (total corners per match); default for any other league 9.8.
USER_CORNER_BASELINES: dict[str, float] = {"aus.1": 11.2, "eng.2": 10.4, "bra.1": 10.5, "usa.1": 9.8, "mex.1": 9.7,
                                           "arg.1": 9.6, "jpn.1": 9.6}
DEFAULT_CORNER_BASELINE = 9.8
DEFAULT_GOALS_BASELINE = 2.70
GOAL_SUPREMACY_DEFAULT = 0.30  # home advantage (goals), as LEAGUE_DEFAULTS
CORNER_SUPREMACY_DEFAULT = 0.90
AUTO_PREFIX = "espn."  # auto-discovered slug = "espn.<league id>"
SKIP_NO_TELEMETRY = "skipped: no_live_telemetry"


@dataclass(frozen=True, slots=True)
class LeagueInfo:
    slug: str
    name: str
    region: str = ""
    tier: str = "TIER_3"
    espn_id: str | None = None
    corner_baseline: float | None = None
    goals_baseline: float | None = None
    football_data: str | None = None
    supported: bool = True
    note: str = ""
    discovered: bool = False


@dataclass(slots=True)
class LeagueRegistry:
    """Configured leagues by ESPN slug; unknown/auto-discovered slugs fall back to ``defaults``."""

    leagues: dict[str, LeagueInfo] = field(default_factory=dict)
    default_tier: str = "TIER_3"
    default_corner_baseline: float = DEFAULT_CORNER_BASELINE
    default_goals_baseline: float = DEFAULT_GOALS_BASELINE
    auto_discovery: bool = True
    discovered: dict[str, LeagueInfo] = field(default_factory=dict)

    def get(self, slug: str | None) -> LeagueInfo | None:
        if not slug:
            return None
        return self.leagues.get(slug) or self.discovered.get(slug)

    def listed(self, slug: str | None) -> bool:
        return bool(slug) and slug in self.leagues

    def supported_slugs(self) -> list[str]:
        return [s for s, i in self.leagues.items() if i.supported]

    def tier_of(self, slug: str | None) -> str:
        i = self.leagues.get(slug or "")
        return i.tier if i is not None and i.tier in TIERS else self.default_tier

    def corner_baseline(self, slug: str | None) -> float:
        i = self.leagues.get(slug or "")
        return float(i.corner_baseline) if i is not None and i.corner_baseline else self.default_corner_baseline

    def goals_baseline(self, slug: str | None) -> float:
        i = self.leagues.get(slug or "")
        return float(i.goals_baseline) if i is not None and i.goals_baseline else self.default_goals_baseline

    def baseline_ratings(self, slug: str | None) -> PreMatchRatings:
        """League-average prior for a slug (goals/corners totals with the default home advantage)."""
        return PreMatchRatings(GOAL_SUPREMACY_DEFAULT, self.goals_baseline(slug), CORNER_SUPREMACY_DEFAULT,
                               self.corner_baseline(slug))

    def priority(self, slug: str | None) -> int:
        """Lower = processed first when the live-match cap binds (listed tier 1 < 2 < 3 < auto)."""
        if not self.listed(slug):
            return 9
        return TIERS.index(self.tier_of(slug))

    def note_discovered(self, slug: str, name: str, espn_id: str | None = None, header_slug: str | None = None) -> None:
        """Remember an auto-discovered league (named from the summary ``header.league``)."""
        if slug in self.leagues:
            return
        prev = self.discovered.get(slug)
        if prev is not None and prev.name == name:
            return
        self.discovered[slug] = LeagueInfo(slug, name, "", "TIER_3", espn_id, note=f"espn slug {header_slug or '?'}",
                                           discovered=True)
        log.info("league auto-discovered", extra={"slug": slug, "name": name, "espn_slug": header_slug, "tier": "TIER_3"})

    def frozen_dict(self) -> dict[str, Any]:
        """The registry part of the v4.3-global freeze (tiers + baselines of every configured slug)."""
        return {
            "defaults": {"tier": self.default_tier, "corner_baseline": self.default_corner_baseline,
                         "goals_baseline": self.default_goals_baseline},
            "auto_discovery": self.auto_discovery,
            "leagues": {s: {"tier": i.tier, "corner_baseline": i.corner_baseline, "goals_baseline": i.goals_baseline,
                            "supported": i.supported} for s, i in sorted(self.leagues.items())},
        }


def load_registry(path: str | Path) -> LeagueRegistry:
    """Load config/leagues.json (``{"defaults", "auto_discovery", "leagues": [{slug, name, tier, ...}]}``)."""
    p = Path(str(path))
    if not p.exists():
        return LeagueRegistry()
    raw = json.loads(p.read_text())
    d = raw.get("defaults") or {}
    names = {f for f in LeagueInfo.__dataclass_fields__}
    leagues = {}
    for x in raw.get("leagues") or []:
        if not isinstance(x, Mapping) or not x.get("slug"):
            continue
        kw = {k: v for k, v in x.items() if k in names}
        kw.setdefault("name", x["slug"])
        if kw.get("espn_id") is not None:
            kw["espn_id"] = str(kw["espn_id"])
        leagues[x["slug"]] = LeagueInfo(**kw)
    auto = raw.get("auto_discovery") or {}
    return LeagueRegistry(leagues, str(d.get("tier", "TIER_3")), float(d.get("corner_baseline", DEFAULT_CORNER_BASELINE)),
                          float(d.get("goals_baseline", DEFAULT_GOALS_BASELINE)), bool(auto.get("enabled", True)))


# ---------------------------------------------------------------------------
# Live Stats Integrity Gate
# ---------------------------------------------------------------------------
TELEMETRY_KEYS = ("wonCorners", "totalShots", "shotsOnTarget")


def telemetry_check(home_stats: Mapping[str, Any] | None, away_stats: Mapping[str, Any] | None, minute: float | None,
                    gate_minute: float = 30.0) -> tuple[bool, str]:
    """``(ok, reason)``. From ``gate_minute`` on, a live match must carry the stats keys (corners, shots, SOT for
    both teams) AND show activity (total shots > 0 or total corners > 0); otherwise ESPN is serving placeholder
    zeros / no telemetry and the match is skipped (``skipped: no_live_telemetry``). Before the gate minute
    the match passes (Engine B's own minute windows start at 30' anyway)."""
    if minute is None or minute < gate_minute:
        return True, "pre_gate"
    hs, as_ = home_stats or {}, away_stats or {}
    if not all(k in hs and k in as_ for k in TELEMETRY_KEYS):
        return False, "missing_keys"

    def v(d: Mapping[str, Any], k: str) -> float:
        try:
            x = float(d.get(k) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return x if math.isfinite(x) else 0.0

    shots = v(hs, "totalShots") + v(as_, "totalShots")
    corners = v(hs, "wonCorners") + v(as_, "wonCorners")
    if shots > 0 or corners > 0:
        return True, "ok"
    return False, "all_zero"


# ---------------------------------------------------------------------------
# Frozen global params
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class GlobalParams:
    telemetry_gate_minute: float = 30.0
    tier_kelly_mult: tuple[tuple[str, float], ...] = (("TIER_1", 1.0), ("TIER_2", 1.0), ("TIER_3", 0.5))
    tier_max_units: tuple[tuple[str, float], ...] = (("TIER_3", 15.0),)
    auto_discovery: bool = True
    espn_all_limit: int = 500
    max_live_events: int = 40
    summaries_per_cycle: int = 3
    team_table_slugs: tuple[str, ...] = ("eng.1",)  # team ratings only used for leagues they were fitted on
    lowconf_prior_alerts: bool = False  # league-average (no pickcenter) priors stay gated like league_default

    def to_dict(self, registry: LeagueRegistry | None = None) -> dict[str, Any]:
        d = asdict(self)
        d["tier_kelly_mult"] = dict(self.tier_kelly_mult)
        d["tier_max_units"] = dict(self.tier_max_units)
        d["team_table_slugs"] = list(self.team_table_slugs)
        if registry is not None:
            d["registry"] = registry.frozen_dict()
        return d


# ---------------------------------------------------------------------------
# Ratings by slug (pickcenter prior > league average, low confidence)
# ---------------------------------------------------------------------------
class GlobalRatingsResolver(RatingsResolver):
    """Slug-aware :class:`RatingsResolver` for v4.3-global.

    Priority: Pinnacle (if a paid feed is configured) > ESPN pickcenter (DraftKings, de-vigged with the
    existing ``devig``) > team table (only for ``team_table_slugs``, so "Liverpool" in Uruguay never maps to
    the EPL side) > league average for the slug (``league_default``, LOW confidence: alerts stay gated
    because ``allow_default_ratings`` is False in v4.2/v4.3). Corners always come from the team table (if
    eligible) or the slug corner baseline - never from league-name substrings.
    """

    def __init__(self, registry: LeagueRegistry, *args: Any, slug_lookup: Callable[[str], str | None] | None = None,
                 team_table_slugs: tuple[str, ...] = ("eng.1",), **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.registry = registry
        self.slug_lookup = slug_lookup
        self.team_table_slugs = tuple(team_table_slugs)
        self.slugs: dict[str, str] = {}
        self.prior_info: dict[str, dict[str, Any]] = {}
        # plumbing: event_key -> provenance of the pickcenter prior (pre / in-play fetch, minute, time, DK lines)
        self.prior_meta_lookup: Callable[[str], dict[str, Any] | None] | None = None

    def slug_for(self, event_key: str) -> str | None:
        s = self.slugs.get(event_key)
        if s is None and self.slug_lookup is not None:
            try:
                s = self.slug_lookup(event_key)
            except Exception:  # noqa: BLE001
                s = None
        return s

    def resolve(self, event_key: str, home: str, away: str, league: str = "", kickoff: datetime | None = None
                ) -> tuple[PreMatchRatings, str]:
        hit = self._cache.get(event_key)
        if hit and hit[1] == "pinnacle":
            return hit
        slug = self.slug_for(event_key)
        base = self.registry.baseline_ratings(slug)
        table = None
        if self.team_table is not None and slug in self.team_table_slugs:
            table = self.team_table.ratings(home, away, league)
        corners = table or base
        goals = table or base
        src = "football_data" if table else "league_default"
        sharp = self.sharp_lookup(home, away, kickoff) if self.sharp_lookup else None
        label = "pinnacle"
        if sharp is None and self.prior_lookup is not None:
            hitp = self.prior_lookup(event_key)
            if hitp is not None:
                sharp, label = hitp[0], hitp[1] or "market_prior"
        ph = pa = None
        if sharp is not None:
            try:
                ph, pd_, pa = devig([sharp.home, sharp.draw, sharp.away], self.devig_method)
                p_over = None
                if sharp.total_line is not None and sharp.over and sharp.under:
                    p_over = devig([sharp.over, sharp.under], self.devig_method)[0]
                goals = ratings_from_prices(ph, pd_, pa, p_over, sharp.total_line or 2.5, corners.corner_total)
                src = label
            except ValueError:
                ph = pa = None
        market = src not in ("league_default", "football_data")
        r = PreMatchRatings(goals.goal_supremacy, goals.goal_total, corners.corner_supremacy, corners.corner_total,
                            ph if market else None, pa if market else None)
        info = {"slug": slug, "lambda_goals_prior": round(float(r.goal_total), 4),
                "corner_prior": round(float(r.corner_total), 3), "source": src,
                "confidence": "market" if market else ("team_table" if src == "football_data" else "low")}
        if market and ph is not None and pa is not None:
            info["fav_prob"] = round(float(max(ph, pa)), 4)
            info["fav"] = "home" if ph >= pa else "away"
        if self.prior_meta_lookup is not None and sharp is not None and label != "pinnacle" and src == label:
            try:
                meta = self.prior_meta_lookup(event_key)
            except Exception:  # noqa: BLE001 - provenance is informational only
                meta = None
            if meta:
                info["fetch"] = {k: meta.get(k) for k in ("fetched_state", "fetched_minute", "fetched_at",
                                                          "kickoff", "snapshot")}
                info["fetch"]["true_pre_match"] = meta.get("fetched_state") == "pre"
                for which in ("first", "latest"):
                    if meta.get(which):
                        info["dk_" + which] = dict(meta[which])
        prev = self.prior_info.get(event_key)
        if prev is None or prev.get("source") != src:
            log.info("v4.3 prior", extra={"event": event_key, **info})
        self.prior_info[event_key] = info
        self._cache[event_key] = (r, src)
        return r, src


# ---------------------------------------------------------------------------
# Engine wrapper: per-match tier sizing + tags
# ---------------------------------------------------------------------------
class GlobalSignalEngine(FreeSignalEngine):
    """FreeSignalEngine with per-match liquidity tier sizing (v4.2 maths unchanged).

    Before each state the sizer is swapped for ``risk.stake_from_kelly(f, tier)``: TIER_3 stakes are
    0.5 x quarter-Kelly (= eighth-Kelly), capped at 15 units and rounded to 5/10 as usual. The 5% match
    stake cap / dedup sees the scaled stake, so it still applies.
    """

    def __init__(self, registry: LeagueRegistry, risk: Any, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.registry = registry
        self.risk = risk
        self.probation: Any = None  # optional quantbet.probation.ProbationBook -> "[PROBATION: n/150]" card tag

    def tier_for(self, state: MatchState) -> tuple[str | None, str]:
        slug = getattr(state, "league_slug", "") or None
        if slug is None and isinstance(self.resolver, GlobalRatingsResolver):
            slug = self.resolver.slug_for(state.event_key)
        return slug, self.registry.tier_of(slug)

    def on_match_state(self, state: MatchState, now: float | None = None,
                       real_odds: Mapping[tuple[Any, float, Any], float] | None = None) -> list[Signal]:
        slug, tier = self.tier_for(state)
        if isinstance(self.resolver, GlobalRatingsResolver) and slug:
            self.resolver.slugs[state.event_key] = slug
        risk = self.risk
        self.sizer = lambda f, o, _t=tier: risk.stake_from_kelly(f, _t)
        sigs = super().on_match_state(state, now, real_odds)
        prior = self.resolver.prior_info.get(state.event_key) if isinstance(self.resolver, GlobalRatingsResolver) else None
        for s in sigs:
            s.extra["liquidity_tier"] = tier
            s.extra["league_slug"] = slug
            if prior:
                s.extra["prior"] = dict(prior)
            if self.probation is not None:
                try:
                    tag = self.probation.tag(slug, tier)
                except Exception:  # noqa: BLE001 - presentation only; never block an alert
                    log.exception("probation lookup failed")
                    tag = ""
                if tag:
                    s.extra["probation"] = tag
        return sigs


def liquidity_tag(tier: str | None) -> str:
    """Card tag, e.g. ``[LIQUIDITY: TIER 3 - SMALL LEAGUE]``."""
    return f"[LIQUIDITY: {TIER_LABELS.get(tier or '', TIER_LABELS['TIER_3'])}]"


__all__ = ["TIERS", "TIER_LABELS", "USER_CORNER_BASELINES", "DEFAULT_CORNER_BASELINE", "DEFAULT_GOALS_BASELINE",
           "AUTO_PREFIX", "SKIP_NO_TELEMETRY", "LeagueInfo", "LeagueRegistry", "load_registry", "telemetry_check",
           "GlobalParams", "GlobalRatingsResolver", "GlobalSignalEngine", "liquidity_tag"]
