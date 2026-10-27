"""Unit tests for the pricing engine, risk, database and auditor."""

from __future__ import annotations

import math
import time
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from quantbet.auditor import ClosingRow, TipsterAuditor, TipsterPick, TipsterVerdict, parse_tip_text
from quantbet.database import AlertDatabase
from quantbet.engine import (
    EngineConfig,
    LiveFootballModel,
    LiveModelParams,
    PreMatchRatings,
    SettlementProbs,
    SignalEngine,
    SteamDetector,
    asian_settlement,
    bivariate_poisson_matrix,
    devig,
    devig_additive,
    devig_multiplicative,
    devig_power,
    devig_shin,
    expected_value,
    fair_odds,
    kelly_fraction,
    min_acceptable_odds,
    overround,
    ratings_from_prices,
    settle_bet,
    split_asian_line,
)
from quantbet.models import MarketKey, MarketType, MatchState, OddsSnapshot, Selection, Urgency
from quantbet.notifier import format_signal
from quantbet.poller import EventMatcher, StatsTracker, TokenBucket, normalize_team, team_similarity
from quantbet.risk import RiskConfig, RiskManager, round_stake

BOOKS = [
    [1.90, 3.60, 4.20],
    [1.25, 6.00, 12.0],
    [2.05, 1.85],
    [1.50, 4.50, 7.50],
    [3.10, 3.30, 2.40],
]


# --------------------------------------------------------------------- de-vig
@pytest.mark.parametrize("odds", BOOKS)
@pytest.mark.parametrize("method", ["multiplicative", "additive", "power", "shin"])
def test_devig_sums_to_one(odds: list[float], method: str) -> None:
    p = devig(odds, method)
    assert math.isclose(sum(p), 1.0, abs_tol=1e-9)
    assert all(0 < x < 1 for x in p)
    assert all(isinstance(x, float) for x in p)


def test_overround() -> None:
    assert math.isclose(overround([1.909, 1.909]), 2 / 1.909 - 1, rel_tol=1e-12)


def test_power_k_and_longshot_bias() -> None:
    odds = [1.25, 6.0, 12.0]
    q = [1 / o for o in odds]
    p = devig_power(odds)
    k = math.log(p[0]) / math.log(q[0])
    assert k > 1.0
    for qi, pi in zip(q, p):  # p_i = q_i ** k for every outcome
        assert math.isclose(pi, qi**k, rel_tol=1e-8)
    pm = devig_multiplicative(odds)
    # Power shades the longshot more and the favourite less than multiplicative.
    assert p[2] < pm[2]
    assert p[0] > pm[0]


def test_power_equals_input_when_no_margin() -> None:
    p = devig_power([2.0, 2.0])
    assert p == pytest.approx([0.5, 0.5])


def test_shin_sanity() -> None:
    odds = [1.25, 6.0, 12.0]
    p, z = devig_shin(odds, return_z=True)
    assert 0.0 < z < 0.2
    pm = devig_multiplicative(odds)
    assert p[2] < pm[2] and p[0] > pm[0]
    # Symmetric 2-way book -> 50/50 under every method.
    for m in ("multiplicative", "additive", "power", "shin"):
        assert devig([1.9, 1.9], m) == pytest.approx([0.5, 0.5])
    # Two-way market path (Brent solve) also yields z in (0,1).
    _, z2 = devig_shin([1.5, 2.6], return_z=True)
    assert 0.0 < z2 < 1.0


def test_shin_fixed_point_matches_root() -> None:
    """Shin probabilities satisfy the defining equation for the solved z."""
    odds = [1.9, 3.6, 4.2]
    p, z = devig_shin(odds, return_z=True)
    q = np.array([1 / o for o in odds])
    b = q.sum()
    expected = (np.sqrt(z * z + 4 * (1 - z) * q * q / b) - z) / (2 * (1 - z))
    assert np.allclose(p, expected / expected.sum())
    assert math.isclose(expected.sum(), 1.0, abs_tol=1e-9)


