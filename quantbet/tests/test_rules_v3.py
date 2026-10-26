"""v3 alert rules: de-correlation, dedup/caps, minute windows, shrinkage, dampener, fav_chasing, chasing corners."""

from __future__ import annotations

import math
import time

import pytest

from quantbet.engine import (
    AlertDeduplicator,
    FreeSignalEngine,
    LiveFootballModel,
    LiveModelParams,
    PreMatchRatings,
    PricingConfig,
    RatingsResolver,
    SharpPrices,
    assess_confidence,
    expected_log_growth,
    favourite_chasing,
    market_family,
    pre_match_win_probs,
    price_live_lines,
)
from quantbet.fotmob_page import fotmob_dominance
from quantbet.models import LiveMatchState, MarketType, Selection
from quantbet.risk import RiskManager

R_EVEN = PreMatchRatings(0.0, 2.7, 0.0, 10.0)
R_FAV = PreMatchRatings(1.0, 3.0, 1.0, 10.5, 0.62, 0.17)  # home favourite, 62 % pre-match


class _Table:
    def ratings(self, home, away, league=None):  # noqa: ANN001
        return R_FAV


def _st(minute=35.0, hg=0, ag=0, hc=3, ac=2, mh=1.15, ma=1.0, period=None, reds=(0, 0), **kw) -> LiveMatchState:
    st = LiveMatchState(event_key=kw.pop("key", "e1"), minute=minute, home_goals=hg, away_goals=ag, home_corners=hc,
                        away_corners=ac, home_red_cards=reds[0], away_red_cards=reds[1],
                        period=period or (2 if minute > 45 else 1), updated_at=kw.pop("t", time.time()),
                        home="Home FC", away="Away FC", league="Premier League", status="2nd half" if minute > 45 else "1st half",
                        stats_available=True, attack_momentum=kw.pop("am", 0.5), **kw)
    st.momentum_mult_home, st.momentum_mult_away, st.momentum_mult_total = mh, ma, (mh + ma) / 2
    return st


def _engine(pricing: PricingConfig | None = None, params: LiveModelParams | None = None) -> FreeSignalEngine:
    from quantbet.engine import SignalEngine

    rm = RiskManager()
    return FreeSignalEngine(RatingsResolver(None, _Table()), pricing or PricingConfig(), SignalEngine(live_params=params) if params else None,
                            sizer=lambda f, o: rm.stake_from_kelly(f))


# -- rule 1: single best line, cooldown, caps ---------------------------------
def test_single_best_line_per_family_by_log_growth():
    fe = _engine()
    now = time.time()
    sigs = fe.on_match_state(_st(minute=35, t=now), now)
    fams = [market_family(s.market) for s in sigs]
    assert sigs and len(fams) == len(set(fams)) <= 2  # never Over 7.25 AND Over 7.5 together
    trace = fe.last_trace["e1"]
    prices = {(lp.market, lp.line, lp.selection): lp for lp in fe.last_prices["e1"]}
    for s in sigs:
        best = s.extra["log_growth"]
        rivals = [expected_log_growth(prices[k]) for k, why in trace.items()
                  if why == "not_best" and market_family(k[0]) == market_family(s.market)]
        assert all(best >= g - 1e-12 for g in rivals)


def test_expected_log_growth_formula():
    from quantbet.engine import LinePrice, SettlementProbs

    sp = SettlementProbs(0.5, 0.0, 0.0, 0.0, 0.5)
    lp = LinePrice(MarketType.TOTAL_GOALS, Selection.OVER, 2.5, sp, 0.5, 2.0, 2.1, 2.05, 0.025, 0.015)
    f = 0.25 * ((1.1 * 0.5 - 0.5) / 1.1)
    g = expected_log_growth(lp, 2.1, f)
    assert math.isclose(g, 0.5 * math.log(1 + f * 1.1) + 0.5 * math.log(1 - f), rel_tol=1e-12) and g > 0


def test_dedup_cooldown_family_cap_and_state_change_exception():
    d = AlertDeduplicator(PricingConfig())
    sig0 = (0, 0, 0, 0)
    assert d.check("m", "corners", 9.5, 20, 60, sig0) is None
    d.record("m", "corners", 9.5, 20, 60, sig0)
    assert d.check("m", "corners", 10.5, 20, 65, sig0) == "cooldown"  # inside 15'
    assert d.check("m", "corners", 10.5, 20, 76, sig0) == "family_cap"  # cooldown over, 1 per family
    goal = (1, 0, 0, 0)
    assert d.check("m", "corners", 10.0, 20, 62, goal) == "cooldown"  # goal but line gap < 1.0
    assert d.check("m", "corners", 10.5, 20, 62, goal) is None  # goal AND gap >= 1.0
    assert d.check("m", "goals", 2.5, 20, 62, sig0) is None  # other family independent
    d.record("m", "goals", 2.5, 20, 62, sig0)
    assert d.check("x", "goals", 2.5, 20, 62, sig0) is None  # other match independent


