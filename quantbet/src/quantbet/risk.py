"""Risk module: fractional-Kelly sizing, natural stake rounding, liquidity flags."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field

from .engine import kelly_fraction
from .models import MarketType, Signal


@dataclass(slots=True)
class RiskConfig:
    """Bankroll & sizing parameters."""

    bankroll: float = 2000.0
    unit_size: float = 10.0
    currency: str = "EUR"
    kelly_multiplier: float = 0.25
    max_stake_fraction: float = 0.03  # hard cap per bet as fraction of bankroll
    round_small_step: float = 5.0  # stakes below `round_switch_at` -> nearest 5
    round_large_step: float = 10.0  # stakes at/above -> nearest 10
    round_switch_at: float = 100.0
    high_liquidity_leagues: tuple[str, ...] = (
        "premier league",
        "la liga",
        "bundesliga",
        "serie a",
        "ligue 1",
        "champions league",
        "europa league",
        "conference league",
    )
    high_limit_risk_stake_mult: float = 1.0  # set <1 to shade stakes on flagged bets
    # v4.3-global liquidity tiers (empty = off, v4.2 behaviour): Kelly multiplier per tier and a cap in units.
    tier_kelly_mult: tuple[tuple[str, float], ...] = ()
    tier_max_units: tuple[tuple[str, float], ...] = ()


def round_stake(raw: float, cfg: RiskConfig, cap: float) -> float:
    """Round a stake to a "recreational" number (nearest 5 below 100, nearest 10 above).

    Rounding never exceeds ``cap``: if the rounded value is above the cap it is
    rounded *down* to the step instead. Returns 0 if the stake rounds to 0.
    """
    if raw <= 0:
        return 0.0
    step = cfg.round_small_step if raw < cfg.round_switch_at else cfg.round_large_step
    stake = round(raw / step) * step
    if stake > cap + 1e-9:
        stake = math.floor(cap / step) * step
    return float(max(0.0, stake))


@dataclass(slots=True)
class RiskManager:
    """Sizes signals and annotates them with liquidity / limit-risk warnings."""

    cfg: RiskConfig = field(default_factory=RiskConfig)

    def is_high_liquidity_league(self, league: str) -> bool:
        """True if the league name matches one of the configured major competitions."""
        name = league.lower()
        return any(tag in name for tag in self.cfg.high_liquidity_leagues)

    def high_limit_risk(self, signal: Signal) -> bool:
        """Flag pre-match bets on obscure leagues (Bet365 limits those fastest).

        In-play goals/corners markets are treated as tolerated volume even in
        smaller leagues; pre-match outside the configured list is flagged.
        """
        if self.is_high_liquidity_league(signal.league):
            return False
        if signal.is_live and signal.market in (MarketType.TOTAL_GOALS, MarketType.TOTAL_CORNERS):
            return False
        return True

    def tier_mult(self, tier: str | None) -> float:
        """Kelly multiplier of a liquidity tier (1.0 when tiers are off or the tier is unknown)."""
        return float(dict(self.cfg.tier_kelly_mult).get(tier or "", 1.0)) if tier else 1.0

    def tier_cap(self, tier: str | None) -> float:
        """Per-bet cap (currency): max_stake_fraction x bankroll, and the tier's unit cap if any."""
        c = self.cfg
        cap = c.bankroll * c.max_stake_fraction
        mu = dict(c.tier_max_units).get(tier or "") if tier else None
        if mu is not None and c.unit_size > 0:
            cap = min(cap, float(mu) * c.unit_size)
        return cap

    def stake_from_kelly(self, f: float, tier: str | None = None) -> float:
        """Rounded, capped stake (currency) for an already-multiplied Kelly fraction (x tier multiplier)."""
        c = self.cfg
        cap = self.tier_cap(tier)
        return round_stake(min(max(f, 0.0) * self.tier_mult(tier) * c.bankroll, cap), c, cap)

    def size(self, signal: Signal) -> Signal:
        """Fill ``stake_units``, ``stake_currency``, ``high_limit_risk`` and notes in place."""
        c = self.cfg
        tier = signal.extra.get("liquidity_tier") if isinstance(signal.extra, dict) else None
        f = kelly_fraction(signal.p_true, signal.bookmaker_odds, c.kelly_multiplier) * self.tier_mult(tier)
        signal.kelly_fraction = f
        cap = self.tier_cap(tier)
        raw = min(f * c.bankroll, cap)
        signal.high_limit_risk = self.high_limit_risk(signal)
        if signal.high_limit_risk:
            signal.risk_notes.append("High Limit Risk: obscure/low-liquidity market")
            raw *= c.high_limit_risk_stake_mult
        if f * c.bankroll > cap:
            signal.risk_notes.append(f"stake capped at {c.max_stake_fraction:.1%} of bankroll"
                                     if cap >= c.bankroll * c.max_stake_fraction - 1e-9 else
                                     f"stake capped at {cap / c.unit_size:.0f} units ({tier})")
        if tier and self.tier_mult(tier) != 1.0:
            signal.risk_notes.append(f"{tier}: stake x{self.tier_mult(tier):g} of quarter-Kelly")
        stake = round_stake(raw, c, cap)
        if stake == 0.0 and raw > 0:
            signal.risk_notes.append("Kelly stake below minimum rounded stake")
        signal.stake_currency = stake
        signal.stake_units = stake / c.unit_size if c.unit_size > 0 else 0.0
        signal.currency = c.currency
        tiers = signal.extra.get("tiers") if isinstance(signal.extra, dict) else None
        if tiers and signal.high_limit_risk:
            scaled, last = [], None
            for o, st in tiers:
                v = round_stake(st * c.high_limit_risk_stake_mult, c, cap)
                if v > 0 and v != last:
                    scaled.append((o, v))
                    last = v
            signal.extra["tiers"] = scaled
        return signal

    def size_all(self, signals: Iterable[Signal]) -> list[Signal]:
        """Size many signals and drop those whose rounded stake is zero.

        Model-only MIN-ODDS alerts are kept if their tier table has a non-zero stake.
        """
        return [
            s for s in (self.size(x) for x in signals) if s.stake_currency > 0 or s.extra.get("tiers")
        ]