def test_additive_handles_negative() -> None:
    p = devig_additive([1.05, 15.0, 40.0])
    assert math.isclose(sum(p), 1.0, abs_tol=1e-12)
    assert min(p) >= 0.0


def test_devig_rejects_bad_odds() -> None:
    with pytest.raises(ValueError):
        devig([1.0, 2.0])
    with pytest.raises(ValueError):
        devig([2.0], "power")
    with pytest.raises(ValueError):
        devig([2.0, 2.0], "nope")


# --------------------------------------------------------------- EV & Kelly
def test_ev_fair_min_odds() -> None:
    assert expected_value(0.55, 2.0) == pytest.approx(0.10)
    assert expected_value(0.5, 2.05) == pytest.approx(0.025)
    assert fair_odds(0.4) == pytest.approx(2.5)
    assert min_acceptable_odds(0.5) == pytest.approx(2.03)
    assert expected_value(0.5, min_acceptable_odds(0.5)) == pytest.approx(0.015)


def test_kelly_known_values() -> None:
    # Full Kelly for p=0.55 @ 2.0 is 0.10 -> quarter Kelly 0.025.
    assert kelly_fraction(0.55, 2.0, 1.0) == pytest.approx(0.10)
    assert kelly_fraction(0.55, 2.0) == pytest.approx(0.025)
    # p=0.40 @ 3.0: ((2*0.4) - 0.6)/2 = 0.10 -> 0.025
    assert kelly_fraction(0.40, 3.0) == pytest.approx(0.025)
    # Negative edge clamps at 0; cap respected.
    assert kelly_fraction(0.45, 2.0) == 0.0
    assert kelly_fraction(0.9, 2.0, 1.0, cap=0.05) == 0.05


# ------------------------------------------------------------ Asian lines
def test_split_asian_line() -> None:
    assert split_asian_line(2.0) == (2.0,)
    assert split_asian_line(2.5) == (2.5,)
    assert split_asian_line(2.25) == (2.0, 2.5)
    assert split_asian_line(2.75) == (2.5, 3.0)
    assert split_asian_line(-0.25) == (-0.5, 0.0)
    assert split_asian_line(-1.75) == (-2.0, -1.5)
    with pytest.raises(ValueError):
        split_asian_line(2.1)


@pytest.mark.parametrize(
    "market,sel,line,h,a,expected",
    [
        (MarketType.TOTAL_GOALS, Selection.OVER, 2.25, 1, 1, "half_loss"),
        (MarketType.TOTAL_GOALS, Selection.OVER, 2.25, 2, 1, "win"),
        (MarketType.TOTAL_GOALS, Selection.UNDER, 2.25, 1, 1, "half_win"),
        (MarketType.TOTAL_GOALS, Selection.OVER, 2.75, 2, 1, "half_win"),
        (MarketType.TOTAL_GOALS, Selection.UNDER, 2.75, 2, 1, "half_loss"),
        (MarketType.TOTAL_GOALS, Selection.OVER, 3.0, 2, 1, "push"),
        (MarketType.TOTAL_GOALS, Selection.OVER, 2.5, 1, 1, "loss"),
        (MarketType.TOTAL_CORNERS, Selection.OVER, 9.5, 6, 4, "win"),
        (MarketType.TOTAL_CORNERS, Selection.UNDER, 10.25, 6, 4, "half_win"),
        (MarketType.ASIAN_HANDICAP, Selection.HOME, -0.25, 1, 1, "half_loss"),
        (MarketType.ASIAN_HANDICAP, Selection.AWAY, -0.25, 1, 1, "half_win"),
        (MarketType.ASIAN_HANDICAP, Selection.HOME, 0.0, 0, 0, "push"),
        (MarketType.ASIAN_HANDICAP, Selection.HOME, -0.75, 1, 0, "half_win"),
        (MarketType.ASIAN_HANDICAP, Selection.AWAY, -0.75, 1, 0, "half_loss"),
        (MarketType.ASIAN_HANDICAP, Selection.HOME, -0.75, 2, 0, "win"),
        (MarketType.ASIAN_HANDICAP, Selection.HOME, -1.0, 1, 0, "push"),
        (MarketType.ASIAN_HANDICAP, Selection.HOME, -1.5, 1, 0, "loss"),
        (MarketType.H2H, Selection.DRAW, None, 1, 1, "win"),
    ],
)
def test_asian_quarter_settlement(market: MarketType, sel: Selection, line: float | None, h: int, a: int, expected: str) -> None:
    assert settle_bet(market, sel, line, h, a)[0] == expected


