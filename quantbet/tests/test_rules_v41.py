"""v4.1: positive-lift gate for Overs, Under support, re-anchoring on the current score, Asian Under
settlement, blowout/caps for Unders, presets, freeze with superseded v4 hash, CLI default, notifier."""

from __future__ import annotations

import dataclasses
import json
import time

import numpy as np
import pytest

from quantbet.engine import (
    LiveFootballModel, PricingConfig, lift_gate_reason, price_live_lines, rules_preset, settle_bet,
)
from quantbet.frozen import FrozenParamsError, enforce_frozen, param_set, params_hash, write_frozen
from quantbet.models import MarketType, Selection
from test_rules_v4 import R_EVEN, R_FAV, _engine, _st

G, C = MarketType.TOTAL_GOALS, MarketType.TOTAL_CORNERS


def _run(st, version="v4.1", now=None):
    fe = _engine(version)
    now = now or st.updated_at
    return fe, fe.on_match_state(st, now)


def _check_emitted_respect_gate(fe, sigs):
    for s in sigs:
        lift = s.extra["lift"]
        assert lift["prior_fair"] is not None
        if s.selection is Selection.OVER:
            assert lift["net_multiplier"] >= 1.06 and s.fair_odds <= lift["prior_fair"] / 1.06 + 1e-9
        else:
            assert lift["net_multiplier"] < 1.0 and s.fair_odds <= lift["prior_fair"] / 1.06 + 1e-9
        assert 1.75 <= s.fair_odds <= 2.20


# -- 1. positive-lift gate ---------------------------------------------------------
def test_marseille_case_no_over_when_model_is_bearish_unders_instead():
    """35' 2-0, no momentum: |diff|=2 dampener -> lambda_model < lambda_prior -> never an Over."""
    st = _st(minute=35, hg=2, ag=0, hc=2, ac=1, mh=1.0, ma=1.0)
    fe, sigs = _run(st)
    lift = fe.last_lift["e1"]
    assert lift["goals"]["net"] < 1.0 and lift["corners"]["net"] < 1.0
    assert not [s for s in sigs if s.selection is Selection.OVER]
    overs = [w for k, w in fe.last_trace["e1"].items() if k[2] is Selection.OVER]
    assert overs and all(w.startswith("lift: net multiplier <= 1.05") for w in overs)
    assert {s.selection for s in sigs} == {Selection.UNDER} and {s.market for s in sigs} == {G, C}
    _check_emitted_respect_gate(fe, sigs)
    # v4 fired an Over here (the bug)
    fe4, sigs4 = _run(st, "v4")
    assert [s for s in sigs4 if s.selection is Selection.OVER]


def _lift(net, pf):
    return {"goals": {"net": net, "prior_fair": pf, "lam_model": 1, "lam_prior": 1},
            "corners": {"net": net, "prior_fair": pf, "lam_model": 1, "lam_prior": 1}}


def _lp(sel, fair, line=2.5, market=G):
    st = _st(minute=60, mh=1.0, ma=1.0)
    lp = next(x for x in price_live_lines(st, R_EVEN, PricingConfig()) if x.market is market and x.selection is sel)
    return dataclasses.replace(lp, fair_odds=fair, line=line)


def test_lift_gate_thresholds():
    cfg, _ = rules_preset("v4.1")
    pf = {(2.5, Selection.OVER): 2.15, (2.5, Selection.UNDER): 2.15}
    o = _lp(Selection.OVER, 1.95)
    assert lift_gate_reason(o, _lift(1.06, pf), cfg) is None  # 2.15 / 1.95 = 1.103 lift
    assert "Overs suppressed" in lift_gate_reason(o, _lift(1.05, pf), cfg)
    assert "Overs suppressed" in lift_gate_reason(o, _lift(0.90, pf), cfg)
    assert lift_gate_reason(o, _lift(1.055, pf), cfg) == "lift: net multiplier < 1.06"
    weak = _lp(Selection.OVER, 2.10)  # 2.15 / 2.10 = 2.4 % < 6 %
    assert lift_gate_reason(weak, _lift(1.20, pf), cfg).startswith("lift: Over line lift")
    edge = _lp(Selection.OVER, 2.15 / 1.06)  # exactly 6 %
    assert lift_gate_reason(edge, _lift(1.20, pf), cfg) is None
    u = _lp(Selection.UNDER, 1.95)
    assert lift_gate_reason(u, _lift(0.80, pf), cfg) is None
    assert lift_gate_reason(u, _lift(1.00, pf), cfg).startswith("lift: Under needs")
    assert lift_gate_reason(_lp(Selection.UNDER, 2.10), _lift(0.80, pf), cfg).startswith("lift: Under line lift")


