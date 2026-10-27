"""Pricing & EV engine.

Contents
--------
* Odds maths: implied probabilities, overround, four de-vig methods
  (multiplicative, additive, power, Shin), EV, fair odds, minimum acceptable odds,
  fractional Kelly.
* Asian-line settlement (whole / half / quarter lines, pushes and half results)
  with *exact* EV, fair odds and floor odds for those lines.
* ``SteamDetector``: rolling-window detector of sharp-price moves.
* Engine B, ``LiveFootballModel``: time-decaying bivariate-Poisson (goals) and
  Negative-Binomial (corners) in-play model with match-state multipliers.
* ``SignalEngine``: orchestrates Engine A (sharp de-vig + steam) and Engine B and
  emits :class:`~quantbet.models.Signal` objects when EV >= threshold.

Conventions
-----------
* All odds are decimal. All probabilities are in [0, 1].
* ``EV = p_true * odds - 1`` (per unit staked). For lines with pushes/half
  results we compute EV exactly from the settlement distribution and report an
  *effective* probability ``p_eff = (EV + 1) / odds`` so that the same formula
  holds everywhere downstream.
* Asian-handicap ``line`` is always the **home** handicap.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import numpy as np
from scipy.optimize import brentq, minimize
from scipy.stats import nbinom, poisson

from .models import (
    EventInfo,
    MarketKey,
    MarketType,
    MatchState,
    OddsSnapshot,
    Selection,
    Signal,
    Urgency,
)

log = logging.getLogger(__name__)

__all__ = [
    "rules_preset",
    "RULE_VERSIONS",
    "sib_dominance",
    "chasing_corner_cap_active",
    "implied_probabilities",
    "overround",
    "devig_multiplicative",
    "devig_additive",
    "devig_power",
    "devig_shin",
    "devig",
    "expected_value",
    "fair_odds",
    "min_acceptable_odds",
    "kelly_fraction",
    "split_asian_line",
    "SettlementProbs",
    "asian_settlement",
    "settle_bet",
    "SteamDetector",
    "SteamEvent",
    "PreMatchRatings",
    "LiveModelParams",
    "LiveFootballModel",
    "LivePrediction",
    "bivariate_poisson_matrix",
    "ratings_from_prices",
    "SignalEngine",
    "EngineConfig",
    "Signal",
    "StakeTier",
    "LinePrice",
    "PricingConfig",
    "build_stake_tiers",
    "price_line",
    "price_live_lines",
    "candidate_lines",
    "assess_confidence",
    "ConfidenceReport",
    "select_alert_lines",
    "AlertRateLimiter",
    "AlertDeduplicator",
    "market_family",
    "expected_log_growth",
    "favourite_chasing",
    "pre_match_win_probs",
    "chasing_corner_penalty_active",
    "SharpPrices",
    "RatingsResolver",
    "LEAGUE_DEFAULTS",
    "FreeSignalEngine",
]

_EPS = 1e-12


def _f(arr: np.ndarray) -> list[float]:
    """numpy array -> list of plain Python floats."""
    return [float(x) for x in arr]


# ---------------------------------------------------------------------------
# Basic odds maths
# ---------------------------------------------------------------------------
def _validate_odds(odds: Sequence[float]) -> np.ndarray:
    arr = np.asarray(odds, dtype=float)
    if arr.ndim != 1 or arr.size < 2:
        raise ValueError("need at least two outcomes")
    if np.any(~np.isfinite(arr)) or np.any(arr <= 1.0):
        raise ValueError(f"decimal odds must be finite and > 1.0, got {odds!r}")
    return arr


def implied_probabilities(odds: Sequence[float]) -> list[float]:
    """Raw implied probabilities ``1/odds`` (they sum to 1 + overround)."""
    return _f(1.0 / _validate_odds(odds))


def overround(odds: Sequence[float]) -> float:
    """Bookmaker margin: ``sum(1/odds) - 1`` (e.g. 0.05 for a 105% book)."""
    return float(np.sum(1.0 / _validate_odds(odds)) - 1.0)


def devig_multiplicative(odds: Sequence[float]) -> list[float]:
    """Normalise implied probabilities proportionally: ``p_i = q_i / sum(q)``."""
    q = 1.0 / _validate_odds(odds)
    return _f(q / q.sum())


def devig_additive(odds: Sequence[float]) -> list[float]:
    """Subtract the margin equally from every outcome: ``p_i = q_i - (B-1)/n``.

    If that would push an outcome below zero (very long shots in a high-margin
    book) the offending outcome is fixed at 0 and the margin is redistributed
    over the remaining outcomes; the result always sums to 1.
    """
    q = 1.0 / _validate_odds(odds)
    active = np.ones(q.size, dtype=bool)
    p = np.zeros_like(q)
    for _ in range(q.size):
        excess = q[active].sum() - 1.0
        p_active = q[active] - excess / active.sum()
        if np.all(p_active > 0):
            p[active] = p_active
            return _f(p)
        idx = np.flatnonzero(active)[p_active <= 0]
        active[idx] = False
        p[idx] = 0.0
    raise ValueError("additive de-vig failed")  # pragma: no cover


def devig_power(odds: Sequence[float]) -> list[float]:
    """Power method: find ``k`` such that ``sum(q_i ** k) = 1``; ``p_i = q_i ** k``.

    ``k > 1`` for a book with positive overround; this shades long shots more
    than favourites (favourite-longshot bias). Solved with Brent's method.
    """
    q = 1.0 / _validate_odds(odds)
    if abs(q.sum() - 1.0) < 1e-12:
        return _f(q)

    def f(k: float) -> float:
        return float(np.sum(q**k) - 1.0)

    lo, hi = 1e-3, 1.0
    while f(hi) > 0:  # need f(hi) < 0
        hi *= 2.0
        if hi > 1e4:
            raise ValueError("power de-vig: could not bracket k")
    while f(lo) < 0:
        lo /= 2.0
        if lo < 1e-9:
            raise ValueError("power de-vig: could not bracket k")
    k = brentq(f, lo, hi, xtol=1e-14, maxiter=500)
    p = q**k
    return _f(p / p.sum())  # remove residual solver error


def _shin_probs(q: np.ndarray, z: float) -> np.ndarray:
    b = q.sum()
    return (np.sqrt(z * z + 4.0 * (1.0 - z) * q * q / b) - z) / (2.0 * (1.0 - z))


def devig_shin(odds: Sequence[float], return_z: bool = False) -> list[float] | tuple[list[float], float]:
    """Shin (1993) method assuming a fraction ``z`` of insider money.

    ``p_i(z) = (sqrt(z^2 + 4(1-z) q_i^2 / B) - z) / (2(1-z))`` with ``B = sum(q)``.
    ``z`` is solved iteratively so that ``sum p_i(z) = 1``: first the
    Jullien-Salanie fixed-point iteration (n >= 3), falling back to Brent
    root-finding on ``(0, 1)`` (always used for 2-way markets where the
    fixed-point formula is undefined). ``sum p_i(z)`` is monotone decreasing from
    ``sqrt(B) > 1`` at z=0 to ``sum(q^2)/B < 1`` as z->1, so the root is unique.
    """
    q = 1.0 / _validate_odds(odds)
    b = q.sum()
    if b <= 1.0 + 1e-12:  # no margin (or an under-round exchange book)
        p = q / b
        return (_f(p), 0.0) if return_z else _f(p)

    n = q.size
    z: float | None = None
    if n >= 3:
        zi = 0.0
        for _ in range(1000):
            z_new = (np.sum(np.sqrt(zi * zi + 4.0 * (1.0 - zi) * q * q / b)) - 2.0) / (n - 2)
            if not (0.0 <= z_new < 1.0):
                break
            if abs(z_new - zi) < 1e-13:
                z = float(z_new)
                break
            zi = float(z_new)
    if z is None:
        z = float(brentq(lambda zz: float(_shin_probs(q, zz).sum() - 1.0), 0.0, 1.0 - 1e-12, xtol=1e-14))
    p = _shin_probs(q, z)
    p = p / p.sum()
    return (_f(p), z) if return_z else _f(p)


_DEVIG = {
    "multiplicative": devig_multiplicative,
    "additive": devig_additive,
    "power": devig_power,
    "shin": devig_shin,
}


def devig(odds: Sequence[float], method: str = "power") -> list[float]:
    """De-vig with the named method (``multiplicative|additive|power|shin``)."""
    try:
        fn = _DEVIG[method.lower()]
    except KeyError as exc:
        raise ValueError(f"unknown de-vig method {method!r}") from exc
    result = fn(odds)
    return result if isinstance(result, list) else result[0]  # type: ignore[return-value]


def expected_value(p_true: float, odds: float) -> float:
    """EV per unit stake of a binary bet: ``p * odds - 1``."""
    return p_true * odds - 1.0


def fair_odds(p_true: float) -> float:
    """Zero-margin odds ``1 / p``."""
    if p_true <= 0:
        return math.inf
    return 1.0 / p_true


def min_acceptable_odds(p_true: float, min_edge: float = 0.015) -> float:
    """Lowest odds still giving ``EV >= min_edge``: ``(1 + min_edge) / p``."""
    if p_true <= 0:
        return math.inf
    return (1.0 + min_edge) / p_true


def kelly_fraction(
    p_true: float, odds: float, multiplier: float = 0.25, cap: float | None = None
) -> float:
    """Fractional Kelly stake as a fraction of bankroll.

    ``f = multiplier * ((o - 1) p - (1 - p)) / (o - 1)``, clamped at 0 and
    optionally capped at ``cap``.
    """
    if odds <= 1.0:
        return 0.0
    b = odds - 1.0
    f = multiplier * ((b * p_true - (1.0 - p_true)) / b)
    f = max(0.0, f)
    if cap is not None:
        f = min(f, cap)
    return f


# ---------------------------------------------------------------------------
# Asian line settlement
# ---------------------------------------------------------------------------
def split_asian_line(line: float) -> tuple[float, ...]:
    """Split a line into its component half-stakes.

    Whole (2.0) and half (2.5) lines return one line; quarter lines return two,
    e.g. ``2.25 -> (2.0, 2.5)`` and ``-0.75 -> (-1.0, -0.5)``.
    """
    q = round(line * 4)
    if abs(line * 4 - q) > 1e-9:
        raise ValueError(f"line must be a multiple of 0.25, got {line}")
    if q % 2 == 0:  # whole or half line
        return (q / 4.0,)
    return ((q - 1) / 4.0, (q + 1) / 4.0)


@dataclass(frozen=True, slots=True)
class SettlementProbs:
    """Probability of each settlement outcome for an Asian-style bet."""

    win: float
    half_win: float
    push: float
    half_loss: float
    loss: float

    @property
    def total(self) -> float:
        """Sum of all outcome probabilities (should be ~1)."""
        return self.win + self.half_win + self.push + self.half_loss + self.loss

    @property
    def win_weight(self) -> float:
        """Stake-weighted probability of winning ``odds-1``."""
        return self.win + 0.5 * self.half_win

    @property
    def loss_weight(self) -> float:
        """Stake-weighted probability of losing the stake."""
        return self.loss + 0.5 * self.half_loss

    def expected_value(self, odds: float) -> float:
        """Exact EV per unit stake: ``(o-1)(W + HW/2) - (L + HL/2)``."""
        return (odds - 1.0) * self.win_weight - self.loss_weight

    def fair_odds(self) -> float:
        """Odds with EV = 0: ``1 + (L + HL/2) / (W + HW/2)``."""
        if self.win_weight <= _EPS:
            return math.inf
        return 1.0 + self.loss_weight / self.win_weight

    def min_acceptable_odds(self, min_edge: float = 0.015) -> float:
        """Odds with EV = ``min_edge``: ``1 + (L + HL/2 + edge) / (W + HW/2)``."""
        if self.win_weight <= _EPS:
            return math.inf
        return 1.0 + (self.loss_weight + min_edge) / self.win_weight

    def effective_probability(self, odds: float) -> float:
        """``p_eff`` such that ``p_eff * odds - 1`` equals the exact EV."""
        return (self.expected_value(odds) + 1.0) / odds


def asian_settlement(
    values: np.ndarray, probs: np.ndarray, sign: int, offset: float
) -> SettlementProbs:
    """Settlement distribution for a bet whose margin is ``sign * S + offset``.

    ``S`` is an integer random variable (final total, or goal difference) with
    support ``values`` and pmf ``probs``. Each half-stake component of a quarter
    line wins if its margin > 0, pushes if == 0, loses if < 0.

    * Over L  -> ``sign=+1, offset=-L``;  Under L -> ``sign=-1, offset=+L``.
    * Home handicap h -> ``S = home-away``, ``sign=+1, offset=h``.
    * Away handicap (home line h) -> ``sign=-1, offset=-h``.
    """
    values = np.asarray(values, dtype=float)
    probs = np.asarray(probs, dtype=float)
    parts = split_asian_line(offset)
    score = np.zeros_like(values)
    for part in parts:
        m = sign * values + part
        score += np.where(m > 1e-9, 1.0, np.where(m < -1e-9, -1.0, 0.0))
    score /= len(parts)
    win = float(probs[score > 0.75].sum())
    half_win = float(probs[(score > 0.25) & (score <= 0.75)].sum())
    push = float(probs[np.abs(score) <= 0.25].sum())
    half_loss = float(probs[(score < -0.25) & (score >= -0.75)].sum())
    loss = float(probs[score < -0.75].sum())
    return SettlementProbs(win, half_win, push, half_loss, loss)


def settle_bet(
    market: MarketType,
    selection: Selection,
    line: float | None,
    home_value: int,
    away_value: int,
) -> tuple[str, float]:
    """Settle a single bet given final home/away counts (goals or corners).

    For Asian handicap pass the goals *counted for settlement* (for in-play AH
    this is usually goals scored after bet placement). Returns
    ``(result, return_multiplier_of_stake_excluding_odds)`` where result is one
    of ``win|half_win|push|half_loss|loss`` and the multiplier is the fraction
    of stake that wins (1, 0.5, 0, -0.5, -1).
    """
    if market is MarketType.H2H:
        outcome = (
            Selection.HOME
            if home_value > away_value
            else Selection.AWAY
            if away_value > home_value
            else Selection.DRAW
        )
        return ("win", 1.0) if outcome is selection else ("loss", -1.0)
    if line is None:
        raise ValueError("line required")
    if market is MarketType.ASIAN_HANDICAP:
        s = home_value - away_value
        sign, offset = (1, line) if selection is Selection.HOME else (-1, -line)
    else:
        s = home_value + away_value
        sign, offset = (1, -line) if selection is Selection.OVER else (-1, line)
    sp = asian_settlement(np.array([s]), np.array([1.0]), sign, offset)
    for name, mult in (
        ("win", 1.0),
        ("half_win", 0.5),
        ("push", 0.0),
        ("half_loss", -0.5),
        ("loss", -1.0),
    ):
        if getattr(sp, name) > 0.5:
            return name, mult
    raise AssertionError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# Steam detection
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SteamEvent:
    """A detected sharp move on one selection."""

    key: tuple[MarketKey, Selection]
    p_from: float
    p_to: float
    window_s: float
    detected_at: float

    @property
    def abs_change(self) -> float:
        """Absolute change in probability (percentage points / 100)."""
        return self.p_to - self.p_from

    @property
    def rel_change(self) -> float:
        """Relative change in probability."""
        return (self.p_to - self.p_from) / self.p_from if self.p_from > 0 else 0.0


class SteamDetector:
    """Rolling-window detector of sharp probability *increases* (odds shortening).

    For every ``(market, selection)`` the detector keeps the sharp no-vig
    probability observed in the last ``window_s`` seconds. A steam move is
    flagged when the latest probability exceeds the window *minimum* by

    * ``mode="relative"``: ``(p_now - p_min) / p_min >= rel_threshold``
      (default 5%: e.g. 0.40 -> 0.42), or
    * ``mode="absolute"``: ``p_now - p_min >= abs_threshold_pp``
      (default 0.03 = 3 percentage points), or
    * ``mode="either"``: whichever triggers first.

    Relative thresholds are more sensitive on long shots, absolute thresholds on
    favourites; ``either`` is the default. A cooldown stops repeated alerts for
    the same move. Odds *drifting* (probability falling) is not steam on that
    selection; it is steam on the opposite side and is caught there.
    """

    def __init__(
        self,
        window_s: float = 60.0,
        mode: str = "either",
        rel_threshold: float = 0.05,
        abs_threshold_pp: float = 0.03,
        cooldown_s: float = 120.0,
    ) -> None:
        if mode not in {"relative", "absolute", "either"}:
            raise ValueError("mode must be relative|absolute|either")
        self.window_s = window_s
        self.mode = mode
        self.rel_threshold = rel_threshold
        self.abs_threshold_pp = abs_threshold_pp
        self.cooldown_s = cooldown_s
        self._hist: dict[tuple[MarketKey, Selection], deque[tuple[float, float]]] = {}
        self._last_fire: dict[tuple[MarketKey, Selection], float] = {}
        self._active: dict[tuple[MarketKey, Selection], SteamEvent] = {}

    def update(
        self, key: tuple[MarketKey, Selection], p: float, ts: float | None = None
    ) -> SteamEvent | None:
        """Record a new observation and return a :class:`SteamEvent` if triggered."""
        now = time.time() if ts is None else ts
        dq = self._hist.setdefault(key, deque())
        dq.append((now, p))
        while dq and now - dq[0][0] > self.window_s:
            dq.popleft()
        p_min = min(v for _, v in dq)
        rel = (p - p_min) / p_min if p_min > 0 else 0.0
        absd = p - p_min
        hit_rel = rel >= self.rel_threshold
        hit_abs = absd >= self.abs_threshold_pp
        hit = {"relative": hit_rel, "absolute": hit_abs, "either": hit_rel or hit_abs}[self.mode]
        if not hit:
            return None
        if now - self._last_fire.get(key, -math.inf) < self.cooldown_s:
            return None
        self._last_fire[key] = now
        ev = SteamEvent(key, p_min, p, self.window_s, now)
        self._active[key] = ev
        return ev

    def active(self, key: tuple[MarketKey, Selection], ts: float | None = None) -> SteamEvent | None:
        """Steam event on ``key`` detected within the last window, if any."""
        now = time.time() if ts is None else ts
        ev = self._active.get(key)
        if ev and now - ev.detected_at <= self.window_s:
            return ev
        return None


# ---------------------------------------------------------------------------
# Engine B: live football model
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PreMatchRatings:
    """Pre-match expectations for a full 90 minutes.

    ``goal_supremacy`` = expected home goals - expected away goals,
    ``goal_total`` = expected total goals (same for corners).
    """

    goal_supremacy: float
    goal_total: float
    corner_supremacy: float
    corner_total: float
    # De-vigged pre-match 1X2 win probabilities from the market prior (DraftKings / Pinnacle)
    # when available; ``None`` -> derived from the goal ratings (see :func:`pre_match_win_probs`).
    home_win_prob: float | None = None
    away_win_prob: float | None = None

    @property
    def home_goals(self) -> float:
        """Expected home goals over 90'."""
        return max(0.02, (self.goal_total + self.goal_supremacy) / 2.0)

    @property
    def away_goals(self) -> float:
        """Expected away goals over 90'."""
        return max(0.02, (self.goal_total - self.goal_supremacy) / 2.0)

    @property
    def home_corners(self) -> float:
        """Expected home corners over 90'."""
        return max(0.1, (self.corner_total + self.corner_supremacy) / 2.0)

    @property
    def away_corners(self) -> float:
        """Expected away corners over 90'."""
        return max(0.1, (self.corner_total - self.corner_supremacy) / 2.0)


