"""v4.2: Overs only, half-split corner profile, half-adjusted upward-only corner shrinkage before 55',
refitted lead dampeners, presets/hashes, freeze superseding v4.1 + v4, CLI default."""

from __future__ import annotations

import dataclasses
import json

import pytest

from quantbet.engine import (
    V42_CORNER_HALF_RATIO, LiveFootballModel, LiveModelParams, lift_gate_reason, rules_preset,
)
from quantbet.frozen import enforce_frozen, param_set, params_hash, write_frozen
from quantbet.models import Selection
from test_rules_v4 import R_EVEN, R_FAV, _engine, _st


def _model(version="v4.2", **kw):
    _, live = rules_preset(version, None)
    return LiveFootballModel(dataclasses.replace(live, **kw) if kw else live)


def test_v42_never_emits_unders_on_marseille_like_state():
    fe = _engine("v4.2")
    st = _st(minute=37, hg=2, ag=0, hc=2, ac=1, mh=1.0, ma=1.0)
    sigs = fe.on_match_state(st, st.updated_at)
    assert all(s.selection is Selection.OVER for s in sigs)
    fe41 = _engine("v4.1")
    st2 = _st(minute=37, hg=2, ag=0, hc=2, ac=1, mh=1.0, ma=1.0, key="e2")
    assert any(s.selection is Selection.UNDER for s in fe41.on_match_state(st2, st2.updated_at))


def test_unders_disabled_reason():
    pricing, _ = rules_preset("v4.2", None)
    assert pricing.allow_unders is False and pricing.lift_gate == 1.06

    from quantbet.models import MarketType

    class LP:
        market = MarketType.TOTAL_GOALS
        line = 2.5
        selection = Selection.UNDER
        fair_odds = 1.9
    fam = {"net": 0.7, "lam_model": 1.0, "lam_prior": 2.0, "prior_fair": {(2.5, Selection.UNDER): 3.0}}
    assert lift_gate_reason(LP, {"goals": fam, "corners": fam}, pricing) == "lift: Unders disabled"


def test_emitted_overs_respect_lift_gate():
    fe = _engine("v4.2")
    for i, (m, hg, ag, hc, ac, mh) in enumerate([(35, 2, 2, 3, 2, 1.25), (65, 0, 1, 6, 1, 1.25), (32, 0, 0, 1, 0, 1.2)]):
        st = _st(minute=m, hg=hg, ag=ag, hc=hc, ac=ac, mh=mh, ma=mh, key=f"k{i}")
        for s in fe.on_match_state(st, st.updated_at):
            lf = s.extra["lift"]
            assert s.selection is Selection.OVER
            assert lf["net_multiplier"] >= 1.06 and s.fair_odds <= lf["prior_fair"] / 1.06 + 1e-9


def test_half_profile_normalised_and_ratio():
    m = _model()
    w1, w2 = m._half_weights(V42_CORNER_HALF_RATIO)
    assert abs(w1 * 45 + w2 * 45 - 90) < 1e-9 and abs(w2 / w1 - V42_CORNER_HALF_RATIO) < 1e-9
    k0 = _st(minute=0, hc=0, ac=0)
    m0 = _model(first_half_stoppage=0.0, second_half_stoppage_base=0.0)
    assert abs(m0.remaining_intensity(k0, 0.15, V42_CORNER_HALF_RATIO) - 1.0) < 1e-9
    h = _st(minute=45, hc=0, ac=0)
    h = dataclasses.replace(h, period=2) if dataclasses.is_dataclass(h) else h
    # at half time the remaining share equals the H2 share w2/2
    assert abs(m0.remaining_intensity(_st(minute=46, hc=0, ac=0), 0.0, V42_CORNER_HALF_RATIO)
               - w2 * 44 / 90) < 1e-9


def test_prior_uses_half_profile_too():
    _, live = rules_preset("v4.2", None)
    prior = LiveFootballModel(dataclasses.replace(live, prior_only=True))
    st = _st(minute=40, hc=2, ac=2)
    assert prior.predict(st, R_EVEN).mu_corners == pytest.approx(
        10.0 * prior.remaining_intensity(st, live.corner_time_slope, V42_CORNER_HALF_RATIO))