def test_net_multiplier_definition_includes_corner_shrinkage():
    st = _st(minute=40, hg=0, ag=0, hc=9, ac=4, mh=1.0, ma=1.0)  # corners well above prior pace
    fe, _ = _run(st)
    lift = fe.last_lift["e1"]
    m = LiveFootballModel(fe.model.p)
    pr = LiveFootballModel(dataclasses.replace(fe.model.p, prior_only=True))
    pm, pp = m.predict(st, R_FAV), pr.predict(st, R_FAV)
    assert np.isclose(lift["corners"]["net"], pm.mu_corners / pp.mu_corners) and lift["corners"]["net"] > 1.06
    assert np.isclose(lift["goals"]["net"], (pm.lam_home_goals + pm.lam_away_goals)
                      / (pp.lam_home_goals + pp.lam_away_goals))


def test_v41_overs_need_lift_and_emitted_alerts_respect_gate():
    st = _st(minute=35, hg=1, ag=0, hc=6, ac=4, mh=1.25, ma=1.25)
    fe, sigs = _run(st)
    assert sigs and all(s.extra.get("lift") for s in sigs)
    _check_emitted_respect_gate(fe, sigs)


# -- 2. Unders ---------------------------------------------------------------------
@pytest.mark.parametrize("line,total,expected", [
    (2.5, 2, "win"), (2.5, 3, "loss"), (2.0, 2, "push"), (2.0, 1, "win"), (2.0, 3, "loss"),
    (2.25, 2, "half_win"), (2.25, 3, "loss"), (2.25, 1, "win"),
    (2.75, 3, "half_loss"), (2.75, 2, "win"), (2.75, 4, "loss"),
])
def test_asian_under_settlement(line, total, expected):
    assert settle_bet(MarketType.TOTAL_GOALS, Selection.UNDER, line, total, 0)[0] == expected
    assert settle_bet(MarketType.TOTAL_CORNERS, Selection.UNDER, line, 0, total)[0] == expected


@pytest.mark.parametrize("line", [2.0, 2.25, 2.5, 2.75, 8.0, 8.25])
def test_under_model_settlement_mirrors_over(line):
    m = LiveFootballModel(rules_preset("v4.1")[1])
    pred = m.predict(_st(minute=50, hg=1, ag=0, hc=4, ac=3), R_EVEN)
    mk = G if line < 5 else C
    o, u = pred.settlement(mk, Selection.OVER, line), pred.settlement(mk, Selection.UNDER, line)
    assert np.isclose(o.win, u.loss) and np.isclose(o.half_win, u.half_loss) and np.isclose(o.push, u.push)
    assert np.isclose(o.loss, u.win) and np.isclose(o.half_loss, u.half_win)


def test_blowout_blocks_overs_but_allows_unders_and_caps_apply():
    t0 = time.time()
    fe = _engine("v4.1")
    sigs = fe.on_match_state(_st(minute=58, hg=3, ag=0, hc=3, ac=1, mh=0.9, ma=0.9, t=t0), t0)
    assert "e1" in fe.blowout
    assert all(w == "blowout" for k, w in fe.last_trace["e1"].items() if k[2] is Selection.OVER)
    assert sigs and all(s.selection is Selection.UNDER for s in sigs)
    fams = [s.market for s in sigs]
    assert len(fams) == len(set(fams)) <= 2  # 1 goals + 1 corners
    assert sum(s.extra["tiers"][0][1] for s in sigs) <= 0.05 * fe.pricing.bankroll + 1e-9
    for s in sigs:
        fe.commit(s)
    again = fe.on_match_state(_st(minute=75, hg=3, ag=0, hc=3, ac=1, mh=0.9, ma=0.9, t=t0 + 900), t0 + 900)
    assert not again  # hard cap: no state-change bypass, Unders count too


def test_unders_respect_minute_windows():
    st = _st(minute=50, hg=2, ag=0, hc=2, ac=1, mh=1.0, ma=1.0)
    fe, sigs = _run(st)
    assert not sigs and all(w.startswith("gate:") for w in fe.last_trace["e1"].values())