@dataclass(frozen=True, slots=True)
class LiveModelParams:
    """Engine B parameters. **Defaults are priors, calibrate on historical data.**"""

    # Time profile: intensity w(u) = 1 + slope*(u/90 - 0.5), mean 1 over 0..90.
    goal_time_slope: float = 0.30
    corner_time_slope: float = 0.15
    # Stoppage time estimate (minutes).
    first_half_stoppage: float = 2.0
    second_half_stoppage_base: float = 3.5
    stoppage_per_goal: float = 0.4
    stoppage_per_red: float = 0.5
    max_stoppage: float = 12.0
    # Bivariate Poisson correlation: lambda3 = rho * min(lambda_h, lambda_a).
    goal_correlation: float = 0.05
    # Negative-binomial size r for total corners (var = mu + mu^2 / r).
    corner_dispersion: float = 14.0
    # Favourite urgency after minute X.
    favourite_supremacy_threshold: float = 0.6
    urgency_minute: float = 60.0
    fav_trailing_goal_mult: float = 1.25
    fav_tied_goal_mult: float = 1.10
    fav_trailing_corner_mult: float = 1.40
    fav_tied_corner_mult: float = 1.20
    underdog_goal_mult_when_chased: float = 1.05
    underdog_corner_mult_when_chased: float = 0.90
    # Red cards (per net man advantage).
    red_card_attack_mult: float = 0.70
    red_card_opponent_goal_mult: float = 1.25
    red_card_corner_mult: float = 0.75
    red_card_opponent_corner_mult: float = 1.15
    # Momentum from rolling 10-minute dangerous attacks / shots on target.
    expected_da_per10_total: float = 9.0
    expected_sot_per10_total: float = 0.9
    momentum_weight: float = 0.35
    momentum_min: float = 0.85
    momentum_max: float = 1.20
    momentum_horizon_min: float = 15.0
    # SofaScore/FotMob attack-momentum index m in [-1, 1] -> pressure ratio 1 +/- scale*m.
    momentum_graph_scale: float = 1.0
    # In-play Asian handicap settles on goals after bet placement (Bet365 rule; verify).
    ah_from_current_score: bool = True
    # Grids.
    max_goals: int = 15
    max_corners: int = 40
    # --- v3 rules (25 Oct 2026 review). ``LiveModelParams.legacy()`` switches them all off. ---
    # Empirical-Bayes shrinkage of the remaining per-team rate towards the pre-match prior:
    # w = (elapsed/90) * shrinkage_max_weight; rate = w * observed_pace + (1 - w) * prior.
    shrinkage_enabled: bool = True
    shrinkage_max_weight: float = 0.5
    # Comfortable-lead dampener: |goal diff| >= N -> remaining goals x0.82, leader corners x0.85.
    game_state_dampener: bool = True
    dampener_goal_diff: int = 2
    dampener_goal_mult: float = 0.82
    dampener_leader_corner_mult: float = 0.85
    # Favourite definition for fav_chasing: "win_prob" (pre-match win prob >= fav_min_win_prob,
    # tied or trailing by <= fav_chasing_max_deficit after fav_chasing_min_minute) or the
    # legacy "supremacy" rule (|supremacy| >= favourite_supremacy_threshold, any deficit, >= urgency_minute).
    fav_rule: str = "win_prob"
    fav_min_win_prob: float = 0.55
    fav_chasing_min_minute: float = 55.0
    fav_chasing_max_deficit: int = 1
    # Chasing-corner penalty: favourite trailing by exactly 1 in [55', 82'] -> corner Over lines
    # more than ``chasing_corner_line_margin`` above (current corners + expected remaining
    # corners) are priced with only ``chasing_corner_boost_keep`` of the chasing corner boost.
    chasing_corner_penalty: bool = True
    chasing_corner_window: tuple[float, float] = (55.0, 82.0)
    chasing_corner_line_margin: float = 1.5
    chasing_corner_boost_keep: float = 0.5
    # --- v4 rules (25 Oct 2026, second review). Selected via ``rules_preset("v4")``. ---
    # "linear" (v3): w = elapsed/90 * shrinkage_max_weight on goals AND corners.
    # "corners_gamma" (v4): goals = prior only (no observed goal pace); corners Gamma-Poisson
    #   w = elapsed / (elapsed + corner_prior_minutes).
    shrinkage_mode: str = "linear"
    corner_prior_minutes: float = 180.0
    # Corner dampener scope at |diff| >= dampener_goal_diff: "leader" (v3) or "both" (v4).
    dampener_corner_scope: str = "leader"
    # Bound on the per-team live activity/momentum multiplier (v4: (0.85, 1.25)); None = unbounded.
    momentum_clip: tuple[float, float] | None = None
    # Benchmark only (BSS / log-loss skill): time-decayed pre-match prior from the current state,
    # no in-play multipliers and no shrinkage. Never used for alerts.
    prior_only: bool = False
    # Chasing corners: "margin" (v3 penalty on lines above expectation + margin) or "cap" (v4:
    # favourite trailing by exactly 1 from chasing_corner_cap_minute -> chasing corner boost
    # capped at chasing_corner_cap for every corner Over unless FotMob shots-inside-box dominance).
    chasing_corner_mode: str = "margin"
    chasing_corner_cap: float = 1.03
    chasing_corner_cap_minute: float = 60.0
    # --- v4.2 (25 Oct 2026, third review; see config/calibration_report.json "v4_2"). ---
    # Corner time profile: None = linear ``corner_time_slope``; a float R = piecewise-constant per-minute rate
    # with H2/H1 = R (weights 2/(1+R) and 2R/(1+R), mean 1 over 90', i.e. the total prior is unchanged).
    # Used by the model AND the prior benchmark (remaining_intensity is shared).
    corner_half_ratio: float | None = None
    # shrinkage_mode "corners_gamma_half" (v4.2): Gamma-Poisson like v4, but the observed corner pace is
    # measured against the half-adjusted *expected* corners so far (effective elapsed = integral of the
    # corner profile over the played clock), and before ``corner_down_shrink_minute`` the shrunk rate is
    # clamped at >= prior (shrinkage may only raise lambda before 55').
    corner_down_shrink_minute: float = 55.0

    @classmethod
    def legacy(cls, **kw: Any) -> "LiveModelParams":
        """Pre-v3 behaviour (used for the Old-Rules replay)."""
        base = dict(shrinkage_enabled=False, game_state_dampener=False, fav_rule="supremacy",
                    chasing_corner_penalty=False)
        base.update(kw)
        return cls(**base)


def pre_match_win_probs(r: PreMatchRatings, rho: float = 0.05, n: int = 12) -> tuple[float, float]:
    """(home, away) pre-match win probability: market prior if present, else from the goal ratings."""
    if r.home_win_prob is not None and r.away_win_prob is not None:
        return float(r.home_win_prob), float(r.away_win_prob)
    m = bivariate_poisson_matrix(r.home_goals, r.away_goals, rho, n)
    return float(np.tril(m, -1).sum()), float(np.triu(m, 1).sum())


def favourite_chasing(state: MatchState, r: PreMatchRatings, p: LiveModelParams) -> tuple[str, int] | None:
    """``(favourite side, favourite goal diff)`` if the fav_chasing condition holds, else ``None``."""
    if p.fav_rule == "supremacy":
        if state.minute < p.urgency_minute:
            return None
        sup = r.goal_supremacy
        if abs(sup) < p.favourite_supremacy_threshold:
            return None
        fav = "home" if sup > 0 else "away"
        diff = (state.home_goals - state.away_goals) * (1 if fav == "home" else -1)
        return (fav, diff) if diff <= 0 else None
    if state.minute < p.fav_chasing_min_minute:
        return None
    ph, pa = pre_match_win_probs(r, p.goal_correlation)
    if max(ph, pa) < p.fav_min_win_prob:
        return None
    fav = "home" if ph >= pa else "away"
    diff = (state.home_goals - state.away_goals) * (1 if fav == "home" else -1)
    return (fav, diff) if -p.fav_chasing_max_deficit <= diff <= 0 else None


