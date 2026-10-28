"""v4.4-syndicate: Under maths, cards model, combos/boost, per-family dedup, sustained momentum,
separate freeze (v4.2 / v4.3-global untouched), CLI and the notifier blocks. Offline, no network."""

from __future__ import annotations

import json
import math
import shutil
import time
from pathlib import Path

import numpy as np
import pytest
from scipy.stats import poisson

from quantbet.engine import PreMatchRatings, SignalEngine, rules_preset
from quantbet.frozen import FrozenParamsError, enforce_frozen, read_frozen_set
from quantbet.global_layer import GlobalRatingsResolver, LeagueInfo, LeagueRegistry
from quantbet.models import LiveMatchState, MarketType, Selection
from quantbet.risk import RiskConfig, RiskManager
from quantbet.syndicate import (CardsInputs, SustainedMomentumTracker, SyndicateDeduplicator, SyndicateParams,
                                SyndicateSignalEngine, boost_required_odds, cards_inputs, cards_model, evaluate_combo,
                                frechet_bounds, joint_probability, leg_rho, load_priors, pairwise_joint,
                                poisson_total_settlement, price_card_lines, syndicate_family, tier_pricing)

ROOT = Path(__file__).resolve().parents[1]
V42_HASH = "40e806b910db93de34f9b26d9ebf361168077f1f38a8fe8d7dbc4e7cb6729aec"
V43_HASH = "971ab88662be2f23c0dd987484c7dcac1f36ed479bc4e650f99e60b0e1e9f77a"
TIER_KW = dict(tier_kelly_mult=(("TIER_1", 1.0), ("TIER_2", 1.0), ("TIER_3", 0.5)), tier_max_units=(("TIER_3", 15.0),))
R = PreMatchRatings(0.6, 2.9, 1.0, 10.0, 0.55, 0.2)
SP = SyndicateParams()


def _state(key="e1", m=35.0, hg=0, ag=0, hc=4, ac=3, hs=8, as_=6, hsot=3, asot=2, mh=1.0, ma=1.0, hf=None, af=None,
           hy=None, ay=None, t=None, slug="eng.1", league="Premier League") -> LiveMatchState:
    s = LiveMatchState(event_key=key, minute=m, home_goals=hg, away_goals=ag, home_corners=hc, away_corners=ac,
                       home_red_cards=0, away_red_cards=0, period=2 if m > 45 else 1,
                       updated_at=time.time() if t is None else t, home="Home FC", away="Away FC", league=league,
                       status="2nd half" if m > 45 else "1st half", stats_available=True, attack_momentum=0.5,
                       home_shots=hs, away_shots=as_, home_shots_on_target=hsot, away_shots_on_target=asot,
                       league_slug=slug, home_fouls=hf, away_fouls=af, home_yellow_cards=hy, away_yellow_cards=ay)
    s.momentum_mult_home, s.momentum_mult_away, s.momentum_mult_total = mh, ma, (mh + ma) / 2
    return s


class _Ratings:
    def ratings(self, h, a, league=None):  # noqa: ANN001, ANN201
        return R


def _engine() -> SyndicateSignalEngine:
    reg = LeagueRegistry({"eng.1": LeagueInfo("eng.1", "Premier League", tier="TIER_1")})
    pricing, live = rules_preset("v4.4-syndicate", None, bankroll=2000.0)
    rm = RiskManager(RiskConfig(bankroll=2000, unit_size=10, **TIER_KW))
    res = GlobalRatingsResolver(reg, None, _Ratings(), team_table_slugs=("eng.1",))
    return SyndicateSignalEngine(reg, rm, res, pricing, SignalEngine(live_params=live),
                                 sizer=lambda f, o: rm.stake_from_kelly(f), params=None,
                                 priors=load_priors(ROOT / "config/syndicate_priors.json"))


