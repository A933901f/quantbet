"""v4.4-syndicate: selective tiered hurdles, sustained momentum, Unders, cards, Bet Builder combos.

Everything here is read-only maths on the free ESPN / FotMob / Football-Data inputs that the
v4.3-global pipeline already fetches through the polite HTTP layer. Nothing in this module
talks to the network or to any bookmaker; Bet365 prices stay *reverse-priced* (fair odds and
the MINIMUM BET365 ODDS are shown, the user checks the app by hand).

Layers (all parameters frozen under ``global.syndicate`` of the ``v4.4-syndicate`` freeze):

1. **Tier EV hurdles** - target edge per liquidity tier (T1 4%, T2 5.5%, T3 7%); the abort floor
   is scaled the same way (``abort = abort_ratio x hurdle``, 0.6 like v4.3's 1.5%/2.5%).
   An *achievability* gate requires the model lift over the prior to cover the hurdle plus an
   ASSUMED Bet365 per-side margin: ``prior_fair / model_fair >= (1 + h) / (1 - m)``.
2. **Sustained momentum** (replaces the one-poll ``activity_high`` label): rolling 12-minute
   shots / shots-on-target / corners (+ dangerous attacks when an enricher provides them) index
   vs. league-neutral per-10-minute baselines, required over >= 3 consecutive polls spanning
   >= 4 match minutes, weighted by game state (trailing favourite / level knockout after 55'
   boosted, a 2+ goal lead penalised).
3. **Unders** - ``tempo_stall`` (high pre-match lambda, dead pace on every reading from 25' to 34'-45') and ``bus_parking``
   (one-goal leader after 55' with a tiny share of the pressure). Half lines only. Bullish states
   (sustained momentum, trailing favourite, level knockout after 55') suppress Unders; bearish
   states (stall / bus parking) suppress Overs.
4. **Cards** - :func:`cards_model`: live Poisson remaining-cards lambda from a Football-Data
   baseline (cards/90, fouls/90 per division), the live foul rate and booking pace, and a late
   urgency multiplier when |goal diff| <= 1 in 60'-85'. Yellow = 1, red = 1. No foul/card
   telemetry -> no cards alert.
5. **Dedup** - per match and family: 15 match-minute cooldown, re-alert only if the line moved
   >= 0.5 or the fair probability of the alerted line shifted >= 4pp; 1 signal per family at a
   time (opposite directions never), max 3 per match (goals + corners + cards); the 5% match
   stake cap counts every family including cards.
6. **Boost target + Bet Builder** - every card carries a ``[BOOST TARGET]`` line; same-match
   combos of 2-3 legs use ``P(A and B) = PA PB + rho sqrt(PA(1-PA) PB(1-PB))`` clipped to the
   Frechet bounds, and a Gaussian copula with a fixed seed for 3 legs (pairwise margins matched
   exactly, result clipped to the 3-way Frechet bounds). rho comes from
   ``config/syndicate_priors.json`` (PRIORS estimated from Football-Data full-time data).

v4.5-sharp (:class:`SharpParams`, frozen under ``global.syndicate`` of the ``v4.5-sharp`` entry;
the ``v4.4-syndicate`` behaviour and hash are untouched because SharpParams is a *subclass* and every
new behaviour is a method that the base class answers with the v4.4 value):

* no achievability gate; lift caps per family (goals 28%, corners 35%); Over nets 1.06 / 1.08;
  tier hurdles 3.5% / 4.5% / 6.0% (abort 0.6x); minute windows 30'-43' and 55'-85';
* league hygiene: only leagues registered in ``config/leagues.json`` may alert
  (``unmapped_league_blocked`` for ``espn.<id>`` auto-discovered leagues) and youth / reserve team
  names are blocked (``youth_reserve_blocked``; tokens u21/u23/u19/u20/u18/u17/reserves/academy/jong as
  whole words, suffixes `` b`` / `` ii`` / `` iii``, ``Juan Pablo II`` excepted - see :mod:`quantbet.youth`);
  TIER_3 = Goals Over/Under only;
* momentum: sustained index 1.35 over 3 polls, ``momentum_flurry_cap`` above 2.90, no extra
  trailing-favourite weight (the model already applies the chasing multipliers);
* Goals/Corners Overs also need live activity: ``momentum_mult_total >= activity_trigger_mult`` (1.04)
  or a trailing favourite, else ``no_activity_confirm``;
* Unders: suppressed when ``|attack_momentum| >= 0.30`` or ``momentum_mult_total >= 1.02``
  (``MomentumView.activity_bullish``); ``bus_parking`` needs 65'+, |gd| == 1, leader share <= 0.25 and
  adj index <= 0.75; the second-half ``tempo_stall`` (55'-75', index <= 0.65, |gd| <= 1) is kept but
  OFF (``stall2_enabled=False``);
* a cross-family direction lock: once a match has an Over (or Under) alert in any family, the other
  direction in any family is dropped as ``match_direction_conflict`` (paper cards: see
  :meth:`SyndicateDeduplicator.check`);
* Total Cards under the Bet365 1/2 rule (yellow 1, red 2, second-yellow dismissal 3 per player)
  priced with a Negative Binomial (pooled Football-Data over-dispersion), PAPER ONLY: logged and
  graded, tagged on Telegram, never counted against the 3-alert / 5% match caps, never a combo leg;
* Bet Builder combos only when another family independently passes its directional net gate.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import re
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
from scipy.special import ndtr, ndtri, owens_t
from scipy.stats import nbinom, poisson

from .engine import (
    AlertDeduplicator,
    ConfidenceReport,
    LinePrice,
    LiveModelParams,
    PreMatchRatings,
    PricingConfig,
    SettlementProbs,
    StakeSizer,
    _ceil2,
    asian_settlement,
    assess_confidence,
    build_stake_tiers,
    expected_log_growth,
    favourite_chasing,
    price_live_lines,
)
from .global_layer import GlobalRatingsResolver, GlobalSignalEngine
from .models import EventInfo, MarketType, MatchState, Selection, Signal
from .youth import youth_reserve_hit  # re-exported (v4.5 public name)

log = logging.getLogger(__name__)

SYNDICATE_VERSION = "v4.4-syndicate"
SHARP_VERSION = "v4.5-sharp"
FAMILIES = ("goals", "corners", "cards")
DEFAULT_PRIORS_PATH = Path("config/syndicate_priors.json")


def is_syndicate(rules: str) -> bool:
    """True for the rule sets that run :class:`SyndicateSignalEngine` (v4.4-syndicate, v4.5-sharp)."""
    r = rules.lower()
    return r.startswith("v4.4") or r.startswith("v4.5")


def is_sharp(rules: str) -> bool:
    """True for v4.5-sharp (:class:`SharpParams`)."""
    return rules.lower().startswith("v4.5")


def syndicate_family(market: MarketType) -> str:
    """Correlation / dedup family: ``goals``, ``corners`` or ``cards``."""
    if market is MarketType.TOTAL_CORNERS:
        return "corners"
    if market is MarketType.TOTAL_CARDS:
        return "cards"
    return "goals"


def is_half_line(line: float) -> bool:
    """``x.5`` lines only (no push, no half-settlement)."""
    return abs((line * 2.0) % 2.0 - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# Frozen parameters
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SyndicateParams:
    """All v4.4 knobs. Hashed (with the used part of the priors file) into the v4.4 freeze."""

    # 1. tier hurdles (EV at the MINIMUM BET365 ODDS) and the abort floor scaled the same way
    tier_target_edge: tuple[tuple[str, float], ...] = (("TIER_1", 0.040), ("TIER_2", 0.055), ("TIER_3", 0.070))
    abort_ratio: float = 0.6  # abort edge = 0.6 x hurdle (v4.3: 1.5% / 2.5%)
    # ASSUMPTION (not measured: we never read Bet365): per-side in-play margin by liquidity tier
    tier_side_margin: tuple[tuple[str, float], ...] = (("TIER_1", 0.030), ("TIER_2", 0.0375), ("TIER_3", 0.045))
    achievability_gate: bool = True
    # goals/corners over-reaction guard: prior_fair / model_fair - 1 above this is treated as model error,
    # not edge (cards are bounded by cards_factor_clip and the cards net thresholds instead)
    # (27-Oct audit: lift 0-0.15 -> +3.77u flat on 12, 0.15-0.30 -> -5.18u on 18, >=0.30 -> -0.41u on 9;
    # nothing above 0.62 was ever alerted)
    max_lift: float = 0.45
    over_min_net: float = 1.10  # model/prior remaining-lambda multiplier for Overs (audit median net was 1.098)
    over_suppress_net: float = 1.05
    under_max_net: float = 0.97  # Unders need the model lambda clearly below the prior
    # 2. sustained momentum
    momentum_window_min: float = 12.0
    momentum_min_span_min: float = 9.0
    momentum_polls: int = 3
    momentum_persist_min: float = 4.0
    w_shots: float = 1.0
    w_sot: float = 2.0
    w_corners: float = 1.5
    w_dangerous_attacks: float = 0.15
    # league-neutral per-team per-10-minute baselines (Football-Data pooled: ~25 shots, ~8.6 SOT,
    # ~10 corners per match; dangerous attacks ~50/team/90 is an ASSUMPTION - ESPN has none)
    base10_shots: float = 1.39
    base10_sot: float = 0.48
    base10_corners: float = 0.56
    base10_dangerous_attacks: float = 5.5
    sustained_over_index: float = 1.70  # ~p90 of all in-window poll readings of the 27-Oct run (p90 = 1.72)
    w_fav_trailing: float = 1.15
    w_knockout_level: float = 1.10
    knockout_level_minute: float = 55.0
    w_lead_two: float = 0.80
    knockout_words: tuple[str, ...] = ("cup", "pokal", "copa", "beker", "trophy", "coppa", "coupe", "taca",
                                       "taça", "shield", "playoff", "play-off", "knockout")
    gap_reset_s: float = 180.0
    # 3. unders
    unders_enabled: bool = True
    unders_half_lines_only: bool = True
    stall_index: float = 0.60  # ~p25 of in-window readings (0.59), required on EVERY reading since 25'
    stall_min_lambda0: float = 2.60
    stall_from_minute: float = 25.0
    stall_until_minute: float = 45.0  # tempo_stall is a first-half pattern (dead pace 25' -> 35'+)
    bus_from_minute: float = 55.0
    bus_leader_share_max: float = 0.35
    bus_index_max: float = 1.00
    # 4. cards
    cards_enabled: bool = True
    cards_line_span: float = 5.0
    cards_stoppage_h1: float = 2.0
    cards_stoppage_h2: float = 4.0
    cards_foul_pseudo: float = 8.0
    cards_book_pseudo: float = 1.5
    cards_foul_weight: float = 0.5
    cards_book_weight: float = 0.5
    cards_factor_clip: tuple[float, float] = (0.70, 1.50)
    cards_urgency_window: tuple[float, float] = (60.0, 85.0)
    cards_urgency_max_gd: int = 1
    cards_urgency_mult: float = 1.11  # ESPN 27-Oct finals: 60'-85' |gd|<=1 rate / all 60'-85' rate = 1.114 (n=84)
    cards_net_over: float = 1.10
    cards_net_under: float = 0.90
    cards_default_cards90: float = 4.3  # used only when the priors file has no pooled value
    cards_default_fouls90: float = 24.0
    cards_default_h2_share: float = 0.68
    # 5. dedup
    cooldown_min: float = 15.0
    realert_line_move: float = 0.5
    realert_p_shift: float = 0.04
    max_per_family: int = 2  # first alert + at most one material re-alert
    max_per_match: int = 3  # 1 goals + 1 corners + 1 cards
    max_match_stake_frac: float = 0.05
    # 6. boost / bet builder
    combo_enabled: bool = True
    combo_max_legs: int = 3
    combo_leg_fair_range: tuple[float, float] = (1.40, 2.80)
    combo_mc_draws: int = 200_000
    combo_seed: int = 20261028
    boost_variant_enabled: bool = True
    boost_pct: float = 0.25

    def hurdle(self, tier: str | None) -> float:
        return dict(self.tier_target_edge).get(tier or "TIER_3", dict(self.tier_target_edge)["TIER_3"])

    def abort_edge(self, tier: str | None) -> float:
        return round(self.hurdle(tier) * self.abort_ratio, 6)

    def margin(self, tier: str | None) -> float:
        return dict(self.tier_side_margin).get(tier or "TIER_3", dict(self.tier_side_margin)["TIER_3"])

    def lift_required(self, tier: str | None) -> float:
        """``(1 + h) / (1 - m)``: model lift over the prior needed so a margin-carrying price can meet h."""
        return (1.0 + self.hurdle(tier)) / (1.0 - self.margin(tier))

    # -- behaviour hooks: the v4.4 answers here, :class:`SharpParams` overrides them (methods are
    # -- not dataclass fields, so they are not part of ``to_dict`` and the v4.4 hash is unchanged)
    def version(self) -> str:
        return SYNDICATE_VERSION

    def lift_cap(self, family: str) -> float:
        """Over-reaction guard ``prior_fair / model_fair - 1`` for goals / corners."""
        return self.max_lift

    def over_net_min(self, family: str) -> float:
        """Minimum model/prior remaining-lambda multiplier for an Over of ``family``."""
        return self.over_min_net

    def tier_allows(self, tier: str | None, family: str) -> bool:
        """v4.4: every family in every tier."""
        return True

    def hygiene_block(self, slug: str | None, listed: bool, home: str, away: str) -> str | None:
        """v4.4: no league / team-name hygiene (the v4.3 registry and telemetry gate only)."""
        return None

    def card_rule(self) -> str:
        """``y1_r1`` (v4.4: yellow = 1, red = 1) or ``bet365`` (yellow 1, red 2, 2nd-yellow dismissal 3)."""
        return "y1_r1"

    def flurry_cap(self) -> float | None:
        return None

    def cards_paper(self) -> bool:
        return False

    def combo_gated(self) -> bool:
        return False

    def second_half_stall(self) -> bool:
        return False

    def activity_confirm_overs(self) -> bool:
        """v4.5 patch: Goals/Corners Overs also need ``momentum_mult_total >= activity_trigger_mult`` or
        ``fav_trailing`` (v4.4: sustained momentum alone)."""
        return False

    def direction_lock(self) -> bool:
        """v4.5 patch: never Over and Under in the same match, across families (v4.4: per family only)."""
        return False

    def under_activity_suppress(self) -> tuple[float, float] | None:
        """v4.5 patch: ``(|attack_momentum| threshold, momentum_mult_total threshold)`` at or above which the
        momentum view is bullish (Unders suppressed). v4.4: ``None`` (sustained/fav_trailing/knockout only)."""
        return None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("tier_target_edge", "tier_side_margin"):
            d[k] = dict(getattr(self, k))
        for k, v in list(d.items()):
            if isinstance(v, tuple):
                d[k] = list(v)
        return d


@dataclass(frozen=True, slots=True)
class SharpParams(SyndicateParams):
    """v4.5-sharp: every v4.4 knob (overridden defaults below) + the v4.5 fields.

    Spec name -> field mapping (documented in CHANGES.md): ``max_lift_goals`` / ``max_lift_corners``
    replace the single v4.4 ``max_lift`` (kept as the max of the two for the record, not used);
    ``over_min_net`` 1.06 Goals / 1.08 Corners -> ``over_min_net_goals`` / ``over_min_net_corners``
    (``over_min_net`` keeps the goals value); ``momentum_polls`` and ``sustained_over_index`` are the
    v4.4 names; ``minute_windows`` lives in the v4.5-sharp PricingConfig preset (engine.py), and
    ``corner_prior_minutes`` / ``corner_dispersion`` in its LiveModelParams preset.
    """

    # 1. hurdles / gates (overridden v4.4 defaults)
    tier_target_edge: tuple[tuple[str, float], ...] = (("TIER_1", 0.035), ("TIER_2", 0.045), ("TIER_3", 0.060))
    abort_ratio: float = 0.60
    achievability_gate: bool = False
    max_lift: float = 0.35  # not used by v4.5 (= max(max_lift_goals, max_lift_corners), see lift_cap)
    over_min_net: float = 1.06  # = over_min_net_goals (legacy field name)
    over_suppress_net: float = 1.05
    max_lift_goals: float = 0.28
    max_lift_corners: float = 0.35
    over_min_net_goals: float = 1.06
    over_min_net_corners: float = 1.08
    # 2. league hygiene
    block_unmapped_leagues: bool = True  # only slugs registered in config/leagues.json may alert
    unmapped_prefix: str = "espn."
    youth_tokens: tuple[str, ...] = ("u21", "u23", "u19", "u20", "u18", "u17", "reserves", "academy", "jong")
    youth_suffix_tokens: tuple[str, ...] = (" b", " ii", " iii")
    youth_name_exceptions: tuple[str, ...] = ("juan pablo ii",)  # senior Peruvian Liga 1 club
    allow_tier3_corners: bool = False
    allow_tier3_cards: bool = False
    # 3. momentum
    sustained_over_index: float = 1.35
    momentum_polls: int = 3
    max_momentum_index: float = 2.90  # adj_index above this = a flurry, not sustained pressure (Overs blocked)
    w_fav_trailing: float = 1.0  # the model already applies fav_trailing_goal_mult 1.25 / corner_mult 1.40
    # Goals/Corners Overs: sustained momentum AND (momentum_mult_total >= PricingConfig.activity_trigger_mult
    # (1.04, the v4.3 activity_high trigger) OR fav_trailing); failures traced "no_activity_confirm"
    require_activity_confirm: bool = True
    # Unders: the momentum view is bullish (Unders suppressed) when |attack_momentum| >= 0.30 or
    # momentum_mult_total >= 1.02 on the current poll
    under_suppress_attack_momentum: float = 0.30
    under_suppress_mult_total: float = 1.02
    # never Over and Under in the same match (all families; see SyndicateDeduplicator.check)
    match_direction_lock: bool = True
    # bus_parking (Goals Under): 65'+, |gd| == 1, leader's pressure share <= 0.25, adj_index <= 0.75
    bus_from_minute: float = 65.0
    bus_leader_share_max: float = 0.25
    bus_index_max: float = 0.75
    stall2_enabled: bool = False  # second-half tempo_stall off by default (fields kept, opt-in)
    stall2_from_minute: float = 55.0
    stall2_until_minute: float = 75.0
    stall2_index: float = 0.65
    stall2_lookback_min: float = 12.0
    stall2_max_abs_gd: int = 1
    # 4. cards (Bet365 Total Cards: yellow 1, red 2, max 3 per player)
    cards_count: str = "bet365"
    cards_distribution: str = "negbin"  # pooled within-division r from config/syndicate_priors.json cards_bet365
    cards_paper_only: bool = True  # until 50 live games with foul telemetry are graded
    cards_default_cards90: float = 4.6  # used only when the priors file has no cards_bet365 block
    # 5. combos
    combo_require_gated_families: bool = True

    def version(self) -> str:
        return SHARP_VERSION

    def lift_cap(self, family: str) -> float:
        return self.max_lift_corners if family == "corners" else self.max_lift_goals

    def over_net_min(self, family: str) -> float:
        return self.over_min_net_corners if family == "corners" else self.over_min_net_goals

    def tier_allows(self, tier: str | None, family: str) -> bool:
        if (tier or "TIER_3") != "TIER_3":
            return True
        if family == "corners":
            return self.allow_tier3_corners
        if family == "cards":
            return self.allow_tier3_cards
        return True

    def hygiene_block(self, slug: str | None, listed: bool, home: str, away: str) -> str | None:
        if self.block_unmapped_leagues and (not slug or slug.startswith(self.unmapped_prefix) or not listed):
            return "unmapped_league_blocked"
        for nm in (home, away):
            hit = youth_reserve_hit(nm, self.youth_tokens, self.youth_suffix_tokens, self.youth_name_exceptions)
            if hit:
                return f"youth_reserve_blocked ({hit.strip()})"
        return None

    def card_rule(self) -> str:
        return self.cards_count

    def flurry_cap(self) -> float | None:
        return self.max_momentum_index

    def cards_paper(self) -> bool:
        return self.cards_paper_only

    def combo_gated(self) -> bool:
        return self.combo_require_gated_families

    def second_half_stall(self) -> bool:
        return self.stall2_enabled

    def activity_confirm_overs(self) -> bool:
        return self.require_activity_confirm

    def direction_lock(self) -> bool:
        return self.match_direction_lock

    def under_activity_suppress(self) -> tuple[float, float] | None:
        return (self.under_suppress_attack_momentum, self.under_suppress_mult_total)


def load_priors(path: str | Path | None = None) -> dict[str, Any]:
    """``config/syndicate_priors.json`` or ``{}`` (callers fall back to the documented defaults)."""
    p = Path(path) if path else DEFAULT_PRIORS_PATH
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def priors_frozen_view(priors: Mapping[str, Any]) -> dict[str, Any]:
    """Only the values the engine uses (no timestamps), so regenerating identical numbers keeps the hash."""
    prof = priors.get("cards_time_profile") or {}
    return {
        "rho": {k: float(v) for k, v in sorted((priors.get("rho") or {}).items())},
        "cards_pooled": {k: float((priors.get("cards_pooled") or {}).get(k, 0.0)) for k in ("cards90", "fouls90")},
        "cards_baselines": {s: {"cards90": float(v["cards90"]), "fouls90": float(v["fouls90"])}
                            for s, v in sorted((priors.get("cards_baselines") or {}).items())},
        "second_half_share": float(prof["second_half_share"]) if prof.get("second_half_share") else None,
    }


def sharp_priors_frozen_view(priors: Mapping[str, Any]) -> dict[str, Any]:
    """v4.5-sharp: rho + the Bet365-rule (yellow 1, red 2) card baselines and the NegBin r."""
    prof = priors.get("cards_time_profile") or {}
    b = priors.get("cards_bet365") or {}
    pooled = b.get("pooled") or {}
    return {
        "rho": {k: float(v) for k, v in sorted((priors.get("rho") or {}).items())},
        "cards_bet365_pooled": {k: float(pooled.get(k, 0.0)) for k in ("cards90", "cards_var", "fouls90")},
        "cards_bet365_nb_r": float(b["nb_r"]) if b.get("nb_r") else None,
        "cards_bet365_baselines": {s: {"cards90": float(v["cards90"]), "fouls90": float(v["fouls90"])}
                                   for s, v in sorted((b.get("baselines") or {}).items())},
        "second_half_share": float(prof["second_half_share"]) if prof.get("second_half_share") else None,
    }


def syndicate_params_dict(params: SyndicateParams, priors: Mapping[str, Any]) -> dict[str, Any]:
    """The ``global.syndicate`` block hashed into the v4.4-syndicate / v4.5-sharp freeze."""
    if isinstance(params, SharpParams):
        return {"params": params.to_dict(), "priors": sharp_priors_frozen_view(priors)}
    return {"params": params.to_dict(), "priors": priors_frozen_view(priors)}


# ---------------------------------------------------------------------------
# Probability helpers (Unders = lower tail)
# ---------------------------------------------------------------------------
def poisson_total_settlement(current: int, lam: float, line: float, selection: Selection,
                             k_max: int | None = None) -> SettlementProbs:
    """Settlement of Over/Under ``line`` when the final total = ``current + Poisson(lam)``.

    Under uses the *lower tail* of the same distribution, so on half lines
    ``P(Under) = 1 - P(Over)`` exactly.
    """
    lam = max(float(lam), 1e-9)
    k_max = k_max or int(max(30, lam + 12 * math.sqrt(lam) + 10))
    ks = np.arange(0, k_max + 1)
    pmf = poisson.pmf(ks, lam)
    pmf = pmf / pmf.sum()
    values = current + ks
    if selection is Selection.OVER:
        return asian_settlement(values, pmf, +1, -line)
    return asian_settlement(values, pmf, -1, +line)


def negbin_total_settlement(current: int, mu: float, r: float | None, line: float, selection: Selection,
                            k_max: int | None = None) -> SettlementProbs:
    """Settlement when the final total = ``current + NegBin(mean mu, shape r)``.

    ``Var = mu + mu^2 / r`` (over-dispersed); ``r`` = None / inf / > 1e6 -> Poisson (the limit r -> inf).
    Unders are the lower tail of the same pmf (``P(Under) = 1 - P(Over)`` on half lines).
    """
    if r is None or not math.isfinite(float(r)) or float(r) > 1e6:
        return poisson_total_settlement(current, mu, line, selection, k_max)
    mu = max(float(mu), 1e-9)
    r = float(r)
    sd = math.sqrt(mu + mu * mu / r)
    k_max = k_max or int(max(30, mu + 14 * sd + 10))
    ks = np.arange(0, k_max + 1)
    pmf = nbinom.pmf(ks, r, r / (r + mu))
    pmf = pmf / pmf.sum()
    values = current + ks
    if selection is Selection.OVER:
        return asian_settlement(values, pmf, +1, -line)
    return asian_settlement(values, pmf, -1, +line)


def price_from_settlement(market: MarketType, selection: Selection, line: float, sp: SettlementProbs,
                          cfg: PricingConfig, sizer: StakeSizer | None) -> LinePrice | None:
    """Same as :func:`quantbet.engine.price_line` for an externally computed settlement."""
    if sp.win_weight <= 1e-9 or sp.loss_weight <= 1e-9:
        return None
    fair = sp.fair_odds()
    lp = LinePrice(market, selection, line, sp, 1.0 / fair, fair, _ceil2(sp.min_acceptable_odds(cfg.target_edge)),
                   _ceil2(sp.min_acceptable_odds(cfg.abort_edge)), cfg.target_edge, cfg.abort_edge)
    lp.tiers = build_stake_tiers(sp, lp.min_odds, cfg, sizer)
    return lp


def boost_required_odds(o_min: float, boost_pct: float = 0.25) -> float:
    """Pre-boost price that a ``boost_pct`` *winnings* boost lifts to ``o_min``: ``1 + (o_min - 1)/(1 + b)``."""
    return 1.0 + (float(o_min) - 1.0) / (1.0 + float(boost_pct))


# ---------------------------------------------------------------------------
# Sustained momentum
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class MomentumReading:
    minute: float
    assessable: bool
    span: float = 0.0
    index: float = 1.0  # raw both-team pressure vs baseline
    home_index: float = 1.0
    away_index: float = 1.0
    leader_share: float | None = None  # leader's share of the pressure numerator (None when level)
    state_weight: float = 1.0
    adj_index: float = 1.0
    inputs: tuple[str, ...] = ()


@dataclass(slots=True)
class MomentumView:
    """What the gates see for one poll."""

    reading: MomentumReading
    sustained_over: bool = False
    tempo_stall: bool = False
    bus_parking: bool = False
    fav_trailing: bool = False
    knockout_level: bool = False
    polls: int = 0
    weights: tuple[str, ...] = ()
    activity_bullish: bool = False  # v4.5 patch: |attack_momentum| / momentum_mult_total over the Under thresholds

    @property
    def bullish(self) -> bool:
        return self.sustained_over or self.fav_trailing or self.knockout_level or self.activity_bullish

    @property
    def bearish(self) -> bool:
        return self.tempo_stall or self.bus_parking

    def labels(self) -> list[str]:
        out = []
        if self.sustained_over:
            out.append("sustained_momentum")
        if self.tempo_stall:
            out.append("tempo_stall")
        if self.bus_parking:
            out.append("bus_parking")
        if self.fav_trailing:
            out.append("fav_trailing")
        if self.knockout_level:
            out.append("knockout_level")
        return out

    def to_dict(self) -> dict[str, Any]:
        r = self.reading
        return {"index": round(r.index, 3), "adj_index": round(r.adj_index, 3), "state_weight": round(r.state_weight, 3),
                "span": round(r.span, 1), "polls": self.polls, "inputs": list(r.inputs),
                "home_index": round(r.home_index, 3), "away_index": round(r.away_index, 3),
                "leader_share": None if r.leader_share is None else round(r.leader_share, 3),
                "labels": self.labels(), "weights": list(self.weights), "activity_bullish": self.activity_bullish}


def is_knockout(league: str, slug: str, words: Sequence[str]) -> bool:
    name = f"{league or ''} {slug or ''}".lower()
    return any(w in name for w in words)


class SustainedMomentumTracker:
    """Per-match rolling pressure index that persists across polls (one instance per engine).

    Inputs are cumulative counters from the state (shots, shots on target, corners) plus the
    rolling 10-minute dangerous-attack counts when an enricher provides them (ESPN does not).
    A wall-clock gap longer than ``gap_reset_s`` (suspend/resume) clears the match history so a
    burst of accumulated deltas is never read as momentum.
    """

    def __init__(self, params: SyndicateParams) -> None:
        self.p = params
        self.samples: dict[str, deque[tuple[float, float, dict[str, float | None]]]] = {}
        self.readings: dict[str, deque[MomentumReading]] = {}
        self.first_assessable: dict[str, float] = {}

    @staticmethod
    def _counters(state: MatchState) -> dict[str, float | None]:
        g = lambda k: getattr(state, k, None)  # noqa: E731
        return {"hs": g("home_shots"), "as": g("away_shots"), "hsot": g("home_shots_on_target"),
                "asot": g("away_shots_on_target"), "hc": float(state.home_corners), "ac": float(state.away_corners),
                "hda": g("home_dangerous_attacks_10"), "ada": g("away_dangerous_attacks_10")}

    def reset(self, key: str) -> None:
        self.samples.pop(key, None)
        self.readings.pop(key, None)
        self.first_assessable.pop(key, None)

    def update(self, state: MatchState, ratings: PreMatchRatings | None, live_params: LiveModelParams | None,
               now: float | None = None) -> MomentumView:
        p = self.p
        now = time.time() if now is None else now
        key = state.event_key
        dq = self.samples.setdefault(key, deque())
        if dq and now - dq[-1][1] > p.gap_reset_s:
            self.reset(key)
            dq = self.samples.setdefault(key, deque())
        m = float(state.minute)
        if dq and m < dq[-1][0] - 1e-9:  # clock went backwards (feed correction): restart
            self.reset(key)
            dq = self.samples.setdefault(key, deque())
        dq.append((m, now, self._counters(state)))
        while dq and m - dq[0][0] > p.momentum_window_min + 1e-9:
            dq.popleft()
        reading = self._reading(state, dq)
        fav_trailing = knockout_level = False
        weights: list[str] = []
        w = 1.0
        gd = state.home_goals - state.away_goals
        if ratings is not None and live_params is not None:
            fc = favourite_chasing(state, ratings, live_params)
            if fc is not None and fc[1] < 0:
                fav_trailing = True
                w *= p.w_fav_trailing
                weights.append(f"fav_trailing x{p.w_fav_trailing:g}")
        if gd == 0 and m >= p.knockout_level_minute and is_knockout(getattr(state, "league", ""),
                                                                    getattr(state, "league_slug", ""), p.knockout_words):
            knockout_level = True
            w *= p.w_knockout_level
            weights.append(f"knockout_level x{p.w_knockout_level:g}")
        if abs(gd) >= 2:
            w *= p.w_lead_two
            weights.append(f"lead_2plus x{p.w_lead_two:g}")
        reading.state_weight = w
        reading.adj_index = reading.index * w
        hist = self.readings.setdefault(key, deque(maxlen=240))
        hist.append(reading)
        if reading.assessable and key not in self.first_assessable:
            self.first_assessable[key] = m
        view = MomentumView(reading, fav_trailing=fav_trailing, knockout_level=knockout_level, weights=tuple(weights))
        thr = p.under_activity_suppress()
        if thr is not None:  # v4.5 patch: live attack momentum / activity contradicts an Under
            am = getattr(state, "attack_momentum", None)
            mt = getattr(state, "momentum_mult_total", None)
            view.activity_bullish = abs(am or 0.0) >= thr[0] - 1e-12 or (mt if mt is not None else 1.0) >= thr[1] - 1e-12
        recent = [r for r in hist if r.minute >= m - p.momentum_persist_min - 1e-9]
        long_enough = key in self.first_assessable and m - self.first_assessable[key] >= p.momentum_persist_min - 1e-9
        ok = len(recent) >= p.momentum_polls and long_enough and all(r.assessable for r in recent)
        view.polls = len(recent)
        if ok:
            view.sustained_over = all(r.adj_index >= p.sustained_over_index for r in recent)
            lam0 = (ratings.goal_total if ratings is not None else 0.0)
            since = [r for r in hist if r.minute >= p.stall_from_minute - 1e-9]
            view.tempo_stall = (p.stall_from_minute + p.momentum_min_span_min - 1e-9 <= m <= p.stall_until_minute + 1e-9
                                and lam0 >= p.stall_min_lambda0 and len(since) >= p.momentum_polls
                                and all(r.assessable and r.adj_index <= p.stall_index for r in since))
            view.bus_parking = (m >= p.bus_from_minute and abs(gd) == 1
                                and all(r.leader_share is not None and r.leader_share <= p.bus_leader_share_max
                                        and r.adj_index <= p.bus_index_max for r in recent))
            if not view.tempo_stall and p.second_half_stall():
                # v4.5: second-half tempo_stall - 55'-75', |gd| <= 1, the rolling 12-minute index (the
                # reading itself covers the last <= 12 minutes, >= 9 of them) <= 0.65 on every poll of the
                # persistence window. No pre-match lambda floor (not in the spec).
                lb = [r for r in hist if r.minute >= m - p.stall2_lookback_min - 1e-9]  # type: ignore[attr-defined]
                view.tempo_stall = (p.stall2_from_minute - 1e-9 <= m <= p.stall2_until_minute + 1e-9  # type: ignore[attr-defined]
                                    and abs(gd) <= p.stall2_max_abs_gd  # type: ignore[attr-defined]
                                    and all(r.assessable and r.adj_index <= p.stall2_index for r in recent)  # type: ignore[attr-defined]
                                    and all(r.adj_index <= p.stall2_index for r in lb if r.assessable))  # type: ignore[attr-defined]
        return view

    def _reading(self, state: MatchState, dq: deque) -> MomentumReading:  # type: ignore[type-arg]
        p = self.p
        m_now, _, c_now = dq[-1]
        m_old, _, c_old = dq[0]
        span = m_now - m_old
        r = MomentumReading(minute=m_now, assessable=False, span=span)
        if span < p.momentum_min_span_min - 1e-9:
            return r
        comps = [("shots", "hs", "as", p.w_shots, p.base10_shots), ("sot", "hsot", "asot", p.w_sot, p.base10_sot),
                 ("corners", "hc", "ac", p.w_corners, p.base10_corners)]
        num_h = num_a = den = 0.0
        used: list[str] = []
        scale = span / 10.0
        for name, kh, ka, w, base in comps:
            vals = (c_now.get(kh), c_now.get(ka), c_old.get(kh), c_old.get(ka))
            if any(v is None for v in vals):
                continue
            dh = max(0.0, float(vals[0]) - float(vals[2]))  # type: ignore[arg-type]
            da = max(0.0, float(vals[1]) - float(vals[3]))  # type: ignore[arg-type]
            num_h += w * dh
            num_a += w * da
            den += w * base * scale
            used.append(name)
        hda, ada = c_now.get("hda"), c_now.get("ada")
        if hda is not None and ada is not None:  # already a rolling 10-minute count
            num_h += p.w_dangerous_attacks * float(hda) * scale
            num_a += p.w_dangerous_attacks * float(ada) * scale
            den += p.w_dangerous_attacks * p.base10_dangerous_attacks * scale
            used.append("dangerous_attacks")
        if den <= 0 or "corners" not in used or len(used) < 2:
            return r  # corners alone are too sparse to call momentum
        r.assessable = True
        r.inputs = tuple(used)
        r.index = (num_h + num_a) / (2.0 * den)  # both teams vs both-team baseline (= 1.0 at league pace)
        r.home_index = num_h / den
        r.away_index = num_a / den
        gd = state.home_goals - state.away_goals
        tot = num_h + num_a
        if gd != 0:
            lead = num_h if gd > 0 else num_a
            r.leader_share = (lead / tot) if tot > 0 else 0.5
        return r


# ---------------------------------------------------------------------------
# Cards model
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class CardsInputs:
    minute: float
    period: int
    cards: int  # v4.4: yellows + reds (1 each); v4.5: Bet365 points (yellow 1, red 2, max 3 per player)
    fouls: int
    goal_diff: int


@dataclass(slots=True)
class CardsModelResult:
    lam_remaining: float
    lam_prior: float
    net: float
    foul_factor: float
    booking_factor: float
    urgency: float
    cards90: float
    fouls90: float
    baseline_source: str
    current: int

    def to_dict(self) -> dict[str, Any]:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def team_card_points(state: MatchState, side: str) -> int | None:
    """Bet365 Total Cards points of one team: yellow 1, red 2, a second-yellow dismissal 3 (max 3 per player).

    Uses ``<side>_card_points`` when the feed reconstructed it per player (quantbet.espn); otherwise
    ``yellows + 2 x reds``, which is exact when the yellow count still contains the first yellow of a
    second-yellow dismissal (ESPN scoreboard *details*, Football-Data European divisions) and
    undercounts such a dismissal by 1 when it does not (ESPN boxscore ``yellowCards``, see espn.py).
    ``None`` when the yellow count is missing.
    """
    pts = getattr(state, f"{side}_card_points", None)
    if pts is not None:
        return int(pts)
    y = getattr(state, f"{side}_yellow_cards", None)
    if y is None:
        return None
    return int(y) + 2 * int(getattr(state, f"{side}_red_cards", 0) or 0)


def cards_inputs(state: MatchState, rule: str = "bet365") -> CardsInputs | None:
    """Fouls + card count from the live state; ``None`` if any of it is missing (-> no cards alert).

    ``rule="bet365"`` (v4.5-sharp, default): Total Cards points, yellow 1 / red 2 (:func:`team_card_points`).
    ``rule="y1_r1"``: the frozen v4.4-syndicate count, yellows + reds at 1 each (kept so v4.4 replays exactly).
    """
    hf, af = getattr(state, "home_fouls", None), getattr(state, "away_fouls", None)
    hy, ay = getattr(state, "home_yellow_cards", None), getattr(state, "away_yellow_cards", None)
    if hf is None or af is None:
        return None
    if rule == "y1_r1":
        if hy is None or ay is None:
            return None
        cards = int(hy) + int(ay) + int(state.home_red_cards or 0) + int(state.away_red_cards or 0)
    else:
        hp, ap = team_card_points(state, "home"), team_card_points(state, "away")
        if hp is None or ap is None:
            return None
        cards = hp + ap
    return CardsInputs(float(state.minute), int(getattr(state, "period", 1) or 1), cards,
                       int(hf) + int(af), state.home_goals - state.away_goals)


def cards_baseline(priors: Mapping[str, Any], slug: str | None, params: SyndicateParams) -> tuple[float, float, str]:
    """``(cards/90, fouls/90, source label)``."""
    b = (priors.get("cards_baselines") or {}).get(slug or "")
    if b:
        return float(b["cards90"]), float(b["fouls90"]), f"football_data:{b.get('fd_code', slug)}"
    pooled = priors.get("cards_pooled") or {}
    if pooled.get("cards90"):
        return float(pooled["cards90"]), float(pooled["fouls90"]), "football_data:pooled (league not covered)"
    return params.cards_default_cards90, params.cards_default_fouls90, "ASSUMPTION: default 4.3 cards / 24 fouls per 90"


def cards_baseline_bet365(priors: Mapping[str, Any], slug: str | None, params: SyndicateParams
                          ) -> tuple[float, float, str]:
    """v4.5: ``(Total Cards points/90 under yellow 1 / red 2, fouls/90, source)`` from ``cards_bet365``."""
    blk = priors.get("cards_bet365") or {}
    b = (blk.get("baselines") or {}).get(slug or "")
    if b:
        return float(b["cards90"]), float(b["fouls90"]), f"football_data:{b.get('fd_code', slug)} (Y=1,R=2)"
    pooled = blk.get("pooled") or {}
    if pooled.get("cards90"):
        return float(pooled["cards90"]), float(pooled["fouls90"]), "football_data:pooled (Y=1,R=2; league not covered)"
    return (params.cards_default_cards90, params.cards_default_fouls90,
            f"ASSUMPTION: default {params.cards_default_cards90:g} cards / {params.cards_default_fouls90:g} fouls per 90")


def cards_nb_r(priors: Mapping[str, Any]) -> float | None:
    """Pooled within-division NegBin shape r of the 90-minute Total Cards count (None -> Poisson)."""
    r = (priors.get("cards_bet365") or {}).get("nb_r")
    return float(r) if r else None


def _minute_rates(cards90: float, h2_share: float, p: SyndicateParams) -> tuple[float, float]:
    r1 = cards90 * (1.0 - h2_share) / (45.0 + p.cards_stoppage_h1)
    r2 = cards90 * h2_share / (45.0 + p.cards_stoppage_h2)
    return r1, r2


def _expected_between(t0: float, t1: float, period_of: int, r1: float, r2: float, p: SyndicateParams,
                      urgency: float = 1.0, gd_close: bool = False) -> float:
    """Expected cards on (t0, t1] of the regulation clock with the half profile (+ late urgency)."""
    h1_end = 45.0 + p.cards_stoppage_h1
    total = 0.0
    if period_of == 1:  # remaining first half (incl. its stoppage), then a full second half
        total += max(0.0, h1_end - max(t0, 0.0)) * r1
        t0 = 45.0
    end = 90.0 + p.cards_stoppage_h2
    t0 = max(t0, 45.0)
    t1 = min(t1, end)
    if t1 > t0:
        total += (t1 - t0) * r2
        if gd_close and urgency != 1.0:
            lo, hi = p.cards_urgency_window
            ov = max(0.0, min(t1, hi) - max(t0, lo))
            total += ov * r2 * (urgency - 1.0)
    return total


def cards_model(inp: CardsInputs, cards90: float, fouls90: float, params: SyndicateParams,
                h2_share: float | None = None, baseline_source: str = "") -> CardsModelResult:
    """Live Poisson remaining-cards lambda.

    ``lam_prior`` = baseline cards/90 spread with the half profile over the remaining clock (what a
    market ignoring the live discipline picture would price). ``lam_remaining`` = prior x foul
    factor^w x booking factor^w x late urgency (|gd| <= 1 inside 60'-85'):

    * foul factor    = (fouls + a) / (expected fouls so far + a), a = ``cards_foul_pseudo``
    * booking factor = (cards + b) / (expected cards so far + b), b = ``cards_book_pseudo``

    Both factors are shrunk toward 1 by the pseudo counts; their geometric weighting avoids
    double-counting (cards and fouls are correlated). These are heuristics, uncalibrated.
    """
    p = params
    h2 = p.cards_default_h2_share if h2_share is None else h2_share
    r1, r2 = _minute_rates(cards90, h2, p)
    t = max(0.0, inp.minute)
    period = 2 if (inp.period >= 2 or t > 45.0 + p.cards_stoppage_h1) else 1
    lam_prior = _expected_between(t, 1e9, period, r1, r2, p)
    if period == 1:
        elapsed_cards = t * r1
    else:
        elapsed_cards = (45.0 + p.cards_stoppage_h1) * r1 + max(0.0, t - 45.0) * r2
    exp_fouls = fouls90 * min(t, 90.0) / 90.0
    lo, hi = p.cards_factor_clip
    ff = (inp.fouls + p.cards_foul_pseudo) / (exp_fouls + p.cards_foul_pseudo)
    bf = (inp.cards + p.cards_book_pseudo) / (elapsed_cards + p.cards_book_pseudo)
    m = min(hi, max(lo, (ff ** p.cards_foul_weight) * (bf ** p.cards_book_weight)))
    close = abs(inp.goal_diff) <= p.cards_urgency_max_gd
    lam_live_base = _expected_between(t, 1e9, period, r1, r2, p, urgency=p.cards_urgency_mult, gd_close=close)
    urgency = (lam_live_base / lam_prior) if lam_prior > 1e-12 else 1.0
    lam = lam_live_base * m
    return CardsModelResult(lam, lam_prior, (lam / lam_prior) if lam_prior > 1e-12 else 1.0, ff, bf, urgency,
                            cards90, fouls90, baseline_source, inp.cards)


def price_card_lines(res: CardsModelResult, cfg: PricingConfig, sizer: StakeSizer | None, span: float = 5.0,
                     nb_r: float | None = None) -> list[tuple[LinePrice, float]]:
    """Half-line Over/Under card prices ``(LinePrice, prior fair)`` for ``current+0.5 .. current+span``.

    ``nb_r`` None -> Poisson (v4.4); else a Negative Binomial with shape ``nb_r`` and the remaining-time
    mean (model ``lam_remaining``; the prior fair uses ``lam_prior`` with the same shape).

    Why the 90-minute r is used unchanged for the remaining time: gamma-frailty model - a match's card
    rate is ``rate(t) x G`` with ``G ~ Gamma(r, 1/r)`` constant over the match, so the count on ANY
    sub-interval with mean ``mu`` is NegBin(shape r, mean mu), ``Var = mu + mu^2 / r``. Only the mean is
    scaled to the remaining time. Not conditioning ``G`` on the cards already shown (the booking factor
    moves the mean instead) keeps slightly more variance than a full posterior update: conservative.
    """
    out = []
    n = int(round(span))
    r = None if nb_r is None else float(nb_r)
    for i in range(n):
        line = res.current + 0.5 + i
        for sel in (Selection.OVER, Selection.UNDER):
            sp = negbin_total_settlement(res.current, res.lam_remaining, r, line, sel)
            lp = price_from_settlement(MarketType.TOTAL_CARDS, sel, line, sp, cfg, sizer)
            if lp is None or not (cfg.min_fair_odds <= lp.fair_odds <= cfg.max_fair_odds):
                continue
            pf = negbin_total_settlement(res.current, res.lam_prior, r, line, sel).fair_odds()
            out.append((lp, pf))
    return out


# ---------------------------------------------------------------------------
# Bet Builder / combos
# ---------------------------------------------------------------------------
def pairwise_joint(pa: float, pb: float, rho: float) -> float:
    """``P(A and B) = PA PB + rho sqrt(PA(1-PA) PB(1-PB))`` clipped to ``[max(0, PA+PB-1), min(PA, PB)]``."""
    raw = pa * pb + rho * math.sqrt(max(0.0, pa * (1 - pa) * pb * (1 - pb)))
    return min(min(pa, pb), max(max(0.0, pa + pb - 1.0), raw))


def frechet_bounds(ps: Sequence[float]) -> tuple[float, float]:
    """Joint-probability bounds for ``len(ps)`` events: ``[max(0, sum - (n-1)), min]``."""
    return max(0.0, sum(ps) - (len(ps) - 1)), min(ps)


def bvn_cdf(h: float, k: float, r: float) -> float:
    """Bivariate standard normal ``P(Z1 <= h, Z2 <= k)`` with correlation ``r`` (Owen's T; deterministic)."""
    if abs(r) < 1e-12:
        return float(ndtr(h) * ndtr(k))
    r = max(-0.999999, min(0.999999, r))
    eps = 1e-10
    h = h if abs(h) > eps else eps
    k = k if abs(k) > eps else eps
    s = math.sqrt(1.0 - r * r)
    ah = (k - r * h) / (h * s)
    ak = (h - r * k) / (k * s)
    corr = 0.5 if (h * k < 0) else 0.0
    return float(0.5 * ndtr(h) + 0.5 * ndtr(k) - owens_t(h, ah) - owens_t(k, ak) - corr)


def latent_corr_for(pa: float, pb: float, joint: float) -> float:
    """Gaussian-copula correlation reproducing the pairwise joint probability (bisection)."""
    h, k = float(ndtri(pa)), float(ndtri(pb))
    lo, hi = -0.999, 0.999
    f_lo, f_hi = bvn_cdf(h, k, lo), bvn_cdf(h, k, hi)
    if joint <= f_lo:
        return lo
    if joint >= f_hi:
        return hi
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if bvn_cdf(h, k, mid) < joint:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _nearest_psd_corr(m: np.ndarray) -> np.ndarray:
    w, v = np.linalg.eigh(m)
    if w.min() >= 1e-10:
        return m
    w = np.clip(w, 1e-8, None)
    out = v @ np.diag(w) @ v.T
    d = np.sqrt(np.diag(out))
    return out / np.outer(d, d)


def joint_probability(ps: Sequence[float], rho: np.ndarray | Sequence[Sequence[float]], draws: int = 200_000,
                      seed: int = 20261028) -> tuple[float, str]:
    """P(all legs win) and the method label.

    * 1 leg: itself; 2 legs: :func:`pairwise_joint` (closed form, Frechet-clipped).
    * 3+ legs: Gaussian copula Monte Carlo with a fixed seed; each latent pair correlation is
      solved so the copula reproduces the clipped pairwise joint exactly; the estimate is then
      clipped to the n-way Frechet bounds. With rho = 0 this is the independent product.
    """
    ps = [min(1 - 1e-9, max(1e-9, float(x))) for x in ps]
    R = np.asarray(rho, dtype=float)
    n = len(ps)
    if n == 1:
        return ps[0], "single"
    if n == 2:
        return pairwise_joint(ps[0], ps[1], float(R[0, 1])), "pairwise_frechet"
    if np.allclose(R - np.eye(n), 0.0):
        return float(np.prod(ps)), "independent"
    L = np.eye(n)
    for i in range(n):
        for j in range(i + 1, n):
            L[i, j] = L[j, i] = latent_corr_for(ps[i], ps[j], pairwise_joint(ps[i], ps[j], float(R[i, j])))
    L = _nearest_psd_corr(L)
    rng = np.random.default_rng(seed)
    z = rng.standard_normal((draws, n)) @ np.linalg.cholesky(L).T
    thr = ndtri(np.asarray(ps))
    est = float(np.mean(np.all(z <= thr, axis=1)))
    lo, hi = frechet_bounds(ps)
    return min(hi, max(lo, est)), "gaussian_copula_mc"


def leg_rho(fam_a: str, sel_a: Selection, fam_b: str, sel_b: Selection, rho_table: Mapping[str, float]) -> float:
    """Indicator correlation of two legs; an Under is the complement of an Over (sign flips)."""
    if fam_a == fam_b:
        return 0.0
    r = rho_table.get(f"{fam_a}|{fam_b}", rho_table.get(f"{fam_b}|{fam_a}", 0.0))
    s = (1 if sel_a is Selection.OVER else -1) * (1 if sel_b is Selection.OVER else -1)
    return float(r) * s


def evaluate_combo(legs: Sequence[Mapping[str, Any]], rho_table: Mapping[str, float], hurdle: float,
                   params: SyndicateParams) -> dict[str, Any]:
    """Combined fair odds, naive independent odds and the minimum Bet365 combo odds for 2-3 legs.

    ``legs``: ``{"family", "selection" (Selection), "line", "p", "label"}``.
    """
    n = len(legs)
    R = np.eye(n)
    for i in range(n):
        for j in range(i + 1, n):
            R[i, j] = R[j, i] = leg_rho(legs[i]["family"], legs[i]["selection"], legs[j]["family"],
                                        legs[j]["selection"], rho_table)
    ps = [float(x["p"]) for x in legs]
    pj, method = joint_probability(ps, R, params.combo_mc_draws, params.combo_seed)
    naive_p = float(np.prod(ps))
    o_min = _ceil2((1.0 + hurdle) / pj) if pj > 0 else math.inf
    out = {"legs": [dict(label=x["label"], family=x["family"], p=round(float(x["p"]), 4)) for x in legs],
           "p_joint": round(pj, 5), "fair_odds": round(1.0 / pj, 3) if pj > 0 else None,
           "naive_odds": round(1.0 / naive_p, 3) if naive_p > 0 else None, "min_odds": o_min,
           "method": method, "rho": [[round(float(v), 4) for v in row] for row in R], "hurdle": hurdle}
    if params.boost_variant_enabled and math.isfinite(o_min):
        out["boost_pct"] = params.boost_pct
        out["boost_required_odds"] = _ceil2(boost_required_odds(o_min, params.boost_pct))
    return out


# ---------------------------------------------------------------------------
# De-duplication (per match x family)
# ---------------------------------------------------------------------------
def combo_leg_selection(sp: SyndicateParams, family: str, net: float, tier: str | None) -> Selection | None:
    """Direction of the extra combo leg for ``family`` (``None`` = no leg).

    v4.4: Over if the family's net multiplier >= 1 else Under (always a leg). v4.5-sharp
    (``combo_gated``): a leg only when that family independently passes its directional net gate -
    Over if net >= its ``over_net_min`` (cards: ``cards_net_over``), Under if net <= ``under_max_net``
    (cards: ``cards_net_under``); paper-only cards and families the tier does not allow are never legs.
    """
    if not sp.combo_gated():
        return Selection.OVER if net >= 1.0 else Selection.UNDER
    if (family == "cards" and sp.cards_paper()) or not sp.tier_allows(tier, family):
        return None
    o_min = sp.cards_net_over if family == "cards" else sp.over_net_min(family)
    u_max = sp.cards_net_under if family == "cards" else sp.under_max_net
    if net >= o_min - 1e-12:
        return Selection.OVER
    if net <= u_max + 1e-12:
        return Selection.UNDER
    return None


def mark_paper_only(sig: Signal) -> Signal:
    """Plumbing: a paper-only alert is logged and graded at 1u flat but NEVER staked.

    Zeroes ``kelly_fraction`` / ``stake_units`` / ``stake_currency`` before sizing and DB insertion, moves the
    theoretical first-tier stake and ladder to ``extra["paper_stake"]`` / ``extra["paper_tiers"]`` (for reference
    only) and empties ``extra["tiers"]``. :meth:`quantbet.risk.RiskManager.size` leaves such signals unsized and
    :meth:`quantbet.database.AlertDatabase.report` excludes them from turnover / profit.
    """
    x = sig.extra
    x["paper_only"] = True
    tiers = x.get("tiers") or []
    x["paper_stake"] = float(tiers[0][1]) if tiers else float(sig.stake_currency or 0.0)
    x["paper_tiers"] = [list(t) for t in tiers]
    x["tiers"] = []
    sig.kelly_fraction = 0.0
    sig.stake_units = 0.0
    sig.stake_currency = 0.0
    return sig


class SyndicateDeduplicator:
    """Cooldown + material-change re-alert + family/match caps + 5% match stake cap (all families)."""

    def __init__(self, params: SyndicateParams, bankroll: float) -> None:
        self.p = params
        self.bankroll = bankroll
        self.family: dict[tuple[str, str], dict[str, Any]] = {}
        self.match: dict[str, dict[str, float]] = {}
        # v4.5 patch direction lock: selections alerted per match (staked / paper-only kept apart)
        self.dirs: dict[str, set[str]] = {}
        self.paper_dirs: dict[str, set[str]] = {}

    def last(self, event_key: str, family: str) -> dict[str, Any] | None:
        return self.family.get((event_key, family))

    def check(self, event_key: str, family: str, selection: str, line: float, stake: float, minute: float,
              p_now_last_line: float | None, pending_stake: float = 0.0, pending_n: int = 0,
              paper: bool = False, pending_dirs: Sequence[str] = ()) -> str | None:
        """Reason to drop, or None. ``paper`` (v4.5 paper-only cards): the family rules (conflict,
        cooldown, family cap, material change) apply, the match signal / stake caps do not.

        Direction lock (v4.5 patch, ``params.direction_lock()``): if any family of the match already
        alerted the other direction (or one is pending in this poll, ``pending_dirs``), the alert is dropped
        as ``match_direction_conflict``. A staked alert is checked against staked alerts only, so a paper
        card can never block a real bet; a paper card is checked against staked AND paper alerts, so the
        paper book never holds Over and Under in the same match either."""
        p = self.p
        f = self.family.get((event_key, family))
        m = self.match.get(event_key, {"stake": 0.0, "n": 0})
        if p.direction_lock():
            seen = set(self.dirs.get(event_key, ())) | set(pending_dirs)
            if paper:
                seen |= self.paper_dirs.get(event_key, set())
            if any(d != selection for d in seen):
                return "match_direction_conflict"
        if f is not None:
            if f["selection"] != selection:
                return "family_conflict"
            if minute - f["minute"] < p.cooldown_min - 1e-9:
                return "cooldown"
            if f["count"] >= p.max_per_family:
                return "family_cap"
            moved = abs(line - f["line"]) >= p.realert_line_move - 1e-9
            shifted = p_now_last_line is not None and abs(p_now_last_line - f["p"]) >= p.realert_p_shift - 1e-9
            if not (moved or shifted):
                return "no_material_change"
        if paper:
            return None
        if m["n"] + pending_n >= p.max_per_match:
            return "match_signal_cap"
        if m["stake"] + pending_stake + stake > p.max_match_stake_frac * self.bankroll + 1e-9:
            return "stake_cap"
        return None

    def remaining_stake(self, event_key: str) -> float:
        m = self.match.get(event_key, {"stake": 0.0, "n": 0})
        return max(0.0, self.p.max_match_stake_frac * self.bankroll - m["stake"])

    def record(self, event_key: str, family: str, selection: str, line: float, p_line: float, stake: float,
               minute: float, paper: bool = False) -> None:
        f = self.family.get((event_key, family))
        self.family[(event_key, family)] = {"selection": selection, "line": line, "p": p_line, "minute": minute,
                                            "count": (f["count"] if f else 0) + 1}
        (self.paper_dirs if paper else self.dirs).setdefault(event_key, set()).add(selection)
        if paper:  # v4.5 paper-only cards: never consume the match signal count or the 5% stake cap
            return
        m = self.match.setdefault(event_key, {"stake": 0.0, "n": 0})
        m["stake"] += stake
        m["n"] += 1

    def load_recent_alerts(self, db_path: Path | str, lookback_s: float = 10800.0,
                           now: float | None = None) -> int:
        """Plumbing: rebuild the dedup / cap state from ``quantbet.sqlite3`` after a restart.

        Replays, oldest first, every alert with ``created_at >= now - lookback_s`` whose ``extra["dedup"]``
        carries the v4.4+/v4.5 syndicate record (``syndicate, family, selection, line, p, stake, minute`` and
        ``paper``) through :meth:`record` - exactly what :meth:`SyndicateSignalEngine.commit` did live - so
        ``family`` (last selection / line / p / match minute / count), ``match`` (staked count and the 5% stake
        cap exposure), ``dirs`` and ``paper_dirs`` are restored. Paper-only alerts (``extra["paper_only"]`` or
        ``dedup["paper"]``) restore the family state and ``paper_dirs`` only; they never consume the match caps.

        Cooldown: ``minute`` is the MATCH minute at alert time and the cooldown compares it with the match
        minute of the next observed state of the same match, so no wall-clock reconstruction is needed (the
        match clock already advanced while the poller was down; half-time is not counted, as live).

        A missing DB, a DB without an ``alerts`` table / the needed columns, rows with malformed or pre-v4.4
        ``extra`` (no ``dedup.syndicate``) are skipped. Returns the number of alerts replayed.
        """
        import sqlite3

        path = Path(db_path)
        if not path.is_file():
            log.info("dedup hydration: no alerts DB", extra={"db": str(path)})
            return 0
        since = (time.time() if now is None else now) - float(lookback_s)
        try:
            conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
            try:
                rows = conn.execute("SELECT event_key, created_at, extra FROM alerts WHERE created_at >= ? "
                                    "ORDER BY created_at", (since,)).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            log.warning("dedup hydration skipped: unreadable alerts DB", extra={"db": str(path), "error": str(exc)})
            return 0
        n = bad = 0
        for event_key, _created, extra in rows:
            try:
                x = json.loads(extra) if isinstance(extra, str) else None
                d = x.get("dedup") if isinstance(x, dict) else None
                if not (isinstance(d, dict) and d.get("syndicate")):
                    bad += 1
                    continue
                paper = bool(d.get("paper") or x.get("paper_only"))
                stake = 0.0 if paper else float(d.get("stake") or 0.0)
                self.record(str(event_key), str(d["family"]), str(d["selection"]), float(d["line"]),
                            float(d["p"]), stake, float(d["minute"]), paper=paper)
                n += 1
            except (TypeError, ValueError, KeyError, AttributeError):
                bad += 1
        log.info("dedup hydrated from alerts DB", extra={"db": str(path), "alerts": n, "skipped": bad,
                                                         "matches": len(self.match), "lookback_s": lookback_s})
        return n


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
def tier_pricing(pricing: PricingConfig, params: SyndicateParams, tier: str | None) -> PricingConfig:
    """Pricing with the tier hurdle as target edge and the abort floor scaled the same way."""
    return dataclasses.replace(pricing, target_edge=params.hurdle(tier), abort_edge=params.abort_edge(tier))


class SyndicateSignalEngine(GlobalSignalEngine):
    """v4.4-syndicate on top of v4.3-global (registry, tiers, telemetry gate and tier sizing kept).

    The goal/corner model, its frozen live params and the v4.2/v4.3 code paths are untouched; this
    class only adds gates/selection, the cards family, the per-family dedup and the combo block.
    """

    def __init__(self, registry: Any, risk: Any, *args: Any, params: SyndicateParams | None = None,
                 priors: Mapping[str, Any] | None = None, **kw: Any) -> None:
        super().__init__(registry, risk, *args, **kw)
        self.sp = params or SyndicateParams()
        self.priors = dict(priors or {})
        self.rho_table = {k: float(v) for k, v in (self.priors.get("rho") or {}).items()}
        prof = self.priors.get("cards_time_profile") or {}
        self.h2_share = float(prof["second_half_share"]) if prof.get("second_half_share") else None
        self.momentum = SustainedMomentumTracker(self.sp)
        self.sdedup = SyndicateDeduplicator(self.sp, self.pricing.bankroll)
        self.last_momentum: dict[str, MomentumView] = {}
        self.last_cards: dict[str, CardsModelResult | None] = {}
        self.version = self.sp.version()
        self.cards_r = cards_nb_r(self.priors) if self.sp.card_rule() == "bet365" and \
            getattr(self.sp, "cards_distribution", "poisson") == "negbin" else None

    # -- public -----------------------------------------------------------------
    def on_match_state(self, state: MatchState, now: float | None = None,
                       real_odds: Mapping[tuple[Any, float, Any], float] | None = None) -> list[Signal]:
        slug, tier = self.tier_for(state)
        if isinstance(self.resolver, GlobalRatingsResolver) and slug:
            self.resolver.slugs[state.event_key] = slug
        risk = self.risk
        self.sizer = lambda f, o, _t=tier: risk.stake_from_kelly(f, _t)
        sigs = self._evaluate(state, now, real_odds, slug, tier)
        prior = self.resolver.prior_info.get(state.event_key) if isinstance(self.resolver, GlobalRatingsResolver) else None
        for s in sigs:
            s.extra["liquidity_tier"] = tier
            s.extra["league_slug"] = slug
            if prior:
                s.extra["prior"] = dict(prior)
            if self.probation is not None:
                try:
                    tag = self.probation.tag(slug, tier)
                except Exception:  # noqa: BLE001 - presentation only
                    log.exception("probation lookup failed")
                    tag = ""
                if tag:
                    s.extra["probation"] = tag
        return sigs

    def commit(self, sig: Signal) -> None:
        d = (sig.extra or {}).get("dedup")
        if d and d.get("syndicate"):
            self.sdedup.record(sig.event_key, d["family"], d["selection"], d["line"], d["p"], d["stake"], d["minute"],
                               paper=bool(d.get("paper")))

    # -- internals ----------------------------------------------------------------
    def _evaluate(self, state: MatchState, now: float | None, real_odds: Mapping[Any, float] | None,
                  slug: str | None, tier: str) -> list[Signal]:
        sp = self.sp
        now = time.time() if now is None else now
        key = state.event_key
        home = getattr(state, "home", "") or "Home"
        away = getattr(state, "away", "") or "Away"
        league = getattr(state, "league", "")
        kickoff = getattr(state, "commence_time", None)
        ratings, src = self.resolver.resolve(key, home, away, league, kickoff)
        self.engine.update_match_state(state)
        self.engine.set_ratings(key, ratings)
        if key not in self.engine.events:
            self.engine.events[key] = EventInfo(key, home, away, league, kickoff, True)
        signals = self.engine.evaluate_live_event(key, now)
        mom = self.momentum.update(state, ratings, self.model.p, now)
        self.last_momentum[key] = mom
        cfg = tier_pricing(self.pricing, sp, tier)
        prices = price_live_lines(state, ratings, cfg, self.model, self.sizer)
        card_res: CardsModelResult | None = None
        card_prices: list[tuple[LinePrice, float]] = []
        rule = sp.card_rule()
        ci = cards_inputs(state, rule) if sp.cards_enabled else None
        if ci is not None:
            if rule == "bet365":
                c90, f90, csrc = cards_baseline_bet365(self.priors, slug, sp)
            else:
                c90, f90, csrc = cards_baseline(self.priors, slug, sp)
            card_res = cards_model(ci, c90, f90, sp, self.h2_share, csrc)
            card_prices = price_card_lines(card_res, cfg, self.sizer, sp.cards_line_span, self.cards_r)
        self.last_cards[key] = card_res
        self.last_prices[key] = prices + [lp for lp, _ in card_prices]
        trace: dict[tuple[Any, float, Any], str] = {}
        self.last_trace[key] = trace
        listed = bool(slug) and bool(getattr(self.registry, "listed", lambda _s: False)(slug))
        block = sp.hygiene_block(slug, listed, home, away)
        if block:  # v4.5: unmapped league / youth-reserve team -> no alerts at all
            for lp in self.last_prices[key]:
                trace[(lp.market, lp.line, lp.selection)] = block
            return []
        bd = cfg.blowout_block_diff
        if bd is not None and abs(state.home_goals - state.away_goals) >= bd:
            self.blowout.add(key)
        conf = assess_confidence(state, src, cfg, ratings, now, self.model.p)
        if not conf.ok:
            for lp in self.last_prices[key]:
                trace[(lp.market, lp.line, lp.selection)] = "gate: " + "; ".join(conf.reasons)
            return signals
        triggers = [t for t in conf.triggers if t != "activity_high"] + mom.labels()
        conf = ConfidenceReport(conf.ok, conf.reasons, tuple(dict.fromkeys(triggers)), conf.direction)
        ref = self._reference_fair(state, ratings)
        lift = self.lift_context(state, ratings)
        self.last_lift[key] = lift
        req = sp.lift_required(tier) if sp.achievability_gate else 1.0
        tlo, thi = cfg.fair_target_range
        lo, hi = cfg.alert_fair_odds_range
        pools: dict[str, list[tuple[float, LinePrice, float | None, float | None]]] = {}

        def consider(lp: LinePrice, fam: str, pf: float | None, net: float, evr: float | None) -> None:
            k = (lp.market, lp.line, lp.selection)
            over = lp.selection is Selection.OVER
            if key in self.blowout and over:
                trace[k] = "blowout"
                return
            if not sp.tier_allows(tier, fam):
                trace[k] = "tier3_market_blocked (TIER_3 = Goals Over/Under only)"
                return
            if fam == "cards":
                if over and net < sp.cards_net_over:
                    trace[k] = "cards: net < %.2f" % sp.cards_net_over
                    return
                if not over and net > sp.cards_net_under:
                    trace[k] = "cards: net > %.2f" % sp.cards_net_under
                    return
            elif over:
                if mom.bearish:
                    trace[k] = "bearish: " + ",".join(x for x in mom.labels() if x in ("tempo_stall", "bus_parking"))
                    return
                if net <= sp.over_suppress_net:
                    trace[k] = "lift: net multiplier <= %.2f (Overs suppressed)" % sp.over_suppress_net
                    return
                if net < sp.over_net_min(fam):
                    trace[k] = "lift: net multiplier < %.2f" % sp.over_net_min(fam)
                    return
                cap = sp.flurry_cap()
                if cap is not None and mom.reading.assessable and mom.reading.adj_index > cap + 1e-12:
                    trace[k] = "momentum_flurry_cap (index %.2f > %.2f)" % (mom.reading.adj_index, cap)
                    return
                if not mom.sustained_over:
                    trace[k] = "no_sustained_momentum"
                    return
                if sp.activity_confirm_overs():
                    mt = getattr(state, "momentum_mult_total", None) or 0.0
                    if not (mt >= cfg.activity_trigger_mult - 1e-12 or mom.fav_trailing):
                        trace[k] = "no_activity_confirm (mult_total %.2f < %.2f, not fav_trailing)" % (
                            mt, cfg.activity_trigger_mult)
                        return
            else:
                if not sp.unders_enabled:
                    trace[k] = "unders disabled"
                    return
                if sp.unders_half_lines_only and not is_half_line(lp.line):
                    trace[k] = "under: half lines only"
                    return
                if mom.bullish:
                    trace[k] = "bullish: suppresses Unders" + (" (attack momentum / activity)" if (
                        mom.activity_bullish and not (mom.sustained_over or mom.fav_trailing or mom.knockout_level)) else "")
                    return
                if not (mom.tempo_stall or (mom.bus_parking and fam == "goals")):
                    trace[k] = "under: no tempo_stall/bus_parking"
                    return
                if net > sp.under_max_net:
                    trace[k] = "lift: Under needs net <= %.2f" % sp.under_max_net
                    return
            if pf is None or lp.fair_odds > pf / req + 1e-12:
                trace[k] = "tier_lift < %.1f%% (%s)" % ((req - 1) * 100, tier)
                return
            if fam != "cards" and pf / lp.fair_odds - 1.0 > sp.lift_cap(fam) + 1e-12:
                trace[k] = "lift > %.0f%% (model over-reaction guard)" % (sp.lift_cap(fam) * 100)
                return
            if not (lo <= lp.fair_odds <= hi):
                trace[k] = "fair_range"
                return
            if not lp.tiers:
                trace[k] = "no_tiers"
                return
            if cfg.drop_negative_reference_ev and evr is not None and evr < 0:
                trace[k] = "neg_ref_ev"
                return
            if not (tlo <= lp.fair_odds <= thi):
                trace[k] = "fair_target_range"
                return
            trace[k] = "not_best"
            pools.setdefault(fam, []).append((-abs(lp.fair_odds - cfg.fair_target), lp, pf, evr))

        for lp in prices:
            fam = syndicate_family(lp.market)
            fl = lift[fam]
            rf = ref.get((lp.market, lp.line, lp.selection))
            consider(lp, fam, fl["prior_fair"].get((lp.line, lp.selection)), fl["net"],
                     round(lp.ev_at(rf), 5) if rf else None)
        if card_res is not None:
            for lp, pf in card_prices:
                consider(lp, "cards", pf, card_res.net, None)
        best = sorted((max(v, key=lambda x: (x[0], x[1].ev_at(x[1].min_odds))) for v in pools.values()),
                      key=lambda x: -x[0])
        pend_stake, pend_n = 0.0, 0
        pend_dirs: set[str] = set()
        pend_pdirs: set[str] = set()
        for _score, lp, pf, evr in best:
            k = (lp.market, lp.line, lp.selection)
            fam = syndicate_family(lp.market)
            stake = lp.tiers[0].stake
            last = self.sdedup.last(key, fam)
            p_last = None
            if last is not None:
                p_last = self._p_of(key, lp.market, last["line"], lp.selection)
            paper = fam == "cards" and sp.cards_paper()
            why = self.sdedup.check(key, fam, lp.selection.value, lp.line, stake, state.minute, p_last, pend_stake,
                                    pend_n, paper=paper, pending_dirs=tuple(pend_dirs | (pend_pdirs if paper else set())))
            if why:
                trace[k] = why
                continue
            sig = self._signal(state, lp, conf, src, ratings, ref, evr, kickoff, home, away, league)
            if fam == "cards":
                sig.method = "live:negbin_cards" if self.cards_r else "live:poisson_cards"
                sig.extra["cards_model"] = card_res.to_dict() if card_res else None
                if card_res is not None:
                    sig.extra["cards_model"]["rule"] = rule
                    sig.extra["cards_model"]["nb_r"] = round(self.cards_r, 4) if self.cards_r else None
                sig.extra["lambda_pre_total"] = None
            if paper:
                mark_paper_only(sig)
            sig.extra["log_growth"] = round(expected_log_growth(lp), 6)
            net = card_res.net if (fam == "cards" and card_res) else lift[fam]["net"]
            sig.extra["lift"] = {"prior_fair": round(pf, 4) if pf else None,
                                 "lift": round(pf / lp.fair_odds - 1.0, 4) if pf else None,
                                 "net_multiplier": round(net, 4),
                                 "lambda_model": round(card_res.lam_remaining if fam == "cards" and card_res else lift[fam]["lam_model"], 4),
                                 "lambda_prior": round(card_res.lam_prior if fam == "cards" and card_res else lift[fam]["lam_prior"], 4)}
            sig.extra["selection_mode"] = cfg.selection_mode
            sig.extra["rules"] = self.version
            sig.extra["momentum"] = mom.to_dict()
            hurdle, margin = sp.hurdle(tier), sp.margin(tier)
            sig.extra["syndicate"] = {"tier": tier, "hurdle": hurdle, "abort_edge": sp.abort_edge(tier),
                                      "assumed_margin": margin, "lift_required": round(req, 4)}
            if self.version != SYNDICATE_VERSION:
                sig.extra["syndicate"]["achievability_gate"] = sp.achievability_gate
                if fam != "cards":
                    sig.extra["syndicate"]["lift_cap"] = sp.lift_cap(fam)
            sig.extra["boost_target"] = self._boost_target(lp, pf, margin)
            sig.extra["dedup"] = {"syndicate": True, "family": fam, "selection": lp.selection.value, "line": lp.line,
                                  "p": round(lp.p_true, 5), "stake": stake, "minute": state.minute,
                                  "reason": "first" if last is None else "material_change"}
            if paper:
                sig.extra["dedup"]["paper"] = True
            if lp.notes:
                sig.extra["notes"] = list(lp.notes)
            if (cfg.corner_over_fotmob_after is not None and lp.market is MarketType.TOTAL_CORNERS
                    and lp.selection is Selection.OVER and state.minute > cfg.corner_over_fotmob_after):
                fc = favourite_chasing(state, ratings, self.model.p)
                mh = getattr(state, "momentum_mult_home", None) or 1.0
                ma = getattr(state, "momentum_mult_away", None) or 1.0
                sig.extra["fotmob_required"] = "dominance"
                sig.extra["dominant_side"] = fc[0] if fc else ("home" if mh >= ma else "away")
            if sp.combo_enabled and not paper:
                combos = self._combos(key, lp, fam, lift, card_res, card_prices, prices, tier, stake + pend_stake)
                if combos:
                    sig.extra["combos"] = combos
            trace[k] = "emitted"
            (pend_pdirs if paper else pend_dirs).add(lp.selection.value)
            if self.auto_commit:
                self.commit(sig)
            elif not paper:
                pend_stake += stake
                pend_n += 1
            signals.append(sig)
        return signals

    def _p_of(self, key: str, market: MarketType, line: float, sel: Selection) -> float | None:
        for lp in self.last_prices.get(key, []):
            if lp.market is market and abs(lp.line - line) < 1e-9 and lp.selection is sel:
                return lp.p_true
        return None

    def _boost_target(self, lp: LinePrice, prior_fair: float | None, margin: float) -> dict[str, Any]:
        bt: dict[str, Any] = {"min_odds": lp.min_odds, "hurdle": lp.target_edge}
        if prior_fair:
            typical = prior_fair * (1.0 - margin)  # ASSUMED unboosted Bet365 price (prior fair less margin)
            bt["typical_unboosted"] = round(typical, 2)
            bt["boost_needed_pct"] = round(max(0.0, (lp.min_odds - 1.0) / max(typical - 1.0, 1e-9) - 1.0), 4)
        if self.sp.boost_variant_enabled:
            bt["boost_pct"] = self.sp.boost_pct
            bt["pre_boost_odds"] = _ceil2(boost_required_odds(lp.min_odds, self.sp.boost_pct))
        return bt

    def _combo_leg(self, lp: LinePrice, fam: str) -> dict[str, Any]:
        return {"family": fam, "selection": lp.selection, "line": lp.line, "p": lp.p_true,
                "label": f"{lp.selection.value.capitalize()} {lp.line:g} {fam.capitalize()}"}

    def _combos(self, key: str, lp: LinePrice, fam: str, lift: Mapping[str, Any], card_res: CardsModelResult | None,
                card_prices: list[tuple[LinePrice, float]], prices: list[LinePrice], tier: str,
                committed_here: float) -> list[dict[str, Any]]:
        """Informational same-match builder: the alert leg (nearest half line) + best leg of each other family."""
        sp = self.sp
        flo, fhi = sp.combo_leg_fair_range

        def pick(cands: list[LinePrice], sel: Selection) -> LinePrice | None:
            c = [x for x in cands if x.selection is sel and is_half_line(x.line) and flo <= x.fair_odds <= fhi]
            return min(c, key=lambda x: abs(x.fair_odds - 1.95)) if c else None

        fam_prices = {"goals": [x for x in prices if x.market is MarketType.TOTAL_GOALS],
                      "corners": [x for x in prices if x.market is MarketType.TOTAL_CORNERS],
                      "cards": [x for x, _ in card_prices]}
        base = lp if is_half_line(lp.line) else pick(fam_prices[fam], lp.selection)
        if base is None:
            return []
        legs = [self._combo_leg(base, fam)]
        for other in FAMILIES:
            if other == fam or not fam_prices[other]:
                continue
            net = card_res.net if (other == "cards" and card_res) else (lift.get(other) or {}).get("net", 1.0)
            sel = combo_leg_selection(sp, other, net, tier)
            if sel is None:
                continue
            leg = pick(fam_prices[other], sel)
            if leg is not None:
                legs.append(self._combo_leg(leg, other))
        if len(legs) < 2:
            return []
        hurdle = sp.hurdle(tier)
        out = []
        sets = [legs[:2]] if len(legs) == 2 else [legs[:2], [legs[0], legs[2]], legs[: sp.combo_max_legs]]
        room = max(0.0, self.sdedup.remaining_stake(key) - committed_here)
        for s in sets:
            c = evaluate_combo(s, self.rho_table, hurdle, sp)
            o = c["min_odds"]
            stake = 0.0
            if math.isfinite(o) and o > 1.0:
                pj = c["p_joint"]
                f = max(0.0, (pj * o - 1.0) / (o - 1.0)) * self.pricing.kelly_multiplier
                stake = float(self.sizer(f, o)) if self.sizer else 0.0
            c["max_stake"] = min(stake, room)
            c["stake_note"] = "informational; any stake shares the 5% match cap"
            out.append(c)
        return out


__all__ = [
    "SYNDICATE_VERSION", "SHARP_VERSION", "FAMILIES", "DEFAULT_PRIORS_PATH", "is_syndicate", "is_sharp",
    "syndicate_family", "is_half_line", "SyndicateParams", "SharpParams", "youth_reserve_hit", "load_priors",
    "priors_frozen_view", "sharp_priors_frozen_view", "syndicate_params_dict", "poisson_total_settlement",
    "negbin_total_settlement", "price_from_settlement", "boost_required_odds", "MomentumReading", "MomentumView",
    "is_knockout", "SustainedMomentumTracker", "CardsInputs", "CardsModelResult", "team_card_points", "cards_inputs", "combo_leg_selection",
    "cards_baseline", "cards_baseline_bet365", "cards_nb_r", "cards_model",
    "price_card_lines", "pairwise_joint", "frechet_bounds", "bvn_cdf", "latent_corr_for", "joint_probability",
    "leg_rho", "evaluate_combo", "SyndicateDeduplicator", "tier_pricing", "SyndicateSignalEngine",
]