def bivariate_poisson_matrix(lam_h: float, lam_a: float, rho: float, n: int) -> np.ndarray:
    """Joint pmf ``P[x, y]`` of a bivariate Poisson with marginals ``lam_h, lam_a``.

    ``X = A + C, Y = B + C`` with ``C ~ Pois(l3)``, ``l3 = rho * min(lam_h, lam_a)``,
    so ``Cov(X, Y) = l3``. The grid is truncated at ``n - 1`` and renormalised.
    """
    lam_h = max(lam_h, 1e-9)
    lam_a = max(lam_a, 1e-9)
    l3 = max(0.0, min(rho, 0.999)) * min(lam_h, lam_a)
    l1, l2 = lam_h - l3, lam_a - l3
    k = np.arange(n)
    pa, pb, pc = poisson.pmf(k, l1), poisson.pmf(k, l2), poisson.pmf(k, l3)
    m = np.zeros((n, n))
    for c in range(n):
        if pc[c] < 1e-15:
            break
        m[c:, c:] += pc[c] * np.outer(pa[: n - c], pb[: n - c])
    return m / m.sum()


def ratings_from_prices(
    p_home: float,
    p_draw: float,
    p_away: float,
    p_over: float | None = None,
    total_line: float = 2.5,
    corner_total: float = 10.0,
    corner_supremacy_per_goal: float = 1.6,
    default_total: float = 2.6,
) -> PreMatchRatings:
    """Back out expected goals (supremacy & total) from no-vig sharp 1X2 (+ O/U).

    Fits independent-Poisson lambdas by least squares on the 1X2 probabilities
    and, if given, ``P(total > total_line)`` (Asian quarter lines are scored on
    their win weight). Corner supremacy is approximated as proportional to goal
    supremacy; override with real corner lines when available.
    """
    grid = np.arange(16)

    def probs(lh: float, la: float) -> tuple[float, float, float, float]:
        m = np.outer(poisson.pmf(grid, lh), poisson.pmf(grid, la))
        hw = float(np.tril(m, -1).sum())
        dr = float(np.trace(m))
        aw = float(np.triu(m, 1).sum())
        tot = np.add.outer(grid, grid).ravel()
        sp = asian_settlement(tot, m.ravel(), 1, -total_line)
        return hw, dr, aw, sp.win_weight / max(sp.win_weight + sp.loss_weight, _EPS)

    def loss(x: np.ndarray) -> float:
        lh, la = np.exp(x)
        hw, dr, aw, ov = probs(lh, la)
        err = (hw - p_home) ** 2 + (dr - p_draw) ** 2 + (aw - p_away) ** 2
        if p_over is not None:
            err += 2.0 * (ov - p_over) ** 2
        else:
            err += 0.05 * ((lh + la) - default_total) ** 2
        return err

    res = minimize(loss, x0=np.log([1.4, 1.1]), method="Nelder-Mead", options={"xatol": 1e-6, "fatol": 1e-12})
    lh, la = np.exp(res.x)
    sup = float(lh - la)
    return PreMatchRatings(sup, float(lh + la), corner_supremacy_per_goal * sup, corner_total)


@dataclass(slots=True)
class LivePrediction:
    """Output of Engine B for one match state: remaining-goal/corner distributions."""

    state: MatchState
    lam_home_goals: float
    lam_away_goals: float
    mu_corners: float
    goal_matrix: np.ndarray  # remaining goals P[home_add, away_add]
    corner_pmf: np.ndarray  # remaining total corners pmf
    params: LiveModelParams
    multipliers: dict[str, float] = field(default_factory=dict)

    def total_goals_distribution(self) -> tuple[np.ndarray, np.ndarray]:
        """(values, pmf) of final total goals."""
        n = self.goal_matrix.shape[0]
        s = np.add.outer(np.arange(n), np.arange(n)).ravel()
        pmf = np.bincount(s, weights=self.goal_matrix.ravel())
        cur = self.state.home_goals + self.state.away_goals
        return np.arange(pmf.size) + cur, pmf

    def goal_difference_distribution(self, from_current_score: bool | None = None) -> tuple[np.ndarray, np.ndarray]:
        """(values, pmf) of (home - away) goals counted for AH settlement."""
        fcs = self.params.ah_from_current_score if from_current_score is None else from_current_score
        n = self.goal_matrix.shape[0]
        d = np.subtract.outer(np.arange(n), np.arange(n)).ravel()
        pmf = np.bincount(d + (n - 1), weights=self.goal_matrix.ravel())
        vals = np.arange(pmf.size) - (n - 1)
        if not fcs:
            vals = vals + (self.state.home_goals - self.state.away_goals)
        return vals, pmf

    def total_corners_distribution(self) -> tuple[np.ndarray, np.ndarray]:
        """(values, pmf) of final total corners."""
        cur = self.state.home_corners + self.state.away_corners
        return np.arange(self.corner_pmf.size) + cur, self.corner_pmf

    def h2h_probabilities(self) -> tuple[float, float, float]:
        """Full-time (home, draw, away) probabilities including the current score."""
        n = self.goal_matrix.shape[0]
        d = np.subtract.outer(np.arange(n), np.arange(n)) + (self.state.home_goals - self.state.away_goals)
        m = self.goal_matrix
        return float(m[d > 0].sum()), float(m[d == 0].sum()), float(m[d < 0].sum())

    def settlement(self, market: MarketType, selection: Selection, line: float) -> SettlementProbs:
        """Settlement distribution for an Over/Under or AH bet at ``line``."""
        if market is MarketType.TOTAL_GOALS:
            vals, pmf = self.total_goals_distribution()
        elif market is MarketType.TOTAL_CORNERS:
            vals, pmf = self.total_corners_distribution()
        elif market is MarketType.ASIAN_HANDICAP:
            vals, pmf = self.goal_difference_distribution()
            sign, off = (1, line) if selection is Selection.HOME else (-1, -line)
            return asian_settlement(vals, pmf, sign, off)
        else:
            raise ValueError(f"settlement not supported for {market}")
        sign, off = (1, -line) if selection is Selection.OVER else (-1, line)
        return asian_settlement(vals, pmf, sign, off)

    def prob_over(self, market: MarketType, line: float) -> float:
        """Plain probability that the final total strictly exceeds ``line``."""
        vals, pmf = (
            self.total_goals_distribution()
            if market is MarketType.TOTAL_GOALS
            else self.total_corners_distribution()
        )
        return float(pmf[vals > line + 1e-9].sum())


class LiveFootballModel:
    """Time-decaying in-play model for goals (bivariate Poisson) and corners (NegBin)."""

    def __init__(self, params: LiveModelParams | None = None) -> None:
        self.p = params or LiveModelParams()

    # -- time ---------------------------------------------------------------
    @staticmethod
    def _w_integral(a: float, b: float, slope: float) -> float:
        """Integral of w(u) = 1 + slope*(u/90 - 0.5) over [a, b] (0 <= a <= b <= 90)."""

        def big_w(u: float) -> float:
            return u + slope * (u * u / 180.0 - u / 2.0)

        return big_w(b) - big_w(a)

    def stoppage_estimate(self, state: MatchState) -> tuple[float, float]:
        """(first-half, second-half) stoppage minutes estimate."""
        p = self.p
        st1 = p.first_half_stoppage
        if state.period == 1 and state.announced_stoppage is not None and state.minute >= 45:
            st1 = state.announced_stoppage
        st2 = (
            p.second_half_stoppage_base
            + p.stoppage_per_goal * (state.home_goals + state.away_goals)
            + p.stoppage_per_red * (state.home_red_cards + state.away_red_cards)
        )
        if state.period == 2 and state.announced_stoppage is not None and state.minute >= 89:
            st2 = state.announced_stoppage
        return st1, min(st2, p.max_stoppage)

    @staticmethod
    def _half_weights(ratio: float) -> tuple[float, float]:
        return 2.0 / (1.0 + ratio), 2.0 * ratio / (1.0 + ratio)

    def played_corner_minutes(self, state: MatchState) -> float:
        """Profile-weighted clock minutes played (stoppage ignored): expected share of corners so far x 90."""
        p = self.p
        t = min(90.0, max(0.0, float(state.minute)))
        if getattr(state, "period", 1) >= 2:
            t = max(t, 45.0)
        if p.corner_half_ratio is None:
            return self._w_integral(0.0, t, p.corner_time_slope)
        w1, w2 = self._half_weights(p.corner_half_ratio)
        return w1 * min(t, 45.0) + w2 * max(0.0, t - 45.0)

    def remaining_intensity(self, state: MatchState, slope: float, half_ratio: float | None = None) -> float:
        if half_ratio is not None:
            return self._remaining_halves(state, half_ratio)
        return self._remaining_slope(state, slope)

    def _remaining_halves(self, state: MatchState, ratio: float) -> float:
        """Piecewise-constant version of :meth:`remaining_intensity` (H1 weight w1, H2 weight w2)."""
        st1, st2 = self.stoppage_estimate(state)
        w1, w2 = self._half_weights(ratio)
        t = max(0.0, state.minute)
        total = 0.0
        if state.period <= 1:
            total += (max(0.0, 45.0 - t) + st1) * w1 if t < 45 else max(0.0, 45.0 + st1 - t) * w1
            total += (45.0 + st2) * w2
        else:
            t = max(t, 45.0)
            total += (max(0.0, 90.0 - t) + st2) * w2 if t < 90 else max(0.0, 90.0 + st2 - t) * w2
        return total / 90.0

    def _remaining_slope(self, state: MatchState, slope: float) -> float:
        """Remaining expected fraction of a 90' match, weighted by the time profile.

        Returns ``(1/90) * integral of w(u) du`` over the remaining play clock,
        with stoppage minutes weighted at the half-end intensity. Equals 1.0 at
        kick-off without stoppage time.
        """
        st1, st2 = self.stoppage_estimate(state)
        t = max(0.0, state.minute)
        w45 = 1.0 + slope * (45 / 90 - 0.5)
        w90 = 1.0 + slope * 0.5
        total = 0.0
        if state.period <= 1:
            if t < 45:
                total += self._w_integral(t, 45.0, slope) + st1 * w45
            else:
                total += max(0.0, 45.0 + st1 - t) * w45
            total += self._w_integral(45.0, 90.0, slope) + st2 * w90
        else:
            t = max(t, 45.0)
            if t < 90:
                total += self._w_integral(t, 90.0, slope) + st2 * w90
            else:
                total += max(0.0, 90.0 + st2 - t) * w90
        return total / 90.0

    # -- multipliers --------------------------------------------------------
    def _momentum(self, state: MatchState, share_home: float) -> tuple[float, float]:
        p = self.p
        mh0, ma0 = getattr(state, "momentum_mult_home", None), getattr(state, "momentum_mult_away", None)
        if mh0 is not None and ma0 is not None:
            # Activity multipliers from the DeltaTracker are the single source of live pressure:
            # no SOT-ratio / momentum-index terms on top (avoids double counting).
            return float(mh0), float(ma0)
        out = []
        for side, share in (("home", share_home), ("away", 1.0 - share_home)):
            da = getattr(state, f"{side}_dangerous_attacks_10")
            sot = getattr(state, f"{side}_shots_on_target_10")
            ratios: list[float] = []
            if da is not None:
                ratios.append(da / max(p.expected_da_per10_total * share, 1e-6))
            if sot is not None:
                ratios.append(sot / max(p.expected_sot_per10_total * share, 1e-6))
            am = getattr(state, "attack_momentum", None)
            if am is not None:
                am = max(-1.0, min(1.0, float(am)))
                ratios.append(max(0.0, 1.0 + p.momentum_graph_scale * (am if side == "home" else -am)))
            if not ratios:
                out.append(1.0)
                continue
            idx = float(np.mean(ratios))
            m = 1.0 + p.momentum_weight * (idx - 1.0)
            out.append(min(p.momentum_max, max(p.momentum_min, m)))
        return out[0], out[1]

    def multipliers(self, state: MatchState, r: PreMatchRatings, chase_corner_keep: float = 1.0,
                    chase_corner_cap: float | None = None) -> dict[str, float]:
        """Rate multipliers (home/away x goals/corners) for the current state.

        ``chase_corner_keep`` < 1 keeps only that share of the favourite's chasing
        corner boost (used by the chasing-corner penalty for high corner Over lines).
        """
        p = self.p
        m = {"home_goals": 1.0, "away_goals": 1.0, "home_corners": 1.0, "away_corners": 1.0}
        if p.prior_only:
            m["momentum_home"] = m["momentum_away"] = 1.0
            return m
        # Favourite urgency (fav_chasing): see favourite_chasing() for the two rule sets.
        fc = favourite_chasing(state, r, p)
        if fc is not None:
            fav, diff = fc
            dog = "away" if fav == "home" else "home"
            gm = p.fav_trailing_goal_mult if diff < 0 else p.fav_tied_goal_mult
            cm = p.fav_trailing_corner_mult if diff < 0 else p.fav_tied_corner_mult
            cm = 1.0 + (cm - 1.0) * chase_corner_keep
            if chase_corner_cap is not None:
                cm = min(cm, chase_corner_cap)
            m[f"{fav}_goals"] *= gm
            m[f"{fav}_corners"] *= cm
            m[f"{dog}_goals"] *= p.underdog_goal_mult_when_chased
            m[f"{dog}_corners"] *= p.underdog_corner_mult_when_chased
        # Comfortable-lead dampener (|diff| >= 2): tempo drops, leader conserves possession.
        gd = state.home_goals - state.away_goals
        if p.game_state_dampener and abs(gd) >= p.dampener_goal_diff:
            lead = "home" if gd > 0 else "away"
            m["home_goals"] *= p.dampener_goal_mult
            m["away_goals"] *= p.dampener_goal_mult
            if p.dampener_corner_scope == "both":
                m["home_corners"] *= p.dampener_leader_corner_mult
                m["away_corners"] *= p.dampener_leader_corner_mult
            else:
                m[f"{lead}_corners"] *= p.dampener_leader_corner_mult
        # Red cards: apply per net man advantage.
        net = state.home_red_cards - state.away_red_cards
        if net != 0:
            short, full = ("home", "away") if net > 0 else ("away", "home")
            k = abs(net)
            m[f"{short}_goals"] *= p.red_card_attack_mult**k
            m[f"{full}_goals"] *= p.red_card_opponent_goal_mult**k
            m[f"{short}_corners"] *= p.red_card_corner_mult**k
            m[f"{full}_corners"] *= p.red_card_opponent_corner_mult**k
        # Momentum, decaying over the remaining time (only lasts ~horizon minutes).
        share = r.home_goals / (r.home_goals + r.away_goals)
        mh, ma = self._momentum(state, share)
        if p.momentum_clip is not None:
            lo_c, hi_c = p.momentum_clip
            mh, ma = min(hi_c, max(lo_c, mh)), min(hi_c, max(lo_c, ma))
        remaining_min = max(1.0, self.remaining_intensity(state, 0.0) * 90.0)
        frac = min(1.0, p.momentum_horizon_min / remaining_min)
        mh_eff, ma_eff = 1.0 + (mh - 1.0) * frac, 1.0 + (ma - 1.0) * frac
        for k2 in ("goals", "corners"):
            m[f"home_{k2}"] *= mh_eff
            m[f"away_{k2}"] *= ma_eff
        m["momentum_home"], m["momentum_away"] = mh, ma
        return m

    # -- prediction ---------------------------------------------------------
    def shrunk_rates(self, state: MatchState, r: PreMatchRatings) -> dict[str, float]:
        """Per-90 base rates for the remainder: Empirical-Bayes blend of observed pace and prior.

        ``w = (elapsed / 90) * shrinkage_max_weight`` (0 at kick-off, 0.5 at 90');
        ``rate = w * observed_pace + (1 - w) * prior`` per team, for goals and corners.
        Observed pace = count so far / elapsed * 90. Multipliers are applied afterwards.
        """
        p = self.p
        prior = {"home_goals": r.home_goals, "away_goals": r.away_goals,
                 "home_corners": r.home_corners, "away_corners": r.away_corners}
        if not p.shrinkage_enabled or p.prior_only:
            return prior
        elapsed = min(90.0, max(0.0, float(state.minute)))
        if getattr(state, "period", 1) >= 2:
            elapsed = max(elapsed, 45.0)
        if elapsed < 1.0:
            return prior
        obs_c = {"home_corners": getattr(state, "home_corners", 0) or 0, "away_corners": getattr(state, "away_corners", 0) or 0}
        if p.shrinkage_mode == "corners_gamma_half":
            eff = max(1e-6, self.played_corner_minutes(state))
            wc = eff / (eff + p.corner_prior_minutes)
            out = dict(prior)
            for k in ("home_corners", "away_corners"):
                rate = wc * (obs_c[k] / eff * 90.0) + (1.0 - wc) * prior[k]
                if float(state.minute) < p.corner_down_shrink_minute and getattr(state, "period", 1) <= 2:
                    rate = max(rate, prior[k])
                out[k] = rate
            return out
        if p.shrinkage_mode == "corners_gamma":
            # Goals: prior only - past goals change the scoreline, not the teams' baseline rate.
            # Corners: Gamma-Poisson posterior mean, prior worth ``corner_prior_minutes`` of play.
            wc = elapsed / (elapsed + p.corner_prior_minutes)
            out = dict(prior)
            for k in ("home_corners", "away_corners"):
                out[k] = wc * (obs_c[k] / elapsed * 90.0) + (1.0 - wc) * prior[k]
            return out
        w = (elapsed / 90.0) * p.shrinkage_max_weight
        obs = {"home_goals": state.home_goals, "away_goals": state.away_goals,
               "home_corners": getattr(state, "home_corners", 0) or 0, "away_corners": getattr(state, "away_corners", 0) or 0}
        return {k: w * (obs[k] / elapsed * 90.0) + (1.0 - w) * prior[k] for k in prior}

    def predict(self, state: MatchState, ratings: PreMatchRatings, chase_corner_keep: float = 1.0,
                chase_corner_cap: float | None = None) -> LivePrediction:
        """Price the remainder of the match from ``state`` and pre-match ``ratings``."""
        p = self.p
        mult = self.multipliers(state, ratings, chase_corner_keep, chase_corner_cap)
        g_frac = self.remaining_intensity(state, p.goal_time_slope)
        c_frac = self.remaining_intensity(state, p.corner_time_slope, p.corner_half_ratio)
        base = self.shrunk_rates(state, ratings)
        lam_h = base["home_goals"] * g_frac * mult["home_goals"]
        lam_a = base["away_goals"] * g_frac * mult["away_goals"]
        mu_c = (
            base["home_corners"] * mult["home_corners"] + base["away_corners"] * mult["away_corners"]
        ) * c_frac
        gm = bivariate_poisson_matrix(lam_h, lam_a, p.goal_correlation, p.max_goals)
        k = np.arange(p.max_corners)
        if mu_c < 1e-9:
            cp = np.zeros(p.max_corners)
            cp[0] = 1.0
        else:
            r = p.corner_dispersion
            cp = nbinom.pmf(k, r, r / (r + mu_c))
            cp = cp / cp.sum()
        return LivePrediction(state, lam_h, lam_a, mu_c, gm, cp, p, mult)