def test_dedup_match_caps():
    d = AlertDeduplicator(PricingConfig(bankroll=2000, max_match_stake_frac=0.05))
    s0 = (0, 0, 0, 0)
    d.record("m", "goals", 2.5, 60, 35, s0)
    assert d.check("m", "corners", 9.5, 45, 35, s0) == "stake_cap"  # 60 + 45 > 100
    assert d.check("m", "corners", 9.5, 40, 35, s0) is None
    d.record("m", "corners", 9.5, 40, 35, s0)
    # third signal only via the state-change exception, and still under the 5 % cap
    assert d.check("m", "goals", 3.5, 10, 70, s0) == "family_cap"
    assert d.check("m", "goals", 3.5, 10, 70, (1, 0, 0, 0)) == "stake_cap"


def test_engine_respects_cooldown_on_next_poll():
    fe = _engine()
    now = time.time()
    first = fe.on_match_state(_st(minute=35, t=now), now)
    again = fe.on_match_state(_st(minute=38, t=now + 180), now + 180)
    fams_first = {market_family(s.market) for s in first}
    assert first and not ({market_family(s.market) for s in again} & fams_first)  # locked families stay silent
    assert set(fe.last_trace["e1"].values()) & {"cooldown", "family_cap", "match_signal_cap", "stake_cap"}


# -- rule 2: minute windows + shrinkage ---------------------------------------
@pytest.mark.parametrize("minute,ok", [(25, False), (30, True), (43, True), (44, False), (50, False), (55, True),
                                        (85, True), (86, False)])
def test_minute_windows(minute, ok):
    st = _st(minute=minute)
    rep = assess_confidence(st, "football_data", PricingConfig(), R_EVEN, time.time())
    assert (not any("outside windows" in r for r in rep.reasons)) is ok


def test_red_card_alerts_follow_the_windows():
    st = _st(minute=50, reds=(1, 0), am=0.0, mh=1.0, ma=1.0)
    rep = assess_confidence(st, "football_data", PricingConfig(), R_EVEN, time.time())
    assert "red_card" in rep.triggers and not rep.ok
    legacy = assess_confidence(st, "football_data", PricingConfig.legacy(), R_EVEN, time.time())
    assert legacy.ok


def test_shrinkage_weight_and_blend():
    m = LiveFootballModel()
    st = _st(minute=45, hg=3, ag=0, hc=6, ac=0, period=1)
    base = m.shrunk_rates(st, R_EVEN)
    w = (45 / 90) * 0.5
    assert math.isclose(base["home_goals"], w * 3 / 45 * 90 + (1 - w) * R_EVEN.home_goals)
    assert math.isclose(base["away_corners"], (1 - w) * R_EVEN.away_corners)
    legacy = LiveFootballModel(LiveModelParams.legacy()).shrunk_rates(st, R_EVEN)
    assert legacy["home_goals"] == R_EVEN.home_goals
    early = m.shrunk_rates(_st(minute=15, hg=2, ag=0), R_EVEN)
    assert early["home_goals"] < 0.25 * 12 + 1  # 15' burst gets only w = 0.083


# -- rule 3: dampener + fav_chasing -------------------------------------------
def test_comfortable_lead_dampener():
    m = LiveFootballModel(LiveModelParams(game_state_dampener=True))
    m0 = LiveFootballModel(LiveModelParams(game_state_dampener=False))
    st = _st(minute=60, hg=2, ag=0, mh=1.0, ma=1.0)
    a, b = m.multipliers(st, R_EVEN), m0.multipliers(st, R_EVEN)
    assert math.isclose(a["home_goals"], b["home_goals"] * 0.82) and math.isclose(a["away_goals"], b["away_goals"] * 0.82)
    assert math.isclose(a["home_corners"], b["home_corners"] * 0.85) and math.isclose(a["away_corners"], b["away_corners"])
    st1 = _st(minute=60, hg=1, ag=0, mh=1.0, ma=1.0)
    assert m.multipliers(st1, R_EVEN) == m0.multipliers(st1, R_EVEN)