def test_asian_ev_exact() -> None:
    # P(T=2)=0.3, P(T=3)=0.7. Over 2.25 @ 2.00: half-loss on 2 goals, win on 3.
    sp = asian_settlement(np.array([2, 3]), np.array([0.3, 0.7]), 1, -2.25)
    assert (sp.win, sp.half_loss) == pytest.approx((0.7, 0.3))
    assert sp.expected_value(2.0) == pytest.approx(1.0 * 0.7 - 0.5 * 0.3)
    # Fair odds give EV = 0; floor odds give EV = 1.5%.
    assert sp.expected_value(sp.fair_odds()) == pytest.approx(0.0, abs=1e-12)
    assert sp.expected_value(sp.min_acceptable_odds(0.015)) == pytest.approx(0.015)
    # Effective probability reproduces EV via p*o - 1.
    assert sp.effective_probability(2.0) * 2.0 - 1 == pytest.approx(sp.expected_value(2.0))
    # Whole line push removes stake from both sides of the equation.
    sp2 = asian_settlement(np.array([2, 3, 4]), np.array([0.2, 0.5, 0.3]), 1, -3.0)
    assert sp2.push == pytest.approx(0.5)
    assert sp2.fair_odds() == pytest.approx(1 + 0.2 / 0.3)


def test_binary_settlement_matches_simple_formula() -> None:
    sp = SettlementProbs(win=0.5, half_win=0, push=0, half_loss=0, loss=0.5)
    assert sp.min_acceptable_odds(0.015) == pytest.approx(min_acceptable_odds(0.5))


# --------------------------------------------------------------- Engine B
def test_bivariate_poisson_sums_and_marginals() -> None:
    m = bivariate_poisson_matrix(1.6, 1.1, 0.1, 20)
    assert m.sum() == pytest.approx(1.0)
    k = np.arange(20)
    assert (m.sum(axis=1) * k).sum() == pytest.approx(1.6, rel=1e-6)
    assert (m.sum(axis=0) * k).sum() == pytest.approx(1.1, rel=1e-6)
    cov = (np.outer(k, k) * m).sum() - 1.6 * 1.1
    assert cov == pytest.approx(0.1 * 1.1, rel=1e-4)


def _state(**kw: object) -> MatchState:
    base: dict[str, object] = dict(event_key="e", minute=0.0)
    base.update(kw)
    return MatchState(**base)  # type: ignore[arg-type]


RATINGS = PreMatchRatings(goal_supremacy=0.8, goal_total=2.8, corner_supremacy=1.5, corner_total=10.5)


def test_live_distributions_sum_to_one() -> None:
    model = LiveFootballModel()
    pred = model.predict(_state(minute=30, home_goals=1, home_corners=3, away_corners=2), RATINGS)
    for vals, pmf in (pred.total_goals_distribution(), pred.total_corners_distribution(), pred.goal_difference_distribution()):
        assert pmf.sum() == pytest.approx(1.0, abs=1e-9)
        assert vals.shape == pmf.shape
    assert sum(pred.h2h_probabilities()) == pytest.approx(1.0)
    # Totals cannot go below the current count.
    vals, pmf = pred.total_corners_distribution()
    assert vals.min() == 5
    # Over + under on a half line = 1.
    o = pred.settlement(MarketType.TOTAL_CORNERS, Selection.OVER, 9.5)
    u = pred.settlement(MarketType.TOTAL_CORNERS, Selection.UNDER, 9.5)
    assert o.win + u.win == pytest.approx(1.0)
    assert o.win == pytest.approx(pred.prob_over(MarketType.TOTAL_CORNERS, 9.5))