# ---------------------------------------------------------------------------
# Signal engine (Engine A + Engine B orchestration)
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class EngineConfig:
    """Parameters of :class:`SignalEngine` (mirrors :class:`~quantbet.config.Settings`)."""

    ev_threshold: float = 0.025
    min_edge_for_floor: float = 0.015
    devig_method: str = "power"
    target_bookmaker: str = "bet365"
    sharp_bookmakers: tuple[str, ...] = ("pinnacle", "betfair_ex_eu", "betfair exchange")
    max_sharp_age_s: float = 30.0
    max_target_age_s: float = 10.0
    kelly_multiplier: float = 0.25
    engine_b_mode: str = "fallback"  # fallback | always | off
    max_odds: float = 15.0
    realert_cooldown_s: float = 300.0
    steam_window_s: float = 60.0
    steam_mode: str = "either"
    steam_rel_threshold: float = 0.05
    steam_abs_threshold_pp: float = 0.03
    steam_cooldown_s: float = 120.0
    default_corner_total: float = 10.0


class SignalEngine:
    """Keeps the latest order books and turns updates into :class:`Signal` objects."""

    def __init__(
        self, config: EngineConfig | None = None, live_params: LiveModelParams | None = None
    ) -> None:
        self.cfg = config or EngineConfig()
        self.model = LiveFootballModel(live_params)
        self.steam = SteamDetector(
            self.cfg.steam_window_s,
            self.cfg.steam_mode,
            self.cfg.steam_rel_threshold,
            self.cfg.steam_abs_threshold_pp,
            self.cfg.steam_cooldown_s,
        )
        # market -> bookmaker -> selection -> snapshot
        self.books: dict[MarketKey, dict[str, dict[Selection, OddsSnapshot]]] = {}
        self.events: dict[str, EventInfo] = {}
        self.match_states: dict[str, MatchState] = {}
        self.ratings: dict[str, PreMatchRatings] = {}
        self._last_alert: dict[tuple[MarketKey, Selection, str], tuple[float, float]] = {}
        # Last pre-match sharp no-vig odds per selection (= closing line at kick-off).
        self.prematch_fair: dict[tuple[MarketKey, Selection], tuple[float, float]] = {}
        self._sharp = tuple(b.lower() for b in self.cfg.sharp_bookmakers)
        self._target = self.cfg.target_bookmaker.lower()

    # -- state ingestion ----------------------------------------------------
    def update_match_state(self, state: MatchState) -> None:
        """Store the latest live statistics for an event."""
        self.match_states[state.event_key] = state

    def set_ratings(self, event_key: str, ratings: PreMatchRatings) -> None:
        """Override pre-match ratings (e.g. from your own rating model)."""
        self.ratings[event_key] = ratings

    def _ingest(self, snap: OddsSnapshot) -> MarketKey:
        mk = snap.market_key()
        book = self.books.setdefault(mk, {}).setdefault(snap.bookmaker.lower(), {})
        book[snap.selection] = snap
        ev = self.events.get(snap.event_key)
        if ev is None:
            self.events[snap.event_key] = EventInfo(
                snap.event_key, snap.home, snap.away, snap.league, snap.commence_time, snap.is_live
            )
        else:
            ev.is_live = ev.is_live or snap.is_live
        return mk

    # -- helpers ------------------------------------------------------------
    def _complete_book(
        self, mk: MarketKey, bookmaker: str, max_age: float, now: float
    ) -> dict[Selection, OddsSnapshot] | None:
        book = self.books.get(mk, {}).get(bookmaker)
        if not book:
            return None
        sels = mk.selections()
        if not all(s in book for s in sels):
            return None
        if any(now - book[s].timestamp > max_age for s in sels):
            return None
        return book

    def sharp_fair_probabilities(
        self, mk: MarketKey, now: float | None = None
    ) -> tuple[dict[Selection, float], str] | None:
        """No-vig probabilities from the first fresh, complete sharp book."""
        now = time.time() if now is None else now
        for bm in self._sharp:
            book = self._complete_book(mk, bm, self.cfg.max_sharp_age_s, now)
            if book is None:
                continue
            sels = mk.selections()
            try:
                probs = devig([book[s].price for s in sels], self.cfg.devig_method)
            except ValueError:
                continue
            return dict(zip(sels, probs, strict=True)), bm
        return None

    def _maybe_fit_ratings(self, event_key: str, now: float) -> None:
        ev = self.events.get(event_key)
        if ev is None or ev.is_live:
            return  # freeze ratings at the last pre-match estimate
        fair = self.sharp_fair_probabilities(MarketKey(event_key, MarketType.H2H), now)
        if fair is None:
            return
        probs, _ = fair
        p_over, line = None, 2.5
        for cand in (2.5, 2.75, 2.25, 3.0, 2.0, 3.25, 3.5):
            tot = self.sharp_fair_probabilities(MarketKey(event_key, MarketType.TOTAL_GOALS, cand), now)
            if tot:
                p_over, line = tot[0][Selection.OVER], cand
                break
        corner_total = self.cfg.default_corner_total
        for mk in self.books:
            if mk.event_key == event_key and mk.market is MarketType.TOTAL_CORNERS and mk.line is not None:
                ct = self.sharp_fair_probabilities(mk, now)
                if ct and 0.4 <= ct[0][Selection.OVER] <= 0.6:
                    corner_total = mk.line + 0.5 * (ct[0][Selection.OVER] - 0.5) * 4
                    break
        self.ratings[event_key] = ratings_from_prices(
            probs[Selection.HOME], probs[Selection.DRAW], probs[Selection.AWAY], p_over, line, corner_total
        )

    def _dedupe(self, mk: MarketKey, sel: Selection, bm: str, odds: float, now: float) -> bool:
        """True if this alert should be suppressed as a repeat."""
        key = (mk, sel, bm)
        last = self._last_alert.get(key)
        if last and now - last[0] < self.cfg.realert_cooldown_s and odds <= last[1] + 1e-9:
            return True
        self._last_alert[key] = (now, odds)
        return False

    def _build_signal(
        self,
        urgency: Urgency,
        engine: str,
        snap: OddsSnapshot,
        p_eff: float,
        ev: float,
        fair: float,
        floor: float,
        method: str,
        sharp_source: str | None,
        extra: dict[str, object],
    ) -> Signal:
        ms = self.match_states.get(snap.event_key)
        ev_info = self.events.get(snap.event_key)
        return Signal(
            urgency=urgency,
            engine=engine,
            event_key=snap.event_key,
            home=ev_info.home if ev_info else snap.home,
            away=ev_info.away if ev_info else snap.away,
            league=snap.league,
            market=snap.market,
            line=snap.line,
            selection=snap.selection,
            bookmaker=snap.bookmaker,
            bookmaker_odds=snap.price,
            p_true=p_eff,
            fair_odds=fair,
            min_acceptable_odds=floor,
            ev=ev,
            kelly_fraction=kelly_fraction(p_eff, snap.price, self.cfg.kelly_multiplier),
            is_live=snap.is_live,
            minute=ms.minute if ms and snap.is_live else None,
            score=ms.score if ms and snap.is_live else None,
            commence_time=snap.commence_time,
            method=method,
            sharp_source=sharp_source,
            extra=dict(extra),
        )

    # -- evaluation ---------------------------------------------------------
    def evaluate_market(self, mk: MarketKey, now: float | None = None) -> list[Signal]:
        """Run Engine A (and Engine B if applicable) on one market."""
        now = time.time() if now is None else now
        signals: list[Signal] = []
        target = self.books.get(mk, {}).get(self._target, {})
        fair = self.sharp_fair_probabilities(mk, now)
        steam_hits: dict[Selection, SteamEvent] = {}
        if fair is not None:
            probs, src = fair
            ev_info = self.events.get(mk.event_key)
            prematch = not (ev_info and ev_info.is_live) and not (
                ev_info and ev_info.commence_time and ev_info.commence_time.timestamp() <= now
            )
            for sel, p in probs.items():
                if prematch and p > 0:
                    self.prematch_fair[(mk, sel)] = (now, 1.0 / p)
                hit = self.steam.update((mk, sel), p, now)
                if hit is not None:
                    log.info("steam detected", extra={"market": str(mk), "sel": sel.value, "from": hit.p_from, "to": hit.p_to})
                active = self.steam.active((mk, sel), now)
                if active:
                    steam_hits[sel] = active
            for sel, snap in target.items():
                if now - snap.timestamp > self.cfg.max_target_age_s or snap.price > self.cfg.max_odds:
                    continue
                p = probs.get(sel)
                if p is None or p <= 0:
                    continue
                ev = expected_value(p, snap.price)
                if ev < self.cfg.ev_threshold or self._dedupe(mk, sel, self._target, snap.price, now):
                    continue
                steam = steam_hits.get(sel)
                extra: dict[str, object] = {"overround_sharp": None}
                if steam:
                    extra.update(steam_from=steam.p_from, steam_to=steam.p_to)
                signals.append(
                    self._build_signal(
                        Urgency.STEAM if steam else Urgency.VALUE,
                        "A",
                        snap,
                        p,
                        ev,
                        fair_odds(p),
                        min_acceptable_odds(p, self.cfg.min_edge_for_floor),
                        f"devig:{self.cfg.devig_method}",
                        src,
                        extra,
                    )
                )
        use_b = self.cfg.engine_b_mode == "always" or (
            self.cfg.engine_b_mode == "fallback" and fair is None
        )
        if use_b and mk.market is not MarketType.H2H and mk.line is not None:
            signals.extend(self._engine_b(mk, target, now))
        return signals

    def _engine_b(
        self, mk: MarketKey, target: Mapping[Selection, OddsSnapshot], now: float
    ) -> list[Signal]:
        state = self.match_states.get(mk.event_key)
        ratings = self.ratings.get(mk.event_key)
        if state is None or ratings is None or mk.line is None:
            return []
        pred = self.model.predict(state, ratings)
        out: list[Signal] = []
        for sel, snap in target.items():
            if not snap.is_live or now - snap.timestamp > self.cfg.max_target_age_s:
                continue
            sp = pred.settlement(mk.market, sel, mk.line)
            ev = sp.expected_value(snap.price)
            if ev < self.cfg.ev_threshold or snap.price > self.cfg.max_odds:
                continue
            if self._dedupe(mk, sel, self._target + ":B", snap.price, now):
                continue
            out.append(
                self._build_signal(
                    Urgency.LIVE_MODEL,
                    "B",
                    snap,
                    sp.effective_probability(snap.price),
                    ev,
                    sp.fair_odds(),
                    sp.min_acceptable_odds(self.cfg.min_edge_for_floor),
                    "live:bivariate_poisson" if mk.market is not MarketType.TOTAL_CORNERS else "live:negbin",
                    None,
                    {
                        "lam_home": round(pred.lam_home_goals, 4),
                        "lam_away": round(pred.lam_away_goals, 4),
                        "mu_corners": round(pred.mu_corners, 4),
                        "settlement": [sp.win, sp.half_win, sp.push, sp.half_loss, sp.loss],
                        "multipliers": {k: round(v, 3) for k, v in pred.multipliers.items()},
                    },
                )
            )
        return out

    def process(self, snapshots: Iterable[OddsSnapshot], now: float | None = None) -> list[Signal]:
        """Ingest a batch of snapshots and return any new signals."""
        now = time.time() if now is None else now
        touched: set[MarketKey] = set()
        for s in snapshots:
            if not s.event_key:
                continue
            touched.add(self._ingest(s))
        for ek in {mk.event_key for mk in touched}:
            self._maybe_fit_ratings(ek, now)
        signals: list[Signal] = []
        for mk in touched:
            try:
                signals.extend(self.evaluate_market(mk, now))
            except (ValueError, FloatingPointError) as exc:
                log.warning("evaluation failed", extra={"market": str(mk), "error": str(exc)})
        return signals

    def evaluate_live_event(self, event_key: str, now: float | None = None) -> list[Signal]:
        """Re-run Engine B on all lines of an event after a match-state update."""
        now = time.time() if now is None else now
        out: list[Signal] = []
        for mk in [m for m in self.books if m.event_key == event_key]:
            out.extend(self.evaluate_market(mk, now))
        return out

    def current_fair_odds(self, mk: MarketKey, sel: Selection, now: float | None = None) -> float | None:
        """Current sharp no-vig odds (used as closing line at kick-off)."""
        fair = self.sharp_fair_probabilities(mk, now if now is not None else time.time())
        if fair is None or fair[0].get(sel, 0) <= 0:
            return None
        return 1.0 / fair[0][sel]

    def closing_fair_odds(self, mk: MarketKey, sel: Selection) -> float | None:
        """Last sharp no-vig odds observed before kick-off (the closing line)."""
        v = self.prematch_fair.get((mk, sel))
        return v[1] if v else None

    def current_target_odds(self, mk: MarketKey, sel: Selection) -> float | None:
        """Latest Bet365 (target) price for a selection, regardless of age."""
        snap = self.books.get(mk, {}).get(self._target, {}).get(sel)
        return snap.price if snap else None