# -- Unders: lower tail -----------------------------------------------------------------------
@pytest.mark.parametrize("current,lam,line", [(0, 1.3, 0.5), (1, 2.1, 2.5), (9, 4.4, 12.5), (3, 0.4, 3.5)])
def test_under_is_lower_tail_and_complements_over_on_half_lines(current, lam, line):
    o = poisson_total_settlement(current, lam, line, Selection.OVER)
    u = poisson_total_settlement(current, lam, line, Selection.UNDER)
    assert o.win_weight + u.win_weight == pytest.approx(1.0, abs=1e-9)
    assert u.win_weight == pytest.approx(float(poisson.cdf(math.floor(line - current), lam)), abs=1e-9)
    assert u.win_weight == pytest.approx(o.loss_weight, abs=1e-12) and u.loss_weight == pytest.approx(o.win_weight, abs=1e-12)


def test_unders_are_half_lines_only_and_bullish_suppresses():
    eng = _engine()
    t0 = 1_000_000.0
    for i, m in enumerate(range(20, 40)):  # high pace: bullish
        s = _state("b", m, hc=int(m * 0.25), ac=int(m * 0.2), hs=int(m * 0.6), as_=int(m * 0.45), hsot=int(m * 0.2),
                   asot=int(m * 0.15), mh=1.08, ma=1.05, t=t0 + i * 60)
        eng.on_match_state(s, s.updated_at)
    tr = eng.last_trace["b"]
    assert eng.last_momentum["b"].bullish
    unders = {k: v for k, v in tr.items() if k[2] is Selection.UNDER}
    assert unders
    for (mk, line, _sel), why in unders.items():
        if abs(line * 2 - round(line * 2)) > 1e-9 or abs(line - math.floor(line) - 0.5) > 1e-9:
            assert why == "under: half lines only", (mk, line, why)
        else:
            assert why == "bullish: suppresses Unders", (mk, line, why)


def test_bus_parking_under_alert_example():
    eng = _engine()
    t0 = 2_000_000.0
    out = []
    for i, m in enumerate(range(40, 86)):
        d = max(0, m - 40)
        s = _state("bus", m, hg=1, ag=0, hc=4, ac=3 + int(d * 0.10), hs=8, as_=6 + int(d * 0.25), hsot=3,
                   asot=2 + int(d * 0.06), mh=0.85, ma=0.85, t=t0 + i * 60)
        for sg in eng.on_match_state(s, s.updated_at):
            eng.commit(sg)
            out.append(sg)
    unders = [x for x in out if x.selection is Selection.UNDER]
    assert unders and all(55 <= x.minute <= 85 for x in unders)
    u = unders[0]
    assert u.market is MarketType.TOTAL_GOALS and u.line % 1 == 0.5
    assert "bus_parking" in u.extra["triggers"] and u.extra["rules"] == "v4.4-syndicate"
    assert u.extra["min_odds"] * u.p_true - 1 >= u.extra["syndicate"]["hurdle"] - 0.01


# -- cards ------------------------------------------------------------------------------------
def test_cards_missing_fields_give_no_inputs_and_no_cards_signal():
    assert cards_inputs(_state(hf=10, af=9, hy=None, ay=1)) is None
    assert cards_inputs(_state(hf=None, af=9, hy=1, ay=1)) is None
    ci = cards_inputs(_state(hf=10, af=9, hy=1, ay=2))
    assert ci is not None and ci.cards == 3 and ci.fouls == 19
    eng = _engine()
    t0 = 3_000_000.0
    for i, m in enumerate(range(20, 44)):
        s = _state("nc", m, hf=int(m * 0.4), af=int(m * 0.4), hy=None, ay=None, t=t0 + i * 60)
        sigs = eng.on_match_state(s, s.updated_at)
        assert not [x for x in sigs if x.market is MarketType.TOTAL_CARDS]
        assert eng.last_cards["nc"] is None
        assert not [k for k in eng.last_trace["nc"] if k[0] is MarketType.TOTAL_CARDS]


def test_cards_red_counts_one_and_yellow_counts_one():
    """The frozen v4.4-syndicate count (yellow 1, red 1) - kept so v4.4 replays exactly.
    v4.5-sharp (the new default rule of cards_inputs) counts Bet365 points: tests/test_sharp_v45.py."""
    s = _state(hf=5, af=5, hy=2, ay=1)
    s.home_red_cards = 1
    assert cards_inputs(s, "y1_r1").cards == 4
    assert cards_inputs(s).cards == 5  # bet365 rule: 2 + 1 yellows + 2 x 1 red