def test_kickoff_matches_prematch_expectation() -> None:
    p = LiveModelParams(first_half_stoppage=0, second_half_stoppage_base=0, stoppage_per_goal=0, stoppage_per_red=0)
    pred = LiveFootballModel(p).predict(_state(minute=0), RATINGS)
    assert pred.lam_home_goals + pred.lam_away_goals == pytest.approx(2.8)
    assert pred.mu_corners == pytest.approx(10.5)


def test_time_decay_and_stoppage() -> None:
    model = LiveFootballModel()
    early = model.predict(_state(minute=20, period=1), RATINGS)
    late = model.predict(_state(minute=80, period=2), RATINGS)
    over = model.predict(_state(minute=96, period=2, announced_stoppage=5), RATINGS)
    assert early.lam_home_goals > late.lam_home_goals > over.lam_home_goals
    assert over.lam_home_goals == pytest.approx(0.0, abs=1e-6)
    # Late-match intensity is higher than early-match per minute (slope > 0).
    w = model._w_integral
    assert w(80, 90, 0.3) > w(0, 10, 0.3)


def test_state_multipliers() -> None:
    model = LiveFootballModel()
    base = model.predict(_state(minute=65, period=2), RATINGS)
    trailing = model.predict(_state(minute=65, period=2, away_goals=1), RATINGS)
    assert trailing.multipliers["home_goals"] > 1.0
    assert trailing.mu_corners > base.mu_corners
    red = model.predict(_state(minute=65, period=2, home_red_cards=1), RATINGS)
    assert red.lam_home_goals < base.lam_home_goals
    assert red.lam_away_goals > base.lam_away_goals
    hot = model.predict(_state(minute=65, period=2, home_dangerous_attacks_10=40, home_shots_on_target_10=6), RATINGS)
    assert 1.0 < hot.multipliers["momentum_home"] <= model.p.momentum_max


def test_corner_negbin_overdispersion() -> None:
    model = LiveFootballModel(LiveModelParams(corner_dispersion=5.0))
    pred = model.predict(_state(minute=0), RATINGS)
    k = np.arange(pred.corner_pmf.size)
    mean = (k * pred.corner_pmf).sum()
    var = (k * k * pred.corner_pmf).sum() - mean**2
    assert var > mean  # overdispersed vs Poisson
    assert var == pytest.approx(mean + mean**2 / 5.0, rel=0.02)


def test_ratings_from_prices_roundtrip() -> None:
    r = ratings_from_prices(0.50, 0.26, 0.24, 0.52, 2.5)
    assert r.goal_supremacy > 0.3
    assert 2.4 < r.goal_total < 3.2


# ---------------------------------------------------------------- Steam
def test_steam_detector_relative_and_absolute() -> None:
    mk = MarketKey("e", MarketType.H2H)
    d = SteamDetector(window_s=60, mode="relative", rel_threshold=0.05)
    assert d.update((mk, Selection.HOME), 0.40, ts=0) is None
    assert d.update((mk, Selection.HOME), 0.41, ts=20) is None
    ev = d.update((mk, Selection.HOME), 0.425, ts=40)
    assert ev is not None and ev.rel_change == pytest.approx(0.0625)
    assert d.update((mk, Selection.HOME), 0.43, ts=45) is None  # cooldown
    # Outside the 60s window, old minimum is forgotten.
    d2 = SteamDetector(window_s=60, mode="absolute", abs_threshold_pp=0.03)
    d2.update((mk, Selection.AWAY), 0.30, ts=0)
    assert d2.update((mk, Selection.AWAY), 0.34, ts=100) is None
    assert d2.update((mk, Selection.AWAY), 0.371, ts=120) is not None