# ---------------------------------------------------------------------------
# $0 stack: "minimum acceptable Bet365 odds" reverse pricing (Layer 3)
# ---------------------------------------------------------------------------
StakeSizer = Callable[[float, float], float]
"""``(kelly_fraction_already_multiplied, odds) -> rounded stake in currency``."""


def _ceil2(x: float) -> float:
    """Round odds *up* to 2 decimals so the displayed price still meets the edge."""
    return math.ceil(x * 100.0 - 1e-9) / 100.0


@dataclass(frozen=True, slots=True)
class StakeTier:
    """One row of the stake-tier table: "If Bet365 offers ``odds`` -> bet ``stake``"."""

    odds: float
    stake: float
    ev: float
    kelly: float


@dataclass(slots=True)
class LinePrice:
    """Model price for one selection on one line (goals/corners, incl. quarter lines).

    ``p_true`` is the push-adjusted probability ``(W + HW/2) / (W + HW/2 + L + HL/2)``
    so that ``fair_odds == 1 / p_true`` holds exactly for Asian quarter/whole lines.
    ``min_odds`` is the lowest Bet365 price giving ``target_edge`` EV and
    ``abort_floor`` the lowest price still giving ``abort_edge`` EV.
    """

    market: MarketType
    selection: Selection
    line: float
    settlement: SettlementProbs
    p_true: float
    fair_odds: float
    min_odds: float
    abort_floor: float
    target_edge: float
    abort_edge: float
    tiers: list[StakeTier] = field(default_factory=list)
    notes: tuple[str, ...] = ()

    @property
    def is_asian(self) -> bool:
        """True for whole / quarter lines that can push or half-settle."""
        return abs(self.line * 2 - round(self.line * 2)) > 1e-9 or abs(self.line - round(self.line)) < 1e-9

    def ev_at(self, odds: float) -> float:
        """Exact EV at a given Bet365 price (push/half outcomes included)."""
        return self.settlement.expected_value(odds)

    def describe(self, home: str = "Home", away: str = "Away") -> str:
        """e.g. ``Over 10.25 Corners``."""
        unit = "Goals" if self.market is MarketType.TOTAL_GOALS else "Corners"
        return f"{self.selection.value.capitalize()} {self.line:g} {unit}"


@dataclass(frozen=True, slots=True)
class PricingConfig:
    """Parameters for the min-odds pricer and the model-only alert policy."""

    target_edge: float = 0.025  # EV needed to bet -> MINIMUM BET365 ODDS
    abort_edge: float = 0.015  # EV floor -> abort if price drops below
    kelly_multiplier: float = 0.25
    ladder_offsets: tuple[float, ...] = (0.0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.50, 0.75, 1.00)
    max_tiers: int = 6
    line_step: float = 0.25  # 0.25 -> quarter, half and whole lines
    goal_line_span: float = 4.0  # price lines up to current total + span
    corner_line_span: float = 8.0
    min_fair_odds: float = 1.30  # "sensible range" for priced lines
    max_fair_odds: float = 6.00
    # Model-only alert policy (confidence gates).
    min_minute: float = 10.0
    max_minute: float = 86.0
    max_state_age_s: float = 90.0
    require_stats: bool = True
    allow_default_ratings: bool = False
    alert_fair_odds_range: tuple[float, float] = (1.50, 3.20)
    lines_per_market: int = 2
    momentum_trigger: float = 0.30  # |attack_momentum| needed for a momentum trigger
    activity_trigger_mult: float = 1.04  # momentum_mult_total >= this -> "activity_high" (activity >= 1.5x normal)
    alert_without_trigger: bool = False
    realert_cooldown_s: float = 600.0
    realert_fair_move: float = 0.08  # re-alert inside cooldown if fair odds moved >= 8 %
    max_alerts_per_event_window: int = 4
    event_window_s: float = 600.0
    # --- v3 rules (25 Oct 2026 review). ``PricingConfig.legacy()`` restores the old policy. ---
    # Allowed alert windows (match minutes, inclusive). Applies to every trigger incl. red_card.
    # ``None`` -> legacy [min_minute, max_minute].
    minute_windows: tuple[tuple[float, float], ...] | None = ((30.0, 43.0), (55.0, 85.0))
    # "best_growth": one line per match x family (goals / corners) per poll, the one with the
    # highest expected log-growth at min odds under quarter Kelly (tie-break: EV).
    # "nearest_main": legacy - ``lines_per_market`` lines nearest fair 2.0.
    selection_mode: str = "best_growth"
    # Drop lines whose EV at the neutral reference price is < 0 *before* selection
    # (legacy: done in main after the rate limiter).
    drop_negative_reference_ev: bool = True
    # Alert deduplicator (replaces AlertRateLimiter when use_deduplicator=True).
    use_deduplicator: bool = True
    family_cooldown_min: float = 15.0
    state_change_line_gap: float = 1.0
    max_signals_per_family: int = 1
    max_signals_per_match: int = 2
    max_match_stake_frac: float = 0.05
    bankroll: float = 2000.0
    # Corner Overs after this minute need FotMob dominance confirmation (xG and shots in box
    # of the dominant side > opponent); unavailable -> suppressed. ``None`` disables.
    corner_over_fotmob_after: float | None = 75.0
    # --- v4 ---
    # "fair_target" (v4, reverse pricing): the line with fair odds closest to ``fair_target``
    # within ``fair_target_range`` (none in range -> no alert). With real bookmaker odds passed to
    # ``on_match_state(real_odds=...)`` the highest expected log-growth at those odds is used.
    fair_target: float = 1.95
    fair_target_range: tuple[float, float] = (1.75, 2.20)
    # v3 goal/red-card + line-gap bypass of the family/match caps (v4: False = hard cap).
    state_change_exception: bool = True
    # |goal diff| >= this at any time -> block all Over alerts for the match from then on (None = off).
    # Unders stay allowed in blown-out matches (v4.1).
    blowout_block_diff: int | None = None
    # --- v4.1: positive-lift gate (reverse-pricing mode only, i.e. no real odds for the line) ---
    # Prior = same engine with ``prior_only=True`` (time-decayed pre-match remaining rates from the
    # current score/corner state; no in-play multipliers, no shrinkage). For family f:
    #   net_multiplier = lambda_model_remaining / lambda_prior_remaining
    # (goals: lam_home + lam_away; corners: mu_corners incl. Gamma-Poisson shrinkage and the
    # chasing-corner cap when active). Over: net >= lift_gate AND fair <= prior_fair / lift_gate
    # (net <= lift_suppress_net -> every Over of that family suppressed). Under (if allow_unders):
    # net < 1 AND fair_under <= prior_fair_under / lift_gate. The trigger/direction gate of
    # assess_confidence is replaced by this gate (it is the quantified trigger).
    lift_gate: float | None = None
    lift_suppress_net: float = 1.05
    allow_unders: bool = False

    @classmethod
    def legacy(cls, **kw: Any) -> "PricingConfig":
        """Pre-v3 alert policy (used for the Old-Rules replay)."""
        base = dict(minute_windows=None, selection_mode="nearest_main", drop_negative_reference_ev=False,
                    use_deduplicator=False, corner_over_fotmob_after=None)
        base.update(kw)
        return cls(**base)


def _default_sizer() -> StakeSizer:
    from .risk import RiskManager  # local import: risk imports engine

    rm = RiskManager()
    return lambda f, o: rm.stake_from_kelly(f)


def build_stake_tiers(
    sp: SettlementProbs,
    min_odds: float,
    cfg: PricingConfig,
    sizer: StakeSizer | None = None,
    max_odds: float = 15.0,
) -> list[StakeTier]:
    """Stake ladder from ``min_odds`` upwards: quarter Kelly, rounded, deduped, no zeros.

    Kelly uses the effective probability ``p_eff = (EV + 1) / o`` so half-win /
    half-loss / push outcomes of Asian lines are reflected exactly in the edge.
    Consecutive odds with the same rounded stake are collapsed to the lowest odds.
    """
    size = sizer or _default_sizer()
    tiers: list[StakeTier] = []
    last_stake: float | None = None
    for off in cfg.ladder_offsets:
        o = round(min_odds + off, 2)
        if o > max_odds:
            break
        ev = sp.expected_value(o)
        if ev <= 0:
            continue
        f = kelly_fraction(sp.effective_probability(o), o, cfg.kelly_multiplier)
        stake = float(size(f, o))
        if stake <= 0 or (last_stake is not None and abs(stake - last_stake) < 1e-9):
            continue
        tiers.append(StakeTier(o, stake, ev, f))
        last_stake = stake
        if len(tiers) >= cfg.max_tiers:
            break
    return tiers