def test_cards_urgency_only_close_games_in_60_85():
    base = dict(cards90=4.3, fouls90=24.0, params=SP)
    close = cards_model(CardsInputs(62.0, 2, 3, 26, 1), **base)
    wide = cards_model(CardsInputs(62.0, 2, 3, 26, 2), **base)
    late = cards_model(CardsInputs(86.0, 2, 3, 36, 0), **base)
    assert close.urgency > 1.0 and close.urgency <= SP.cards_urgency_mult + 1e-9
    assert wide.urgency == pytest.approx(1.0) and late.urgency == pytest.approx(1.0)
    assert close.lam_remaining > wide.lam_remaining
    first = cards_model(CardsInputs(30.0, 1, 1, 8, 0), **base)
    assert 1.0 < first.urgency < close.urgency  # only the 60'-85' slice of the remainder is boosted


def test_cards_model_on_pace_is_near_prior_and_factors_clip():
    on = cards_model(CardsInputs(45.0, 1, 1, 12, 0), 4.3, 24.0, SP, h2_share=0.68)
    assert 0.85 < on.foul_factor < 1.15 and 0.8 < on.booking_factor < 1.3
    hot = cards_model(CardsInputs(30.0, 1, 9, 60, 3), 4.3, 24.0, SP)
    assert hot.net <= SP.cards_factor_clip[1] + 1e-9
    cold = cards_model(CardsInputs(70.0, 2, 0, 0, 3), 4.3, 24.0, SP)
    assert cold.net >= SP.cards_factor_clip[0] - 1e-9


def test_card_lines_are_half_lines_both_sides():
    res = cards_model(CardsInputs(50.0, 2, 3, 22, 0), 4.3, 24.0, SP)
    pricing, _ = rules_preset("v4.4-syndicate", None)
    lps = price_card_lines(res, tier_pricing(pricing, SP, "TIER_1"), None)
    assert lps and all(lp.line % 1 == 0.5 and lp.market is MarketType.TOTAL_CARDS for lp, _ in lps)
    assert {lp.selection for lp, _ in lps} == {Selection.OVER, Selection.UNDER}
    assert syndicate_family(MarketType.TOTAL_CARDS) == "cards"


# -- combos -------------------------------------------------------------------------------------
def test_pairwise_rho_zero_is_independent_and_frechet_clipped():
    assert pairwise_joint(0.55, 0.62, 0.0) == pytest.approx(0.55 * 0.62)
    assert pairwise_joint(0.9, 0.85, 1.0) == pytest.approx(0.85)  # upper bound min(PA, PB)
    assert pairwise_joint(0.3, 0.4, -1.0) == pytest.approx(0.0)  # lower bound max(0, PA+PB-1)
    assert pairwise_joint(0.8, 0.7, -1.0) == pytest.approx(0.5)
    assert frechet_bounds([0.8, 0.7, 0.9]) == (pytest.approx(0.4), 0.7)


def test_three_leg_copula_independent_bounded_and_seeded():
    ps = [0.55, 0.6, 0.5]
    pj, method = joint_probability(ps, np.eye(3))
    assert method == "independent" and pj == pytest.approx(np.prod(ps))
    R = np.array([[1, 0.2, -0.1], [0.2, 1, 0.15], [-0.1, 0.15, 1.0]])
    a, m1 = joint_probability(ps, R, draws=50_000, seed=7)
    b, _ = joint_probability(ps, R, draws=50_000, seed=7)
    assert m1 == "gaussian_copula_mc" and a == b
    lo, hi = frechet_bounds(ps)
    assert lo <= a <= hi
    tiny, _ = joint_probability(ps, np.full((3, 3), 1e-6) + np.eye(3) * (1 - 1e-6), draws=200_000)
    assert tiny == pytest.approx(np.prod(ps), abs=0.004)