# -- 3. re-anchoring on the current score -----------------------------------------
def test_atalanta_2_2_35_reanchored_no_over_without_lift():
    st = _st(minute=35, hg=2, ag=2, hc=3, ac=2, mh=1.0, ma=1.0)
    fe, sigs = _run(st)
    assert not sigs
    tr = fe.last_trace["e1"]
    assert tr[(G, 6.5, Selection.OVER)].startswith("lift:")
    # Over 6.5 with 4 already scored = P(>= 3 remaining goals): remaining distribution only.
    m = LiveFootballModel(fe.model.p)
    pred = m.predict(st, R_FAV)
    n = pred.goal_matrix.shape[0]
    rem = np.add.outer(np.arange(n), np.arange(n))
    assert np.isclose(pred.prob_over(G, 6.5), pred.goal_matrix[rem >= 3].sum())
    # like-for-like: prior uses the same state (score, clock, stoppage) - only the multipliers differ
    assert np.isclose(fe.last_lift["e1"]["goals"]["net"], 1.0)
    # with in-play multipliers creating >= 6 % lift a goals Over may fire, and only with that lift
    hot = _st(minute=35, hg=2, ag=2, hc=3, ac=2, mh=1.25, ma=1.25)
    fe2, sigs2 = _run(hot)
    goals = [s for s in sigs2 if s.market is G]
    assert fe2.last_lift["e1"]["goals"]["net"] >= 1.06
    assert goals and goals[0].selection is Selection.OVER
    _check_emitted_respect_gate(fe2, sigs2)


# -- presets / freeze / CLI / notifier ---------------------------------------------
def test_presets_v41_selectable_and_v4_hash_unaffected():
    p41, l41 = rules_preset("v4.1")
    p4, l4 = rules_preset("v4")
    assert p41.lift_gate == 1.06 and p41.allow_unders and p41.alert_without_trigger
    assert p4.lift_gate is None and not p4.allow_unders and l4 == l41
    ps4 = param_set("v4", p4, l4)
    assert "lift_gate" not in ps4["pricing"] and "allow_unders" not in ps4["pricing"]
    assert "lift_gate" in param_set("v4.1", p41, l41)["pricing"]
    for v in ("legacy", "v3"):
        assert rules_preset(v)[0].lift_gate is None


def test_refreeze_supersedes_v4(tmp_path):
    path = tmp_path / "p.json"
    p4, l4 = rules_preset("v4")
    h4 = write_frozen(path, "v4", p4, l4)
    p41, l41 = rules_preset("v4.1")
    h41 = write_frozen(path, "v4.1", p41, l41)
    raw = json.loads(path.read_text())
    assert raw["rules_version"] == "v4.1" and raw["sha256"] == h41 != h4
    assert raw["superseded"] == [{"rules_version": "v4", "sha256": h4, "frozen_at": raw["superseded"][0]["frozen_at"]}]
    assert enforce_frozen(path, "v4.1", p41, l41) == (h41, [])
    h, notes = enforce_frozen(path, "v4", p4, l4)  # the exact superseded set may still run
    assert h == h4 and "SUPERSEDED" in notes[0]
    with pytest.raises(FrozenParamsError):
        enforce_frozen(path, "v4", dataclasses.replace(p4, target_edge=0.05), l4)
    with pytest.raises(FrozenParamsError):
        enforce_frozen(path, "v4.1", dataclasses.replace(p41, lift_gate=1.03), l41)
    assert params_hash(param_set("v4.1", p41, l41)) == h41


def test_cli_live_defaults_to_latest():
    from quantbet.config import Settings
    from quantbet.main import parse_args, settings_from_args

    ok = Settings(_env_file=None).model_copy(update={"telegram_bot_token": "x", "telegram_chat_id": "1"})  # type: ignore[call-arg]
    assert settings_from_args(parse_args(["--live"]), ok).alert_rules == "v4.2"
    assert settings_from_args(parse_args(["--live", "--rules", "v3"]), ok).alert_rules == "v3"
    assert settings_from_args(parse_args(["--live", "--rules", "v4.1"]), ok).alert_rules == "v4.1"


def test_notifier_formats_under_with_lift():
    from quantbet.notifier import format_min_odds_signal

    _, sigs = _run(_st(minute=35, hg=2, ag=0, hc=2, ac=1, mh=1.0, ma=1.0))
    txt = format_min_odds_signal(sigs[0])
    assert "Under" in txt and "MINIMUM BET365 ODDS TO BET" in txt and "Prior fair" in txt and "lift +" in txt
    assert sigs[0].extra["tiers"] and sigs[0].extra["min_odds"] > sigs[0].fair_odds


def test_paper_settle_total_under_quarter_lines():
    from quantbet.paper import result_label, settle_total
    assert result_label(settle_total(2.25, "under", 2)) == "half-win"
    assert result_label(settle_total(2.75, "under", 3)) == "half-loss"
    assert result_label(settle_total(2.0, "under", 2)) == "push"
    assert result_label(settle_total(2.5, "under", 2)) == "win"
    assert result_label(settle_total(2.5, "under", 3)) == "loss"
    assert result_label(settle_total(6.25, "under", 6)) == "half-win"