def price_line(
    pred: LivePrediction,
    market: MarketType,
    selection: Selection,
    line: float,
    cfg: PricingConfig | None = None,
    sizer: StakeSizer | None = None,
) -> LinePrice | None:
    """Price one selection; ``None`` if it is already settled or has no loss/win mass."""
    cfg = cfg or PricingConfig()
    sp = pred.settlement(market, selection, line)
    if sp.win_weight <= 1e-9 or sp.loss_weight <= 1e-9:
        return None
    fair = sp.fair_odds()
    lp = LinePrice(
        market,
        selection,
        line,
        sp,
        1.0 / fair,
        fair,
        _ceil2(sp.min_acceptable_odds(cfg.target_edge)),
        _ceil2(sp.min_acceptable_odds(cfg.abort_edge)),
        cfg.target_edge,
        cfg.abort_edge,
    )
    lp.tiers = build_stake_tiers(sp, lp.min_odds, cfg, sizer)
    return lp


def candidate_lines(current: int, span: float, step: float) -> list[float]:
    """Lines from ``current + step`` to ``current + span`` (e.g. 2.25, 2.5, 2.75, ...)."""
    n = int(round(span / step))
    return [round(current + step * i, 2) for i in range(1, n + 1)]


def price_live_lines(
    state: MatchState,
    ratings: PreMatchRatings,
    cfg: PricingConfig | None = None,
    model: LiveFootballModel | None = None,
    sizer: StakeSizer | None = None,
    markets: Sequence[MarketType] = (MarketType.TOTAL_GOALS, MarketType.TOTAL_CORNERS),
) -> list[LinePrice]:
    """Fair odds, minimum Bet365 odds, abort floor and stake tiers for all goal & corner lines.

    Covers half, whole and quarter (Asian) lines from just above the current
    count up to ``goal_line_span`` / ``corner_line_span``; only lines whose fair
    odds lie within ``[min_fair_odds, max_fair_odds]`` are returned.
    """
    cfg = cfg or PricingConfig()
    model = model or LiveFootballModel()
    pred = model.predict(state, ratings)
    pen_pred, pen_threshold, pen_note = None, math.inf, "chasing_corner_penalty"
    if model.p.chasing_corner_mode == "cap":
        if chasing_corner_cap_active(state, ratings, model.p):
            # v4: favourite 1 down after 60' -> any corner Over priced with the boost capped at 1.03x.
            pen_pred = model.predict(state, ratings, chase_corner_cap=model.p.chasing_corner_cap)
            pen_threshold, pen_note = -math.inf, "chasing_corner_cap"
    elif chasing_corner_penalty_active(state, ratings, model.p):
        # Favourite trails by exactly 1: one equaliser ends the corner siege. Corner Over lines
        # more than `margin` above (current corners + expected remaining corners) keep only
        # `boost_keep` of the chasing corner boost.
        pen_threshold = state.home_corners + state.away_corners + pred.mu_corners + model.p.chasing_corner_line_margin
        pen_pred = model.predict(state, ratings, chase_corner_keep=model.p.chasing_corner_boost_keep)
    out: list[LinePrice] = []
    for market in markets:
        if market is MarketType.TOTAL_GOALS:
            cur, span = state.home_goals + state.away_goals, cfg.goal_line_span
        elif market is MarketType.TOTAL_CORNERS:
            cur, span = state.home_corners + state.away_corners, cfg.corner_line_span
        else:
            continue
        for line in candidate_lines(cur, span, cfg.line_step):
            for sel in (Selection.OVER, Selection.UNDER):
                use_pen = (pen_pred is not None and market is MarketType.TOTAL_CORNERS
                           and sel is Selection.OVER and line > pen_threshold)
                lp = price_line(pen_pred if use_pen else pred, market, sel, line, cfg, sizer)
                if lp and use_pen:
                    lp.notes = (pen_note,)
                if lp and cfg.min_fair_odds <= lp.fair_odds <= cfg.max_fair_odds:
                    out.append(lp)
    return out


def sib_dominance(state: MatchState, side: str) -> bool | None:
    """FotMob shots-inside-box dominance of ``side`` (None if the data is missing)."""
    h, a = getattr(state, "home_shots_inside_box", None), getattr(state, "away_shots_inside_box", None)
    if h is None or a is None:
        return None
    return (h > a) if side == "home" else (a > h)


def chasing_corner_cap_active(state: MatchState, ratings: PreMatchRatings, p: LiveModelParams) -> bool:
    """v4: favourite trailing by exactly 1 from ``chasing_corner_cap_minute`` and no FotMob SIB dominance."""
    if not p.chasing_corner_penalty or state.minute < p.chasing_corner_cap_minute:
        return False
    fc = favourite_chasing(state, ratings, p)
    if fc is None or fc[1] != -1:
        return False
    return sib_dominance(state, fc[0]) is not True


def chasing_corner_penalty_active(state: MatchState, ratings: PreMatchRatings, p: LiveModelParams) -> bool:
    """True when the chasing-corner penalty applies (favourite trailing by exactly 1 in the window)."""
    if not p.chasing_corner_penalty:
        return False
    lo, hi = p.chasing_corner_window
    if not (lo <= state.minute <= hi):
        return False
    fc = favourite_chasing(state, ratings, p)
    return fc is not None and fc[1] == -1


def expected_log_growth(lp: LinePrice, odds: float | None = None, kelly: float | None = None) -> float:
    """Expected log-growth of bankroll for one bet on ``lp`` at ``odds`` (default: min odds).

    Stake fraction ``f`` = quarter Kelly at those odds (``lp.tiers[0].kelly`` when available).
    ``G = W ln(1 + f b) + HW ln(1 + f b / 2) + HL ln(1 - f / 2) + L ln(1 - f)``, ``b = odds - 1``
    (pushes contribute 0). This is the selection metric for the single-best-line rule.
    """
    o = lp.min_odds if odds is None else odds
    if kelly is None:
        kelly = lp.tiers[0].kelly if lp.tiers else kelly_fraction(lp.settlement.effective_probability(o), o, 0.25)
    f, b, sp = min(max(kelly, 0.0), 0.99), o - 1.0, lp.settlement
    return (sp.win * math.log1p(f * b) + sp.half_win * math.log1p(f * b / 2)
            + sp.half_loss * math.log1p(-f / 2) + sp.loss * math.log1p(-f))


@dataclass(frozen=True, slots=True)
class ConfidenceReport:
    """Result of the model-only alert gate."""

    ok: bool
    reasons: tuple[str, ...]
    triggers: tuple[str, ...]
    direction: dict[MarketType, Selection | None]


def assess_confidence(
    state: MatchState,
    ratings_source: str,
    cfg: PricingConfig | None = None,
    ratings: PreMatchRatings | None = None,
    now: float | None = None,
    params: LiveModelParams | None = None,
) -> ConfidenceReport:
    """Decide whether Engine B is trustworthy enough for a model-only (min-odds) alert.

    Gates: minute window, fresh state, parsed statistics, non-default ratings.
    Triggers (an edge *hypothesis* – a reason Bet365 may lag the model):
    ``momentum`` (|attack momentum| >= threshold), ``fav_chasing`` (pre-match
    favourite level/behind after the urgency minute), ``red_card``. Momentum and
    chasing point to Overs; a red card has no fixed direction.
    """
    cfg = cfg or PricingConfig()
    p = params or LiveModelParams()
    now = time.time() if now is None else now
    reasons: list[str] = []
    if cfg.minute_windows is not None:
        if not any(lo <= state.minute <= hi for lo, hi in cfg.minute_windows):
            win = ", ".join(f"{lo:.0f}-{hi:.0f}" for lo, hi in cfg.minute_windows)
            reasons.append(f"minute {state.minute:.0f} outside windows {win}")
    else:
        if state.minute < cfg.min_minute:
            reasons.append(f"minute {state.minute:.0f} < {cfg.min_minute:.0f}")
        if state.minute > cfg.max_minute:
            reasons.append(f"minute {state.minute:.0f} > {cfg.max_minute:.0f}")
    if now - state.updated_at > cfg.max_state_age_s:
        reasons.append("stale match state")
    if cfg.require_stats and not getattr(state, "stats_available", True):
        reasons.append("live statistics missing")
    if ratings_source == "league_default" and not cfg.allow_default_ratings:
        reasons.append("only league-default ratings")
    status = getattr(state, "status", "")
    if status and status.lower() in {"halftime", "ended", "postponed", "interrupted", "abandoned"}:
        reasons.append(f"status {status}")
    triggers: list[str] = []
    am = getattr(state, "attack_momentum", None)
    if am is not None and abs(am) >= cfg.momentum_trigger:
        triggers.append("momentum_home" if am > 0 else "momentum_away")
    mt = getattr(state, "momentum_mult_total", None)
    if mt is not None and mt >= cfg.activity_trigger_mult:
        triggers.append("activity_high")
    if ratings is not None and favourite_chasing(state, ratings, p) is not None:
        triggers.append("fav_chasing")
    if state.home_red_cards or state.away_red_cards:
        triggers.append("red_card")
    if not triggers and not cfg.alert_without_trigger:
        reasons.append("no trigger")
    directional = any(t.startswith("momentum") or t in ("fav_chasing", "activity_high") for t in triggers)
    d: Selection | None = Selection.OVER if directional else None
    return ConfidenceReport(
        not reasons, tuple(reasons), tuple(triggers), {MarketType.TOTAL_GOALS: d, MarketType.TOTAL_CORNERS: d}
    )


def select_alert_lines(
    prices: Sequence[LinePrice], cfg: PricingConfig, direction: Mapping[MarketType, Selection | None]
) -> list[LinePrice]:
    """Pick the ``lines_per_market`` lines nearest the main line (fair ~ 2.0) per market/side."""
    lo, hi = cfg.alert_fair_odds_range
    out: list[LinePrice] = []
    groups: dict[tuple[MarketType, Selection], list[LinePrice]] = {}
    for lp in prices:
        want = direction.get(lp.market)
        if want is not None and lp.selection is not want:
            continue
        if lo <= lp.fair_odds <= hi:
            groups.setdefault((lp.market, lp.selection), []).append(lp)
    for (_, _), items in sorted(groups.items(), key=lambda kv: (kv[0][0].value, kv[0][1].value)):
        items.sort(key=lambda x: abs(math.log(x.fair_odds / 2.0)))
        out.extend(items[: cfg.lines_per_market])
    return out


class AlertRateLimiter:
    """Per-market cooldown + per-event alert budget for repeated alerts."""

    def __init__(self, cfg: PricingConfig | None = None) -> None:
        self.cfg = cfg or PricingConfig()
        self._last: dict[tuple[str, str, float | None, str], tuple[float, float]] = {}
        self._event_hist: dict[str, deque[float]] = {}

    def allow(self, event_key: str, market: MarketType, line: float | None, sel: Selection, fair: float, now: float | None = None) -> bool:
        """True if an alert may be sent now (and records it)."""
        now = time.time() if now is None else now
        c = self.cfg
        hist = self._event_hist.setdefault(event_key, deque())
        while hist and now - hist[0] > c.event_window_s:
            hist.popleft()
        if len(hist) >= c.max_alerts_per_event_window:
            return False
        key = (event_key, market.value, line, sel.value)
        last = self._last.get(key)
        if last is not None:
            t, f = last
            if now - t < c.realert_cooldown_s and abs(fair / f - 1.0) < c.realert_fair_move:
                return False
        self._last[key] = (now, fair)
        hist.append(now)
        return True


def market_family(market: MarketType) -> str:
    """Correlation family of a market: ``goals`` or ``corners``."""
    return "corners" if market is MarketType.TOTAL_CORNERS else "goals"


class AlertDeduplicator:
    """De-correlation / exposure policy for model-only alerts (v3).

    Per match and market family (goals, corners):

    * **cooldown** - after an alert the family is locked for ``family_cooldown_min`` match
      minutes, and
    * **family cap** - at most ``max_signals_per_family`` alerts,

    unless the **state-change exception** applies: a goal or red card occurred since the
    family's last alert *and* the new line differs by >= ``state_change_line_gap``.
    Per match: at most ``max_signals_per_match`` alerts (exception as above) and the sum
    of quarter-Kelly stakes (first tier = stake at min odds) <= ``max_match_stake_frac`` x
    bankroll - the stake cap has **no** exception. Blocked alerts are dropped, not resized.
    """

    def __init__(self, cfg: PricingConfig | None = None) -> None:
        self.cfg = cfg or PricingConfig()
        self.family: dict[tuple[str, str], dict[str, Any]] = {}
        self.match: dict[str, dict[str, float]] = {}

    @staticmethod
    def state_sig(state: MatchState) -> tuple[int, int, int, int]:
        return (state.home_goals, state.away_goals, state.home_red_cards, state.away_red_cards)

    def check(self, event_key: str, family: str, line: float, stake: float, minute: float,
              sig: tuple[int, ...], pending_stake: float = 0.0, pending_n: int = 0) -> str | None:
        """``None`` if allowed, else the blocking rule (cooldown/family_cap/match_signal_cap/stake_cap)."""
        c = self.cfg
        f = self.family.get((event_key, family))
        m = self.match.get(event_key, {"stake": 0.0, "n": 0})
        exception = c.state_change_exception and f is not None and tuple(f["sig"]) != tuple(sig) and abs(line - f["line"]) >= c.state_change_line_gap - 1e-9
        if f is not None and not exception:
            if minute - f["minute"] < c.family_cooldown_min:
                return "cooldown"
            if f["count"] >= c.max_signals_per_family:
                return "family_cap"
        if m["n"] + pending_n >= c.max_signals_per_match and not exception:
            return "match_signal_cap"
        if m["stake"] + pending_stake + stake > c.max_match_stake_frac * c.bankroll + 1e-9:
            return "stake_cap"
        return None

    def record(self, event_key: str, family: str, line: float, stake: float, minute: float, sig: tuple[int, ...]) -> None:
        f = self.family.get((event_key, family))
        self.family[(event_key, family)] = {"line": line, "minute": minute, "sig": tuple(sig),
                                            "count": (f["count"] if f else 0) + 1}
        m = self.match.setdefault(event_key, {"stake": 0.0, "n": 0})
        m["stake"] += stake
        m["n"] += 1