def test_evaluate_combo_min_odds_and_boost_formula():
    legs = [dict(family="goals", selection=Selection.OVER, line=2.5, p=0.55, label="Over 2.5 Goals"),
            dict(family="corners", selection=Selection.OVER, line=10.5, p=0.6, label="Over 10.5 Corners")]
    c = evaluate_combo(legs, {}, 0.04, SP)
    assert c["fair_odds"] == pytest.approx(1 / 0.33, abs=1e-3) and c["naive_odds"] == c["fair_odds"]
    assert c["min_odds"] == pytest.approx(math.ceil(1.04 / 0.33 * 100) / 100)
    assert c["boost_required_odds"] == pytest.approx(math.ceil((1 + (c["min_odds"] - 1) / 1.25) * 100) / 100)
    assert boost_required_odds(3.5) == pytest.approx(3.0) and boost_required_odds(2.0, 0.25) == pytest.approx(1.8)
    assert leg_rho("goals", Selection.OVER, "corners", Selection.UNDER, {"goals|corners": 0.1}) == pytest.approx(-0.1)
    assert leg_rho("corners", Selection.OVER, "goals", Selection.OVER, {"goals|corners": 0.1}) == pytest.approx(0.1)


# -- dedup ---------------------------------------------------------------------------------------
def test_dedup_cooldown_material_change_and_caps():
    d = SyndicateDeduplicator(SP, bankroll=2000.0)
    assert d.check("m", "goals", "over", 2.5, 20, 31.0, None) is None
    d.record("m", "goals", "over", 2.5, 0.52, 20, 31.0)
    assert d.check("m", "goals", "over", 2.5, 20, 40.0, 0.60) == "cooldown"
    assert d.check("m", "goals", "under", 2.5, 20, 50.0, 0.40) == "family_conflict"
    assert d.check("m", "goals", "over", 2.5, 20, 47.0, 0.54) == "no_material_change"
    assert d.check("m", "goals", "over", 2.5, 20, 47.0, 0.56) is None  # >= 4pp shift
    assert d.check("m", "goals", "over", 3.0, 20, 47.0, 0.53) is None  # line moved 0.5
    d.record("m", "goals", "over", 3.0, 0.5, 20, 47.0)
    assert d.check("m", "goals", "over", 3.5, 20, 70.0, 0.6) == "family_cap"
    d.record("m", "corners", "over", 10.5, 0.5, 20, 33.0)
    assert d.check("m", "cards", "over", 4.5, 20, 60.0, None) == "match_signal_cap"
    d2 = SyndicateDeduplicator(SP, bankroll=2000.0)  # 5% of 2000 = 100
    d2.record("x", "goals", "over", 2.5, 0.5, 60, 31.0)
    assert d2.check("x", "corners", "over", 10.5, 45, 33.0, None) == "stake_cap"
    assert d2.remaining_stake("x") == pytest.approx(40.0)


def test_engine_respects_cooldown_between_polls():
    eng = _engine()
    t0 = 4_000_000.0
    fired = []
    for i, m in enumerate(range(20, 44)):
        s = _state("cd", m, hc=int(m * 0.13), ac=int(m * 0.104), hs=int(m * 0.5), as_=int(m * 0.35),
                   hsot=int(m * 0.18), asot=int(m * 0.12), mh=1.10, ma=1.07, t=t0 + i * 60)
        for sg in eng.on_match_state(s, s.updated_at):
            eng.commit(sg)
            fired.append(sg)
    assert fired, "the sample Over scenario must alert"
    fams = {}
    for sg in fired:
        fam = sg.extra["dedup"]["family"]
        if fam in fams:
            assert sg.minute - fams[fam] >= SP.cooldown_min
        fams[fam] = sg.minute
    sg = fired[0]
    assert sg.selection is Selection.OVER and "sustained_momentum" in sg.extra["triggers"]
    assert "activity_high" not in sg.extra["triggers"]
    assert sg.extra["boost_target"]["pre_boost_odds"] == pytest.approx(
        math.ceil((1 + (sg.extra["min_odds"] - 1) / 1.25) * 100) / 100)
    assert sg.extra["lift"]["lift"] <= SP.max_lift + 1e-6