# ------------------------------------------------------- Signal engine
def _snap(bm: str, sel: Selection, price: float, market: MarketType = MarketType.H2H, line: float | None = None, live: bool = False, ts: float | None = None) -> OddsSnapshot:
    return OddsSnapshot("test", bm, "1", "Arsenal", "Chelsea", "England - Premier League",
                        datetime.now(timezone.utc) + timedelta(hours=2), market, line, sel, price, live,
                        ts if ts is not None else time.time(), event_key="ev1")


def test_engine_a_value_and_steam() -> None:
    eng = SignalEngine(EngineConfig(devig_method="power"))
    now = time.time()
    sharp = [_snap("pinnacle", Selection.HOME, 2.00, ts=now), _snap("pinnacle", Selection.DRAW, 3.60, ts=now), _snap("pinnacle", Selection.AWAY, 4.00, ts=now)]
    soft = [_snap("bet365", Selection.HOME, 2.00, ts=now), _snap("bet365", Selection.DRAW, 3.40, ts=now), _snap("bet365", Selection.AWAY, 3.75, ts=now)]
    assert eng.process(sharp + soft, now=now) == []
    # Pinnacle steams the home side 2.00 -> 1.85 within 30s; Bet365 stale at 2.00.
    moved = [_snap("pinnacle", Selection.HOME, 1.85, ts=now + 30), _snap("pinnacle", Selection.DRAW, 3.80, ts=now + 30), _snap("pinnacle", Selection.AWAY, 4.40, ts=now + 30), _snap("bet365", Selection.HOME, 2.00, ts=now + 30)]
    sigs = eng.process(moved, now=now + 30)
    assert len(sigs) == 1
    s = sigs[0]
    assert s.urgency is Urgency.STEAM and s.selection is Selection.HOME
    assert s.ev >= 0.025 and s.ev == pytest.approx(s.p_true * 2.0 - 1)
    assert s.min_acceptable_odds == pytest.approx(1.015 / s.p_true)
    # Re-processing the same price does not re-alert.
    assert eng.process([_snap("bet365", Selection.HOME, 2.00, ts=now + 31)], now=now + 31) == []
    assert "Arsenal" in format_signal(RiskManager().size(s))


def test_engine_b_fallback_live_corners() -> None:
    eng = SignalEngine(EngineConfig(engine_b_mode="fallback"))
    eng.set_ratings("ev1", RATINGS)
    eng.update_match_state(MatchState("ev1", minute=70, period=2, home_goals=0, away_goals=1, home_corners=6, away_corners=2))
    now = time.time()
    snaps = [_snap("bet365", Selection.OVER, 2.20, MarketType.TOTAL_CORNERS, 10.5, live=True, ts=now),
             _snap("bet365", Selection.UNDER, 1.62, MarketType.TOTAL_CORNERS, 10.5, live=True, ts=now)]
    sigs = eng.process(snaps, now=now)
    assert sigs, "favourite trailing late should make Over 10.5 corners +EV at 2.20"
    s = sigs[0]
    assert s.urgency is Urgency.LIVE_MODEL and s.selection is Selection.OVER
    assert s.ev == pytest.approx(s.p_true * s.bookmaker_odds - 1)


# ------------------------------------------------------------------ Risk
def test_risk_rounding_and_flags() -> None:
    cfg = RiskConfig(bankroll=2000, unit_size=10, max_stake_fraction=0.05)
    assert round_stake(37.4, cfg, 100) == 35
    assert round_stake(123.0, cfg, 200) == 120
    assert round_stake(98.0, cfg, 97) == 95  # never above cap
    assert round_stake(2.0, cfg, 100) == 0
    rm = RiskManager(cfg)
    eng = SignalEngine()
    eng.set_ratings("ev1", RATINGS)
    from quantbet.models import Signal
    sig = Signal(Urgency.VALUE, "A", "ev1", "A", "B", "Iceland - 2. Deild", MarketType.H2H, None, Selection.HOME, "bet365", 2.1, 0.52, 1 / 0.52, 1.015 / 0.52, 0.092, 0.0)
    rm.size(sig)
    assert sig.high_limit_risk
    assert sig.stake_currency % 5 == 0 and sig.stake_currency > 0
    assert sig.stake_units == sig.stake_currency / 10