# ---------------------------------------------------------------------------
# Pre-match ratings resolution: Pinnacle cache -> Football-Data table -> league default
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SharpPrices:
    """Cached pre-match sharp (Pinnacle) prices for one fixture."""

    home: float
    draw: float
    away: float
    total_line: float | None = None
    over: float | None = None
    under: float | None = None
    fetched_at: float = 0.0


class TeamRatingsSource(Protocol):
    """Anything that can produce :class:`PreMatchRatings` for a fixture (see football_data)."""

    def ratings(self, home: str, away: str, league: str | None = None) -> PreMatchRatings | None: ...


LEAGUE_DEFAULTS: dict[str, PreMatchRatings] = {
    # Rough priors (goals: home-adv ~0.3; corners ~10). Calibrate with football_data.py.
    "default": PreMatchRatings(0.30, 2.70, 0.90, 10.0),
    "premier league": PreMatchRatings(0.30, 2.85, 1.0, 10.4),
    "bundesliga": PreMatchRatings(0.30, 3.10, 0.8, 9.6),
    "serie a": PreMatchRatings(0.25, 2.70, 0.8, 9.8),
    "la liga": PreMatchRatings(0.30, 2.55, 0.9, 9.6),
    "laliga": PreMatchRatings(0.30, 2.55, 0.9, 9.6),
    "champions league": PreMatchRatings(0.30, 3.00, 0.9, 10.0),
    "europa league": PreMatchRatings(0.30, 2.80, 0.9, 10.0),
    "ligue 1": PreMatchRatings(0.30, 2.75, 0.9, 9.5),
}


class RatingsResolver:
    """Resolve pre-match ratings with provenance:
    ``pinnacle`` > ``<prior provider>`` (e.g. ESPN pickcenter DraftKings close) > ``football_data`` > ``league_default``.

    Goals come from the de-vigged cached Pinnacle 1X2 (+ totals) when present,
    else from the optional ``prior_lookup`` (a softer book's pre-match lines);
    corners always come from the team table (or league default) because the
    free odds feeds have no corner lines.
    """

    def __init__(
        self,
        sharp_lookup: Callable[[str, str, datetime | None], SharpPrices | None] | None = None,
        team_table: TeamRatingsSource | None = None,
        league_defaults: Mapping[str, PreMatchRatings] | None = None,
        devig_method: str = "power",
        prior_lookup: Callable[[str], tuple[SharpPrices, str] | None] | None = None,
    ) -> None:
        self.sharp_lookup = sharp_lookup
        self.prior_lookup = prior_lookup  # event_key -> (prices, provider label)
        self.team_table = team_table
        self.league_defaults = dict(league_defaults or LEAGUE_DEFAULTS)
        self.devig_method = devig_method
        self._cache: dict[str, tuple[PreMatchRatings, str]] = {}

    def league_default(self, league: str) -> PreMatchRatings:
        """Best league-default prior for a league name."""
        name = (league or "").lower()
        for k, v in self.league_defaults.items():
            if k != "default" and k in name:
                return v
        return self.league_defaults["default"]

    def resolve(
        self, event_key: str, home: str, away: str, league: str = "", kickoff: datetime | None = None
    ) -> tuple[PreMatchRatings, str]:
        """Ratings and their source; cached per event (pre-match ratings are frozen)."""
        hit = self._cache.get(event_key)
        if hit and hit[1] == "pinnacle":
            return hit
        base = self.league_default(league)
        table = self.team_table.ratings(home, away, league) if self.team_table else None
        corners = table or base
        src = "football_data" if table else "league_default"
        goals = table or base
        sharp = self.sharp_lookup(home, away, kickoff) if self.sharp_lookup else None
        ph = pa = None
        label = "pinnacle"
        if sharp is None and self.prior_lookup is not None:
            hitp = self.prior_lookup(event_key)
            if hitp is not None:
                sharp, label = hitp[0], hitp[1] or "market_prior"
        if sharp is not None:
            try:
                ph, pd, pa = devig([sharp.home, sharp.draw, sharp.away], self.devig_method)
                p_over = None
                if sharp.total_line is not None and sharp.over and sharp.under:
                    p_over = devig([sharp.over, sharp.under], self.devig_method)[0]
                goals = ratings_from_prices(ph, pd, pa, p_over, sharp.total_line or 2.5, corners.corner_total)
                src = label
            except ValueError:
                pass
        r = PreMatchRatings(goals.goal_supremacy, goals.goal_total, corners.corner_supremacy, corners.corner_total,
                            ph if src == label and sharp is not None else None,
                            pa if src == label and sharp is not None else None)
        self._cache[event_key] = (r, src)
        return r, src


# ---------------------------------------------------------------------------
# Free-stack orchestration
# ---------------------------------------------------------------------------
def lift_gate_reason(lp: LinePrice, lift: Mapping[str, Any], cfg: PricingConfig) -> str | None:
    """v4.1 positive-lift gate; returns a trace reason or None if the line may compete."""
    fam = lift[market_family(lp.market)]
    g = cfg.lift_gate or 1.0
    net = fam["net"]
    pf = fam["prior_fair"].get((lp.line, lp.selection))
    if lp.selection is Selection.OVER:
        if net <= cfg.lift_suppress_net:
            return "lift: net multiplier <= %.2f (Overs suppressed)" % cfg.lift_suppress_net
        if net < g:
            return "lift: net multiplier < %.2f" % g
        if pf is None or lp.fair_odds > pf / g + 1e-12:
            return "lift: Over line lift < %.0f%%" % ((g - 1) * 100)
        return None
    if lp.selection is Selection.UNDER:
        if not cfg.allow_unders:
            return "lift: Unders disabled"
        if net >= 1.0:
            return "lift: Under needs lambda_model < lambda_prior"
        if pf is None or lp.fair_odds > pf / g + 1e-12:
            return "lift: Under line lift < %.0f%%" % ((g - 1) * 100)
        return None
    return "lift: unsupported selection"


class FreeSignalEngine:
    """Engine B on free live stats: model-only MIN-ODDS alerts + direct +EV path.

    * :meth:`on_match_state` – price every goal/corner line from a
      :class:`~quantbet.models.LiveMatchState`, gate on confidence, rate-limit
      and emit :class:`~quantbet.models.Signal` objects with urgency ``MIN_ODDS``
      (``bookmaker_odds`` = minimum Bet365 odds; tier table in ``extra["tiers"]``).
    * :meth:`on_odds` – when an aggregator delivers real prices, defer to the
      classic :class:`SignalEngine` (Engine A vs. sharp, Engine B fallback).
    """

    def __init__(
        self,
        resolver: RatingsResolver | None = None,
        pricing: PricingConfig | None = None,
        engine: SignalEngine | None = None,
        sizer: StakeSizer | None = None,
    ) -> None:
        self.resolver = resolver or RatingsResolver()
        self.pricing = pricing or PricingConfig()
        self.engine = engine or SignalEngine()
        self.model = self.engine.model
        self.sizer = sizer
        self.limiter = AlertRateLimiter(self.pricing)
        self.dedup = AlertDeduplicator(self.pricing)
        self.auto_commit = True  # main.py sets False and commits after the FotMob confirmation step
        self.last_prices: dict[str, list[LinePrice]] = {}
        self.last_trace: dict[str, dict[tuple[Any, float, Any], str]] = {}
        self.blowout: set[str] = set()  # matches with |diff| >= blowout_block_diff seen (v4)
        self.last_lift: dict[str, dict[str, Any] | None] = {}
        self._prior_model: LiveFootballModel | None = None

    def prior_model(self) -> LiveFootballModel:
        """Same engine with ``prior_only=True``: time-decayed pre-match rates, no in-play multipliers."""
        if self._prior_model is None or self._prior_model.p is not self._prior_src:
            self._prior_src = self.model.p
            self._prior_model = LiveFootballModel(dataclasses.replace(self.model.p, prior_only=True))
        return self._prior_model

    def lift_context(self, state: MatchState, ratings: PreMatchRatings) -> dict[str, Any]:
        """Per family: remaining lambdas (model vs prior), net multiplier and prior fair odds per line.

        Both predictions start from the same state (score, corners, minute, stoppage), so a goal
        line's probability is P(remaining >= line - current total) in both: like-for-like.
        """
        pred = self.model.predict(state, ratings)
        cpred = pred
        if self.model.p.chasing_corner_mode == "cap" and chasing_corner_cap_active(state, ratings, self.model.p):
            cpred = self.model.predict(state, ratings, chase_corner_cap=self.model.p.chasing_corner_cap)
        ppred = self.prior_model().predict(state, ratings)
        out: dict[str, Any] = {}
        for fam, market, lm, lpri, pm in (
            ("goals", MarketType.TOTAL_GOALS, pred.lam_home_goals + pred.lam_away_goals,
             ppred.lam_home_goals + ppred.lam_away_goals, pred),
            ("corners", MarketType.TOTAL_CORNERS, cpred.mu_corners, ppred.mu_corners, cpred),
        ):
            cur = (state.home_goals + state.away_goals) if fam == "goals" else (state.home_corners + state.away_corners)
            span = self.pricing.goal_line_span if fam == "goals" else self.pricing.corner_line_span
            pf: dict[tuple[float, Selection], float] = {}
            for line in candidate_lines(cur, span, self.pricing.line_step):
                for sel in (Selection.OVER, Selection.UNDER):
                    try:
                        f = ppred.settlement(market, sel, line).fair_odds()
                    except Exception:  # noqa: BLE001
                        continue
                    if f and math.isfinite(f):
                        pf[(line, sel)] = f
            out[fam] = {"lam_model": lm, "lam_prior": lpri, "net": (lm / lpri) if lpri > 1e-9 else 1.0,
                        "prior_fair": pf}
        return out

    def _reference_fair(self, state: MatchState, ratings: PreMatchRatings) -> dict[tuple[Any, float, Any], float]:
        """Fair odds of a *neutral* model (live pressure multipliers = 1, attack momentum off).

        Model-only reference price: no Bet365 price exists, so the reference is what a
        market that knows score, clock and red cards but ignores live pressure would
        price. ``EV at reference`` = EV of the full model at that price.
        """
        import copy

        try:
            neutral = copy.copy(state)
            for attr in ("momentum_mult_home", "momentum_mult_away"):
                if hasattr(neutral, attr):
                    setattr(neutral, attr, 1.0)
            for attr in ("attack_momentum", "home_shots_on_target_10", "away_shots_on_target_10",
                         "home_dangerous_attacks_10", "away_dangerous_attacks_10"):
                if hasattr(neutral, attr):
                    setattr(neutral, attr, None)
            if not hasattr(neutral, "momentum_mult_home"):
                return {}
            out = {}
            for lp in price_live_lines(neutral, ratings, self.pricing, self.model, None):
                out[(lp.market, lp.line, lp.selection)] = lp.fair_odds
            return out
        except Exception:  # noqa: BLE001
            return {}

    def on_odds(self, snapshots: Iterable[OddsSnapshot], now: float | None = None) -> list[Signal]:
        """Direct +EV path when real (aggregator / API) odds are available."""
        return self.engine.process(snapshots, now)

    def on_match_state(self, state: MatchState, now: float | None = None,
                       real_odds: Mapping[tuple[Any, float, Any], float] | None = None) -> list[Signal]:
        """Update Engine B with a new live state and return min-odds alerts (+ direct +EV).

        ``self.last_trace[event_key]`` maps ``(market, line, selection)`` to the decision for
        every priced line (``emitted``, ``gate: ...``, ``direction``, ``fair_range``, ``no_tiers``,
        ``neg_ref_ev``, ``not_best``, ``cooldown``, ``family_cap``, ``match_signal_cap``,
        ``stake_cap``, ``rate_limited``) - used by the replay for rule attribution.
        """
        now = time.time() if now is None else now
        home = getattr(state, "home", "") or "Home"
        away = getattr(state, "away", "") or "Away"
        league = getattr(state, "league", "")
        kickoff = getattr(state, "commence_time", None)
        ratings, src = self.resolver.resolve(state.event_key, home, away, league, kickoff)
        self.engine.update_match_state(state)
        self.engine.set_ratings(state.event_key, ratings)
        if state.event_key not in self.engine.events:
            self.engine.events[state.event_key] = EventInfo(state.event_key, home, away, league, kickoff, True)
        signals = self.engine.evaluate_live_event(state.event_key, now)
        prices = price_live_lines(state, ratings, self.pricing, self.model, self.sizer)
        self.last_prices[state.event_key] = prices
        trace: dict[tuple[Any, float, Any], str] = {}
        self.last_trace[state.event_key] = trace
        bd = self.pricing.blowout_block_diff
        if bd is not None and abs(state.home_goals - state.away_goals) >= bd:
            self.blowout.add(state.event_key)
        conf = assess_confidence(state, src, self.pricing, ratings, now, self.model.p)
        if not conf.ok:
            for lp in prices:
                trace[(lp.market, lp.line, lp.selection)] = "gate: " + "; ".join(conf.reasons)
            log.debug("model-only alert gated", extra={"event": state.event_key, "reasons": list(conf.reasons)})
            return signals
        ref = self._reference_fair(state, ratings)
        cfg = self.pricing

        def ev_ref_of(lp: LinePrice) -> float | None:
            rf = ref.get((lp.market, lp.line, lp.selection))
            return round(lp.ev_at(rf), 5) if rf else None

        if cfg.selection_mode not in ("best_growth", "fair_target"):  # legacy policy
            for lp in select_alert_lines(prices, cfg, conf.direction):
                if not lp.tiers:
                    continue
                if not self.limiter.allow(state.event_key, lp.market, lp.line, lp.selection, lp.fair_odds, now):
                    trace[(lp.market, lp.line, lp.selection)] = "rate_limited"
                    continue
                trace[(lp.market, lp.line, lp.selection)] = "emitted"
                signals.append(self._signal(state, lp, conf, src, ratings, ref, ev_ref_of(lp), kickoff, home, away, league))
            return signals

        lo, hi = cfg.alert_fair_odds_range
        tlo, thi = cfg.fair_target_range
        lift = self.lift_context(state, ratings) if cfg.lift_gate is not None else None
        self.last_lift[state.event_key] = lift
        fams: dict[str, list[tuple[float, float, LinePrice, float | None]]] = {}
        for lp in prices:
            k = (lp.market, lp.line, lp.selection)
            if state.event_key in self.blowout and lp.selection is Selection.OVER:
                trace[k] = "blowout"
                continue
            if lift is not None and (real_odds or {}).get(k) is None:
                why = lift_gate_reason(lp, lift, cfg)
                if why:
                    trace[k] = why
                    continue
            else:
                want = conf.direction.get(lp.market)
                if lp.selection is Selection.UNDER and lift is not None and not cfg.allow_unders:
                    trace[k] = "direction"
                    continue
                if want is not None and lp.selection is not want:
                    trace[k] = "direction"
                    continue
            if not (lo <= lp.fair_odds <= hi):
                trace[k] = "fair_range"
                continue
            if not lp.tiers:
                trace[k] = "no_tiers"
                continue
            evr = ev_ref_of(lp)
            if cfg.drop_negative_reference_ev and evr is not None and evr < 0:
                trace[k] = "neg_ref_ev"
                continue
            ro = (real_odds or {}).get(k)
            if cfg.selection_mode == "fair_target" and ro is None:
                if not (tlo <= lp.fair_odds <= thi):
                    trace[k] = "fair_target_range"
                    continue
                score = -abs(lp.fair_odds - cfg.fair_target)  # closest to 1.95 (p ~ 0.5)
            else:
                score = expected_log_growth(lp, ro) if ro else expected_log_growth(lp)
            trace[k] = "not_best"
            fams.setdefault(market_family(lp.market), []).append((score, lp.ev_at(ro or lp.min_odds), lp, evr, ro is not None))
        best = []
        for v in fams.values():
            with_ro = [x for x in v if x[4]]
            # fair_target: once real bookmaker odds are attached to a family, only those lines
            # compete, on expected log-growth at the real price (scores are not mixed).
            pool = with_ro if (cfg.selection_mode == "fair_target" and with_ro) else v
            best.append(max(pool, key=lambda x: (x[0], x[1]))[:4])
        best.sort(key=lambda x: (-x[0], -x[1]))
        sig_state = AlertDeduplicator.state_sig(state)
        pend_stake, pend_n = 0.0, 0
        for g, _ev, lp, evr in best:
            k = (lp.market, lp.line, lp.selection)
            fam = market_family(lp.market)
            stake = lp.tiers[0].stake
            if cfg.use_deduplicator:
                why = self.dedup.check(state.event_key, fam, lp.line, stake, state.minute, sig_state, pend_stake, pend_n)
                if why:
                    trace[k] = why
                    continue
            elif not self.limiter.allow(state.event_key, lp.market, lp.line, lp.selection, lp.fair_odds, now):
                trace[k] = "rate_limited"
                continue
            sig = self._signal(state, lp, conf, src, ratings, ref, evr, kickoff, home, away, league)
            sig.extra["log_growth"] = round(expected_log_growth(lp), 6)
            if lift is not None:
                fam_l = lift[market_family(lp.market)]
                pf = fam_l["prior_fair"].get((lp.line, lp.selection))
                sig.extra["lift"] = {"prior_fair": round(pf, 4) if pf else None,
                                     "lift": round(pf / lp.fair_odds - 1.0, 4) if pf else None,
                                     "net_multiplier": round(fam_l["net"], 4),
                                     "lambda_model": round(fam_l["lam_model"], 4),
                                     "lambda_prior": round(fam_l["lam_prior"], 4)}
            sig.extra["selection_mode"] = cfg.selection_mode
            sig.extra["dedup"] = {"family": fam, "line": lp.line, "stake": stake, "minute": state.minute,
                                  "sig": list(sig_state)}
            if lp.notes:
                sig.extra["notes"] = list(lp.notes)
            if (cfg.corner_over_fotmob_after is not None and lp.market is MarketType.TOTAL_CORNERS
                    and lp.selection is Selection.OVER and state.minute > cfg.corner_over_fotmob_after):
                fc = favourite_chasing(state, ratings, self.model.p)
                mh = getattr(state, "momentum_mult_home", None) or 1.0
                ma = getattr(state, "momentum_mult_away", None) or 1.0
                sig.extra["fotmob_required"] = "dominance"
                sig.extra["dominant_side"] = fc[0] if fc else ("home" if mh >= ma else "away")
            trace[k] = "emitted"
            if self.auto_commit:
                self.commit(sig)  # recorded now -> not also counted as pending
            else:
                pend_stake += stake
                pend_n += 1
            signals.append(sig)
        return signals

    def commit(self, sig: Signal) -> None:
        """Record an alert in the deduplicator (call after any external confirmation step)."""
        d = (sig.extra or {}).get("dedup")
        if d and self.pricing.use_deduplicator:
            self.dedup.record(sig.event_key, d["family"], d["line"], d["stake"], d["minute"], tuple(d["sig"]))

    def _signal(self, state: MatchState, lp: LinePrice, conf: ConfidenceReport, src: str, ratings: PreMatchRatings,
                ref: Mapping[Any, float], ev_ref: float | None, kickoff: Any, home: str, away: str, league: str) -> Signal:
        ref_fair = ref.get((lp.market, lp.line, lp.selection))
        p_eff = lp.settlement.effective_probability(lp.min_odds)
        return Signal(
            urgency=Urgency.MIN_ODDS,
            engine="B",
            event_key=state.event_key,
            home=home,
            away=away,
            league=league,
            market=lp.market,
            line=lp.line,
            selection=lp.selection,
            bookmaker="bet365 (check app)",
            bookmaker_odds=lp.min_odds,
            p_true=p_eff,
            fair_odds=lp.fair_odds,
            min_acceptable_odds=lp.abort_floor,
            ev=lp.ev_at(lp.min_odds),
            kelly_fraction=lp.tiers[0].kelly,
            is_live=True,
            minute=state.minute,
            score=state.score,
            commence_time=kickoff,
            method="live:negbin" if lp.market is MarketType.TOTAL_CORNERS else "live:bivariate_poisson",
            sharp_source=None,
            extra={
                "model_only": True,
                "min_odds": lp.min_odds,
                "abort_floor": lp.abort_floor,
                "p_push_adjusted": round(lp.p_true, 5),
                "tiers": [(t.odds, t.stake) for t in lp.tiers],
                "settlement": [lp.settlement.win, lp.settlement.half_win, lp.settlement.push,
                               lp.settlement.half_loss, lp.settlement.loss],
                "triggers": list(conf.triggers),
                "ratings_source": src,
                "source": getattr(state, "source", ""),
                "attack_momentum": getattr(state, "attack_momentum", None),
                "momentum_mult": (getattr(state, "momentum_mult_home", None),
                                  getattr(state, "momentum_mult_away", None),
                                  getattr(state, "momentum_mult_total", None)),
                "reference_fair": ref_fair,
                "ev_at_reference": ev_ref,
                "home_goals": state.home_goals, "away_goals": state.away_goals,
                "home_corners": getattr(state, "home_corners", None),
                "away_corners": getattr(state, "away_corners", None),
                "lambda_pre_total": round(ratings.home_goals + ratings.away_goals, 4),
            },
        )