# -- sustained momentum -----------------------------------------------------------------------------
def _feed(tr, key, minutes, t0, rate=1.0, step_s=60.0):
    v = None
    for i, m in enumerate(minutes):
        s = _state(key, m, hc=int(m * 0.25 * rate), ac=int(m * 0.2 * rate), hs=int(m * 0.6 * rate),
                   as_=int(m * 0.45 * rate), hsot=int(m * 0.2 * rate), asot=int(m * 0.15 * rate))
        v = tr.update(s, R, None, now=t0 + i * step_s)
    return v


def test_sustained_momentum_needs_persistence_and_polls():
    tr = SustainedMomentumTracker(SP)
    v = _feed(tr, "k", range(20, 30), 0.0)  # span reaches 9' at 29' -> first assessable, not yet persistent
    assert v.reading.assessable and not v.sustained_over
    v = _feed(tr, "k", range(30, 34), 600.0)
    assert v.polls >= SP.momentum_polls and v.reading.adj_index >= SP.sustained_over_index and v.sustained_over
    assert "activity_high" not in v.labels() and "sustained_momentum" in v.labels()


def test_sustained_momentum_resets_on_wall_clock_gap_and_clock_back():
    tr = SustainedMomentumTracker(SP)
    v = _feed(tr, "g", range(20, 36), 0.0)
    assert v.sustained_over
    v = _feed(tr, "g", [36.0], 15 * 60 + SP.gap_reset_s + 10)  # suspend/resume gap -> history cleared
    assert not v.reading.assessable and not v.sustained_over
    tr2 = SustainedMomentumTracker(SP)
    _feed(tr2, "c", range(20, 36), 0.0)
    v = _feed(tr2, "c", [30.0], 16 * 60)  # clock went backwards -> restart
    assert not v.reading.assessable and not v.sustained_over


def test_low_pace_is_not_sustained_and_lead_two_is_penalised():
    tr = SustainedMomentumTracker(SP)
    v = _feed(tr, "l", range(20, 36), 0.0, rate=0.4)
    assert not v.sustained_over
    tr2 = SustainedMomentumTracker(SP)
    for i, m in enumerate(range(20, 36)):
        s = _state("two", m, hg=2, ag=0, hc=int(m * 0.25), ac=int(m * 0.2), hs=int(m * 0.6), as_=int(m * 0.45),
                   hsot=int(m * 0.2), asot=int(m * 0.15))
        v2 = tr2.update(s, R, None, now=i * 60.0)
    assert v2.reading.state_weight == pytest.approx(SP.w_lead_two)
    assert v2.reading.adj_index == pytest.approx(v2.reading.index * SP.w_lead_two)


# -- freeze ------------------------------------------------------------------------------------------
def _settings(path, rules="v4.4-syndicate"):
    from quantbet.config import Settings
    return Settings(_env_file=None, alert_rules=rules, engine_b_params_path=path,  # type: ignore[call-arg]
                    leagues_config_path=ROOT / "config/leagues.json",
                    syndicate_priors_path=ROOT / "config/syndicate_priors.json")


def test_repo_freeze_has_v44_entry_matching_config_and_older_hashes_unchanged():
    from quantbet.main import build_rules
    path = ROOT / "config/engine_b_params.json"
    raw = json.loads(path.read_text())
    assert raw["sha256"] == V42_HASH and raw["rules_version"] == "v4.2"
    assert read_frozen_set(path, "v4.3-global")["sha256"] == V43_HASH
    fs = read_frozen_set(path, "v4.4-syndicate")
    assert fs is not None and fs["sha256"] not in (V42_HASH, V43_HASH)
    assert fs["global"]["syndicate"]["params"]["tier_target_edge"] == {"TIER_1": 0.04, "TIER_2": 0.055, "TIER_3": 0.07}
    assert build_rules(_settings(path))[2] == fs["sha256"]
    assert build_rules(_settings(path, "v4.3-global"))[2] == V43_HASH
    assert build_rules(_settings(path, "v4.2"))[2] == V42_HASH