@pytest.mark.parametrize("minute,hc,ac", [(30, 0, 0), (35, 1, 1), (40, 0, 2), (54, 1, 1)])
def test_slow_first_half_cannot_lower_corners_before_55(minute, hc, ac):
    m = _model()
    st = _st(minute=minute, hc=hc, ac=ac, mh=1.0, ma=1.0)
    base = m.shrunk_rates(st, R_EVEN)
    assert base["home_corners"] >= R_EVEN.home_corners - 1e-9 and base["away_corners"] >= R_EVEN.away_corners - 1e-9
    # v4 (full-match pace, no clamp) does lower it
    b4 = _model("v4").shrunk_rates(st, R_EVEN)
    assert b4["home_corners"] + b4["away_corners"] < 10.0


def test_shrinkage_can_lower_after_55_and_raise_before():
    m = _model()
    late = m.shrunk_rates(_st(minute=70, hc=1, ac=1), R_EVEN)
    assert late["home_corners"] + late["away_corners"] < 10.0
    early = m.shrunk_rates(_st(minute=30, hc=6, ac=5), R_EVEN)
    assert early["home_corners"] + early["away_corners"] > 10.0


def test_half_adjusted_pace_reference():
    m = _model()
    st = _st(minute=40, hc=2, ac=2)
    eff = m.played_corner_minutes(st)
    w1, _ = m._half_weights(V42_CORNER_HALF_RATIO)
    assert eff == pytest.approx(w1 * 40)  # expected corners so far = prior x eff / 90 (H1 rate, not match average)


def test_lead_dampeners_from_fit_and_shield_only():
    _, live = rules_preset("v4.2", None)
    assert live.dampener_goal_mult == 1.0 and live.dampener_leader_corner_mult == pytest.approx(0.952)
    assert live.dampener_goal_mult <= 1.0 and live.dampener_leader_corner_mult <= 1.0
    rep = json.load(open("config/calibration_report.json"))["v4_2"]
    assert rep["football_data"]["has_half_time_corners"] is False
    assert rep["statsbomb"]["h2_h1_per_minute_ratio"] == pytest.approx(V42_CORNER_HALF_RATIO, abs=1e-3)


def test_hashes_of_older_versions_unchanged_and_v42_distinct():
    hs = {v: params_hash(param_set(v, *rules_preset(v, None))) for v in ("v4", "v4.1", "v4.2")}
    lv4 = param_set("v4", *rules_preset("v4", None))["live_params"]
    assert "corner_half_ratio" not in lv4 and len(set(hs.values())) == 3
    assert param_set("v4.2", *rules_preset("v4.2", None))["live_params"]["corner_half_ratio"] == V42_CORNER_HALF_RATIO


def test_v42_freeze_supersedes_v41_and_v4(tmp_path):
    p = tmp_path / "f.json"
    hs = {}
    for v in ("v4", "v4.1", "v4.2"):
        pc, lp = rules_preset(v, None)
        hs[v] = write_frozen(p, v, pc, lp)
    d = json.loads(p.read_text())
    assert d["rules_version"] == "v4.2" and [x["rules_version"] for x in d["superseded"]] == ["v4", "v4.1"]
    for v in ("v4", "v4.1"):
        pc, lp = rules_preset(v, None)
        h, notes = enforce_frozen(p, v, pc, lp)
        assert h == hs[v] and notes
    pc, lp = rules_preset("v4.2", None)
    assert enforce_frozen(p, "v4.2", pc, lp)[0] == hs["v4.2"]


def test_cli_default_v42():
    from quantbet.config import Settings
    from quantbet.main import parse_args, settings_from_args
    assert Settings(_env_file=None).alert_rules == "v4.2"
    ok = Settings(_env_file=None).model_copy(update={"telegram_bot_token": "x", "telegram_chat_id": "1"})
    s = settings_from_args(parse_args(["--live"]), ok)
    assert s.alert_rules == "v4.2"