# ---------------------------------------------------------------------------
# Rule-set presets (legacy / v3 / v4)
# ---------------------------------------------------------------------------
RULE_VERSIONS = ("legacy", "v3", "v4", "v4.1", "v4.2", "v4.3-global")

_LIVE_RULE_FIELDS: dict[str, dict[str, Any]] = {
    "legacy": dict(shrinkage_enabled=False, shrinkage_mode="linear", game_state_dampener=False, fav_rule="supremacy",
                   chasing_corner_penalty=False, chasing_corner_mode="margin", momentum_clip=None,
                   dampener_goal_mult=0.82, dampener_leader_corner_mult=0.85, dampener_corner_scope="leader"),
    "v3": dict(shrinkage_enabled=True, shrinkage_mode="linear", game_state_dampener=True, fav_rule="win_prob",
               chasing_corner_penalty=True, chasing_corner_mode="margin", momentum_clip=None,
               dampener_goal_mult=0.82, dampener_leader_corner_mult=0.85, dampener_corner_scope="leader"),
    "v4": dict(shrinkage_enabled=True, shrinkage_mode="corners_gamma", corner_prior_minutes=180.0,
               game_state_dampener=True, dampener_goal_diff=2, dampener_goal_mult=0.75,
               dampener_leader_corner_mult=0.80, dampener_corner_scope="both", fav_rule="win_prob",
               fav_min_win_prob=0.55, fav_chasing_min_minute=55.0, fav_chasing_max_deficit=1,
               chasing_corner_penalty=True, chasing_corner_mode="cap", chasing_corner_cap=1.03,
               chasing_corner_cap_minute=60.0, momentum_clip=(0.85, 1.25)),
}
_PRICING_RULE_FIELDS: dict[str, dict[str, Any]] = {
    "legacy": dict(minute_windows=None, selection_mode="nearest_main", drop_negative_reference_ev=False,
                   use_deduplicator=False, corner_over_fotmob_after=None, state_change_exception=True,
                   blowout_block_diff=None),
    "v3": dict(minute_windows=((30.0, 43.0), (55.0, 85.0)), selection_mode="best_growth",
               drop_negative_reference_ev=True, use_deduplicator=True, corner_over_fotmob_after=75.0,
               state_change_exception=True, blowout_block_diff=None),
    "v4": dict(minute_windows=((30.0, 43.0), (55.0, 85.0)), selection_mode="fair_target", fair_target=1.95,
               fair_target_range=(1.75, 2.20), drop_negative_reference_ev=True, use_deduplicator=True,
               max_signals_per_family=1, max_signals_per_match=2, max_match_stake_frac=0.05,
               corner_over_fotmob_after=75.0, state_change_exception=False, blowout_block_diff=3),
}
# Pre-v4.2 sets never use the half profile (explicit, so a v4.2 params file cannot leak into them).
for _v in ("legacy", "v3", "v4"):
    _LIVE_RULE_FIELDS[_v].update(corner_half_ratio=None, corner_down_shrink_minute=55.0)
# v4.1 = v4 + positive-lift gate + Under support (model fields identical to v4).
_LIVE_RULE_FIELDS["v4.1"] = dict(_LIVE_RULE_FIELDS["v4"])
_PRICING_RULE_FIELDS["v4.1"] = dict(_PRICING_RULE_FIELDS["v4"], lift_gate=1.06, lift_suppress_net=1.05,
                                    allow_unders=True, alert_without_trigger=True)
# v4.2 = v4.1 Overs-only + calibrated corner half split + half-adjusted, upward-only-before-55' corner shrinkage
# + refitted lead dampeners (config/calibration_report.json "v4_2"):
#   corner H2/H1 per-minute ratio 1.064 (StatsBomb open data, 354 matches, 90% CI 1.011-1.122);
#   goals lead dampener: Football-Data fit 1.033 (n=1223 HT |diff|>=2, CI 1.00-1.065) -> no dampening found,
#   capped at 1.00 because dampeners may only suppress Overs; corners (both teams) 0.952 (StatsBomb, 163 matches).
V42_CORNER_HALF_RATIO = 1.064
V42_GOAL_LEAD_DAMPENER = 1.0
V42_CORNER_LEAD_DAMPENER = 0.952
_LIVE_RULE_FIELDS["v4.2"] = dict(_LIVE_RULE_FIELDS["v4.1"], shrinkage_mode="corners_gamma_half",
                                 corner_half_ratio=V42_CORNER_HALF_RATIO, corner_down_shrink_minute=55.0,
                                 dampener_goal_mult=V42_GOAL_LEAD_DAMPENER,
                                 dampener_leader_corner_mult=V42_CORNER_LEAD_DAMPENER)
_PRICING_RULE_FIELDS["v4.2"] = dict(_PRICING_RULE_FIELDS["v4.1"], allow_unders=False)
# v4.3-global = v4.2 maths UNCHANGED (identical live/pricing rule fields) + the global layer
# (quantbet.global_layer: league registry/tiers, live-stats integrity gate, slug baselines, tier sizing),
# whose parameters are frozen separately under "global" in the v4.3-global freeze entry.
_LIVE_RULE_FIELDS["v4.3-global"] = dict(_LIVE_RULE_FIELDS["v4.2"])
_PRICING_RULE_FIELDS["v4.3-global"] = dict(_PRICING_RULE_FIELDS["v4.2"])


def rules_preset(version: str, base_live: LiveModelParams | None = None, **pricing_kw: Any
                 ) -> tuple[PricingConfig, LiveModelParams]:
    """``(PricingConfig, LiveModelParams)`` for rule set ``legacy`` / ``v3`` / ``v4``.

    Non-rule (calibrated) model fields come from ``base_live``; rule fields from the preset.
    ``pricing_kw`` (bankroll, edges, ...) are applied before the preset's rule fields, so a
    preset cannot be silently overridden by environment settings (v4 is frozen).
    """
    v = version.lower()
    if v not in RULE_VERSIONS:
        raise ValueError(f"unknown rules version {version!r}; choose from {RULE_VERSIONS}")
    base = dataclasses.asdict(base_live) if base_live is not None else {}
    base = {k: (tuple(x) if isinstance(x, list) else x) for k, x in base.items()}
    base.update(_LIVE_RULE_FIELDS[v])
    live = LiveModelParams(**base)
    kw = dict(pricing_kw)
    kw.update(_PRICING_RULE_FIELDS[v])
    return PricingConfig(**kw), live