def test_v44_freeze_tamper_raises(tmp_path):
    from quantbet.main import build_rules, global_params_dict
    path = tmp_path / "p.json"
    shutil.copy(ROOT / "config/engine_b_params.json", path)
    s = _settings(path)
    pr, lv, h = build_rules(s)
    gp = global_params_dict(s)
    bad = json.loads(json.dumps(gp))
    bad["syndicate"]["params"]["cooldown_min"] = 5.0
    with pytest.raises(FrozenParamsError):
        enforce_frozen(path, "v4.4-syndicate", pr, lv, global_params=bad)
    raw = json.loads(path.read_text())
    raw["frozen_sets"]["v4.4-syndicate"]["pricing"]["target_edge"] = 0.02
    path.write_text(json.dumps(raw))
    with pytest.raises(FrozenParamsError):
        build_rules(s)


def test_v44_pricing_preset_and_defaults():
    p, _ = rules_preset("v4.4-syndicate", None)
    assert p.allow_unders and p.max_signals_per_family == 2 and p.max_signals_per_match == 3
    assert p.max_match_stake_frac == 0.05 and p.family_cooldown_min == 15.0
    t3 = tier_pricing(p, SP, "TIER_3")
    assert t3.target_edge == 0.07 and t3.abort_edge == pytest.approx(0.042)
    assert SP.lift_required("TIER_1") == pytest.approx(1.04 / 0.97)


def test_cli_accepts_v44_and_v45_is_the_default():
    from quantbet.config import Settings
    from quantbet.main import parse_args, settings_from_args
    assert parse_args(["--dry-run", "--rules", "v4.4-syndicate"]).rules == "v4.4-syndicate"
    for old in ("v4.3-global", "v4.2"):
        assert parse_args(["--dry-run", "--rules", old]).rules == old
    ok = Settings(_env_file=None).model_copy(update={"telegram_bot_token": "x", "telegram_chat_id": "1"})
    assert settings_from_args(parse_args(["--live"]), ok).alert_rules == "v4.5-sharp"  # v4.5 superseded v4.4
    assert settings_from_args(parse_args(["--live", "--rules", "v4.4-syndicate"]), ok).alert_rules == "v4.4-syndicate"


def test_workflow_keeps_v44_selectable():
    wf = (ROOT / ".github/workflows/live_quantbet.yml").read_text()
    assert "default: v4.5-sharp" in wf.replace('"', "").replace("'", "")  # v4.5-sharp is the new default
    assert "v4.4-syndicate" in wf and "v4.3-global" in wf and "v4.2" in wf
    assert "inputs.rules || 'v4.5-sharp'" in wf


# -- notifier -----------------------------------------------------------------------------------------
def test_card_shows_boost_target_and_bet_builder_blocks():
    from quantbet.notifier import format_min_odds_signal
    eng = _engine()
    t0 = 5_000_000.0
    sig = None
    for i, m in enumerate(range(20, 44)):
        s = _state("nb", m, hc=int(m * 0.1), ac=int(m * 0.1), hs=int(m * 0.25), as_=int(m * 0.25),
                   hsot=int(m * 0.08), asot=int(m * 0.08), hf=int(m * 0.25), af=int(m * 0.25), hy=int(m * 0.045),
                   ay=int(m * 0.04), t=t0 + i * 60)
        for sg in eng.on_match_state(s, s.updated_at):
            eng.commit(sg)
            sig = sig or sg
    assert sig is not None and sig.market is MarketType.TOTAL_CARDS
    txt = format_min_odds_signal(sig)
    assert "[BOOST TARGET]" in txt and "[BET BUILDER / 3-WAY COMBO]" in txt
    assert "MINIMUM BET365 ODDS TO BET" in txt and "Cards" in txt
    assert "Minimum Bet365 Combo Odds" in txt
    for c in sig.extra["combos"]:
        assert c["max_stake"] <= eng.sdedup.remaining_stake("nb") + 1e-9