def test_fav_chasing_needs_55pct_favourite_tied_or_one_down_after_55():
    p = LiveModelParams()
    assert favourite_chasing(_st(minute=56, hg=0, ag=0), R_FAV, p) == ("home", 0)
    assert favourite_chasing(_st(minute=56, hg=0, ag=1), R_FAV, p) == ("home", -1)
    assert favourite_chasing(_st(minute=56, hg=0, ag=2), R_FAV, p) is None  # 2 down: no boost
    assert favourite_chasing(_st(minute=54, hg=0, ag=1), R_FAV, p) is None
    weak = PreMatchRatings(0.7, 3.0, 0.0, 10.0, 0.50, 0.25)  # supremacy >= 0.6 but only 50 %
    assert favourite_chasing(_st(minute=70, hg=0, ag=1), weak, p) is None
    assert favourite_chasing(_st(minute=70, hg=0, ag=1), weak, LiveModelParams.legacy()) == ("home", -1)
    # no market prior -> derived from ratings
    ph, pa = pre_match_win_probs(PreMatchRatings(1.0, 3.0, 0.0, 10.0))
    assert ph > 0.55 > pa


def test_resolver_keeps_prior_win_probs():
    prior = SharpPrices(1.60, 4.2, 5.5)
    r, src = RatingsResolver(None, None, prior_lookup=lambda k: (prior, "espn_draftkings")).resolve("k", "A", "B", "Serie A")
    assert src == "espn_draftkings" and r.home_win_prob and r.home_win_prob > 0.55 and r.away_win_prob < 0.2


# -- rule 4: chasing corners ------------------------------------------------
def test_chasing_corner_penalty_on_high_corner_overs_only():
    st = _st(minute=70, hg=0, ag=1, hc=6, ac=1, mh=1.0, ma=1.0)
    cfg = PricingConfig()
    # margin 0 so the threshold (current + expected remaining + margin) falls inside the priced ladder
    on = {(lp.market, lp.line, lp.selection): lp for lp in
          price_live_lines(st, R_FAV, cfg, LiveFootballModel(LiveModelParams(chasing_corner_line_margin=0.0)))}
    off = {(lp.market, lp.line, lp.selection): lp for lp in
           price_live_lines(st, R_FAV, cfg, LiveFootballModel(LiveModelParams(chasing_corner_penalty=False)))}
    pen = [k for k, lp in on.items() if lp.notes]
    assert pen and all(k[0] is MarketType.TOTAL_CORNERS and k[1] > 7 + 3.0 and k[2] is Selection.OVER for k in pen)
    for k in pen:
        if k in off:
            assert on[k].p_true < off[k].p_true
    low = (MarketType.TOTAL_CORNERS, 9.5, Selection.OVER)
    if low in on and not on[low].notes:
        assert math.isclose(on[low].p_true, off[low].p_true)
    # not trailing by exactly 1 -> no penalty
    tied = price_live_lines(_st(minute=70, hg=1, ag=1, hc=6, ac=1), R_FAV, cfg,
                            LiveFootballModel(LiveModelParams(chasing_corner_line_margin=0.0)))
    assert not any(lp.notes for lp in tied)


def test_corner_over_after_75_requires_fotmob_dominance():
    fe = _engine(PricingConfig(minute_windows=((30, 90),)))
    now = time.time()
    sigs = fe.on_match_state(_st(minute=78, hg=0, ag=1, hc=6, ac=1, mh=1.2, ma=0.95, t=now), now)
    corner = [s for s in sigs if s.market is MarketType.TOTAL_CORNERS and s.selection is Selection.OVER]
    assert corner and all(s.extra["fotmob_required"] == "dominance" and s.extra["dominant_side"] == "home" for s in corner)
    assert fotmob_dominance("home", 1.5, 0.2, 18, 2) == "confirmed"
    assert fotmob_dominance("home", 1.5, 0.2, 5, 9) == "contradicted"
    assert fotmob_dominance("away", 1.5, 0.2, 18, 2) == "contradicted"
    assert fotmob_dominance("home", None, 0.2, 18, 2) == "unavailable"


def test_auto_commit_off_defers_dedup_until_commit():
    fe = _engine()
    fe.auto_commit = False
    now = time.time()
    a = fe.on_match_state(_st(minute=35, t=now), now)
    b = fe.on_match_state(_st(minute=36, t=now + 60), now + 60)
    assert a and b  # nothing committed (e.g. FotMob suppressed) -> family not locked
    for s in b:
        fe.commit(s)
    assert not fe.on_match_state(_st(minute=37, t=now + 120), now + 120)


def test_legacy_presets_restore_old_policy():
    fe = _engine(PricingConfig.legacy(), LiveModelParams.legacy())
    now = time.time()
    sigs = fe.on_match_state(_st(minute=35, t=now), now)
    per_market = {}
    for s in sigs:
        per_market[s.market] = per_market.get(s.market, 0) + 1
    assert sigs and max(per_market.values()) == 2  # two adjacent lines per market (the old stacking)