# -------------------------------------------------------------- Database
@pytest.mark.asyncio
async def test_database_clv_and_report() -> None:
    from quantbet.models import Signal
    async with AlertDatabase(":memory:") as db:
        s = Signal(Urgency.VALUE, "A", "ev1", "A", "B", "Premier League", MarketType.H2H, None, Selection.HOME, "bet365", 2.10, 0.5, 2.0, 2.03, 0.05, 0.0125,
                   commence_time=datetime(2026, 1, 1, tzinfo=timezone.utc), stake_currency=50, stake_units=5)
        await db.insert_signal(s)
        due = await db.alerts_needing_close(datetime(2026, 1, 2, tzinfo=timezone.utc).isoformat())
        assert [d["id"] for d in due] == [s.signal_id]
        clv = await db.update_closing_line(s.signal_id, 2.0)
        assert clv == pytest.approx(0.05)
        assert await db.settle(s.signal_id, "win") == pytest.approx(55.0)
        rep = await db.report(2000)
        assert rep.yield_pct == pytest.approx(110.0)
        assert rep.avg_clv_pct == pytest.approx(5.0)
        assert rep.beat_close_pct == pytest.approx(100.0)


# --------------------------------------------------------------- Auditor
def test_auditor_whitelist_and_parse() -> None:
    rng = np.random.default_rng(1)
    closing, picks = [], []
    for i in range(600):
        d = date(2025, 1, 1) + timedelta(days=i)
        closing.append(ClosingRow(d, f"Team{i}A", f"Team{i}B", (2.0, 3.5, 4.0), None, None, None))
        fair = 1 / devig([2.0, 3.5, 4.0], "power")[0]
        picks.append(TipsterPick("sharp", d, f"Team{i}A", f"Team{i}B", MarketType.H2H, Selection.HOME, None, fair * rng.uniform(1.02, 1.08), 1.0, "win" if rng.random() < 0.5 else "loss"))
    res = TipsterAuditor(closing).audit(picks)
    assert res.n_matched == 600 and res.mean_clv > 1.02 and res.clv_p_value < 0.05
    assert res.verdict is TipsterVerdict.WHITELIST
    tip = parse_tip_text("Arsenal vs Chelsea Over 2.5 goals @ 1.95", "x")
    assert tip and tip.market is MarketType.TOTAL_GOALS and tip.line == 2.5 and tip.odds == 1.95
    tip2 = parse_tip_text("Inter v Milan away +0.25 AH @ 2.02", "x")
    assert tip2 and tip2.market is MarketType.ASIAN_HANDICAP and tip2.line == -0.25


# ---------------------------------------------------------------- Poller
def test_team_matching() -> None:
    assert normalize_team("Manchester United FC") == normalize_team("Man Utd")
    assert team_similarity("Bayern München", "Bayern Munich") == 100.0
    assert team_similarity("Arsenal", "Chelsea") < 60
    m = EventMatcher()
    ko = datetime(2026, 10, 25, 15, tzinfo=timezone.utc)
    k1 = m.match("a", "1", "Tottenham Hotspur", "Wolverhampton Wanderers", "EPL", ko)
    k2 = m.match("b", "x9", "Spurs", "Wolves", "Premier League", ko + timedelta(minutes=5))
    k3 = m.match("b", "x10", "Spurs", "Wolves", "Premier League", ko + timedelta(days=60))
    assert k1 == k2 and k3 != k1


def test_stats_tracker_rolling() -> None:
    t = StatsTracker(10)
    t.rolling("e", "da", 50, 30)
    t.rolling("e", "da", 55, 34)
    assert t.rolling("e", "da", 60, 40) == pytest.approx(10)


@pytest.mark.asyncio
async def test_token_bucket_rate() -> None:
    b = TokenBucket(rate=20, capacity=1)
    t0 = time.monotonic()
    for _ in range(5):
        await b.acquire()
    assert time.monotonic() - t0 >= 0.18
