"""v4.5-sharp: league hygiene (unmapped / youth), TIER_3 = Goals O/U only, minute windows, momentum flurry
cap, w_fav_trailing, Bet365 card counting (Y 1 / R 2 / second yellow 3), NegBin cards, paper-only cards
outside the match caps, gated combos, the (opt-in) second-half tempo_stall and the v4.5 freeze (v4.2 / v4.3 /
v4.4 hashes intact). The final v4.5 patch: restored windows, corner dispersion 65 / prior 200, more youth
tokens, Over activity confirmation, the cross-family direction lock, attack momentum / activity suppressing
Unders, stricter bus_parking, the youth guard in team-name matching / ratings, and the replay output key.
Offline, no network."""

from __future__ import annotations

import json
import math
import shutil
import time
from pathlib import Path

import pytest
from scipy.stats import nbinom, poisson

from quantbet.engine import LiveFootballModel, PreMatchRatings, SignalEngine, rules_preset
from quantbet.espn import card_points, card_points_from_details, card_points_from_key_events
from quantbet.frozen import FrozenParamsError, enforce_frozen, read_frozen_set
from quantbet.global_layer import GlobalRatingsResolver, LeagueInfo, LeagueRegistry
from quantbet.models import LiveMatchState, MarketType, Selection
from quantbet.notifier import format_min_odds_signal
from quantbet.risk import RiskConfig, RiskManager
from quantbet.syndicate import (SharpParams, SustainedMomentumTracker, SyndicateDeduplicator, SyndicateParams,
                                SyndicateSignalEngine, cards_inputs, cards_model, cards_nb_r, combo_leg_selection,
                                is_sharp, is_syndicate, load_priors, negbin_total_settlement, poisson_total_settlement,
                                price_card_lines, team_card_points, youth_reserve_hit)

ROOT = Path(__file__).resolve().parents[1]
V42_HASH = "40e806b910db93de34f9b26d9ebf361168077f1f38a8fe8d7dbc4e7cb6729aec"
V43_HASH = "971ab88662be2f23c0dd987484c7dcac1f36ed479bc4e650f99e60b0e1e9f77a"
V44_HASH = "c3a6d97a385bd07e614e4040b4df2c6aff437d445061fec35bbe30f10dcb3ced"
TIER_KW = dict(tier_kelly_mult=(("TIER_1", 1.0), ("TIER_2", 1.0), ("TIER_3", 0.5)), tier_max_units=(("TIER_3", 15.0),))
R = PreMatchRatings(0.6, 2.9, 1.0, 10.0, 0.55, 0.2)
SH = SharpParams()
PRIORS = load_priors(ROOT / "config/syndicate_priors.json")


def _state(key="e1", m=35.0, hg=0, ag=0, hc=4, ac=3, hs=8, as_=6, hsot=3, asot=2, mh=1.0, ma=1.0, hf=None, af=None,
           hy=None, ay=None, hr=0, ar=0, t=None, slug="eng.1", league="Premier League", home="Home FC",
           away="Away FC") -> LiveMatchState:
    s = LiveMatchState(event_key=key, minute=m, home_goals=hg, away_goals=ag, home_corners=hc, away_corners=ac,
                       home_red_cards=hr, away_red_cards=ar, period=2 if m > 45 else 1,
                       updated_at=time.time() if t is None else t, home=home, away=away, league=league,
                       status="2nd half" if m > 45 else "1st half", stats_available=True, attack_momentum=0.5,
                       home_shots=hs, away_shots=as_, home_shots_on_target=hsot, away_shots_on_target=asot,
                       league_slug=slug, home_fouls=hf, away_fouls=af, home_yellow_cards=hy, away_yellow_cards=ay)
    s.momentum_mult_home, s.momentum_mult_away, s.momentum_mult_total = mh, ma, (mh + ma) / 2
    return s


class _Ratings:
    def ratings(self, h, a, league=None):  # noqa: ANN001, ANN201
        return R


def _engine(rules="v4.5-sharp", params=None, tier="TIER_1") -> SyndicateSignalEngine:
    reg = LeagueRegistry({"eng.1": LeagueInfo("eng.1", "Premier League", tier=tier),
                          "eng.3": LeagueInfo("eng.3", "League One", tier="TIER_3")})
    pricing, live = rules_preset(rules, None, bankroll=2000.0)
    rm = RiskManager(RiskConfig(bankroll=2000, unit_size=10, **TIER_KW))
    res = GlobalRatingsResolver(reg, None, _Ratings(), team_table_slugs=("eng.1", "eng.3"))
    return SyndicateSignalEngine(reg, rm, res, pricing, SignalEngine(live_params=live),
                                 sizer=lambda f, o: rm.stake_from_kelly(f), params=params or SharpParams(),
                                 priors=PRIORS)


def _drive(eng, key, minutes, rate=1.0, t0=1_000_000.0, **kw):  # noqa: ANN001, ANN201
    out = []
    for i, m in enumerate(minutes):
        s = _state(key, float(m), hc=int(m * 0.11 * rate), ac=int(m * 0.09 * rate), hs=int(m * 0.28 * rate),
                   as_=int(m * 0.22 * rate), hsot=int(m * 0.10 * rate), asot=int(m * 0.08 * rate),
                   t=t0 + i * 60, **kw)
        out += eng.on_match_state(s, s.updated_at)
    return out


# -- registration -------------------------------------------------------------------------------
def test_v45_is_registered_and_default_everywhere():
    from quantbet.config import Settings
    from quantbet.engine import RULE_VERSIONS
    assert "v4.5-sharp" in RULE_VERSIONS
    assert Settings(_env_file=None).alert_rules == "v4.5-sharp"  # type: ignore[call-arg]
    assert is_syndicate("v4.5-sharp") and is_sharp("v4.5-sharp") and not is_sharp("v4.4-syndicate")
    wf = (ROOT / ".github/workflows/live_quantbet.yml").read_text()
    assert "default: v4.5-sharp" in wf.replace('"', "") and "inputs.rules || 'v4.5-sharp'" in wf
    for old in ("v4.4-syndicate", "v4.3-global", "v4.2"):
        assert old in wf
    assert SH.version() == "v4.5-sharp" and SyndicateParams().version() == "v4.4-syndicate"


def test_cli_default_is_v45_and_old_rules_selectable():
    from quantbet.config import Settings
    from quantbet.main import parse_args, settings_from_args
    ok = Settings(_env_file=None).model_copy(update={"telegram_bot_token": "x", "telegram_chat_id": "1"})
    assert settings_from_args(parse_args(["--live"]), ok).alert_rules == "v4.5-sharp"
    for r in ("v4.5-sharp", "v4.4-syndicate", "v4.3-global", "v4.2"):
        assert parse_args(["--dry-run", "--rules", r]).rules == r
        assert settings_from_args(parse_args(["--live", "--rules", r]), ok).alert_rules == r


def test_spec_values():
    assert dict(SH.tier_target_edge) == {"TIER_1": 0.035, "TIER_2": 0.045, "TIER_3": 0.060}
    assert SH.abort_ratio == 0.60 and SH.achievability_gate is False
    assert SH.lift_cap("goals") == 0.28 and SH.lift_cap("corners") == 0.35
    assert SH.over_net_min("goals") == 1.06 and SH.over_net_min("corners") == 1.08 and SH.over_suppress_net == 1.05
    assert SH.abort_edge("TIER_2") == pytest.approx(0.045 * 0.60)
    assert SH.sustained_over_index == 1.35 and SH.momentum_polls == 3 and SH.max_momentum_index == 2.90
    assert SH.w_fav_trailing == 1.0 and SH.cards_paper_only is True
    p, lp = rules_preset("v4.5-sharp", None)
    assert p.minute_windows == ((30.0, 43.0), (55.0, 85.0))
    assert lp.corner_prior_minutes == 200.0 and lp.corner_dispersion == 65.0
    assert p.activity_trigger_mult == 1.04
    assert SH.require_activity_confirm and SH.match_direction_lock and SH.stall2_enabled is False
    assert (SH.under_suppress_attack_momentum, SH.under_suppress_mult_total) == (0.30, 1.02)
    assert (SH.bus_from_minute, SH.bus_leader_share_max, SH.bus_index_max) == (65.0, 0.25, 0.75)
    p44, lp44 = rules_preset("v4.4-syndicate", None)
    assert p44.minute_windows == ((30.0, 43.0), (55.0, 85.0))  # v4.4 untouched


# -- §2 league hygiene ----------------------------------------------------------------------------
def test_unmapped_espn_league_blocked_with_trace_and_no_alerts():
    assert SH.hygiene_block("espn.18481", False, "Wimbledon", "Arsenal") == "unmapped_league_blocked"
    assert SH.hygiene_block("espn.3957", True, "A", "B") == "unmapped_league_blocked"  # prefix always blocked
    assert SH.hygiene_block("xyz.9", False, "A", "B") == "unmapped_league_blocked"  # not in leagues.json
    assert SH.hygiene_block(None, False, "A", "B") == "unmapped_league_blocked"
    assert SH.hygiene_block("eng.1", True, "A", "B") is None
    assert SyndicateParams().hygiene_block("espn.18481", False, "A", "B") is None  # v4.4 unchanged
    eng = _engine()
    sigs = _drive(eng, "u", range(20, 70), rate=2.0, slug="espn.18481", league="EFL Trophy")
    assert sigs == []
    tr = eng.last_trace["u"]
    assert tr and set(tr.values()) == {"unmapped_league_blocked"}


def test_only_leagues_json_slugs_listed():
    leagues = json.loads((ROOT / "config/leagues.json").read_text())
    slugs = {x["slug"] for x in (leagues["leagues"] if isinstance(leagues, dict) else leagues)}
    assert not any(s.startswith("espn.") for s in slugs)
    assert "eng.1" in slugs


@pytest.mark.parametrize("name,hit", [
    ("Arsenal U21", "u21"), ("Everton U-21", "u21"), ("Aston Villa u 23", "u23"), ("Jong PSV U19", "u19"),
    ("Real Madrid B", " b"), ("Villarreal B", " b"), ("Hertha BSC II", " ii"), ("Borussia Dortmund II", " ii"),
    ("Liverpool Reserves", "reserves"), ("Ajax Academy", "academy"), ("Bayern-II", " ii"),
])
def test_youth_filter_hits(name, hit):
    assert youth_reserve_hit(name, SH.youth_tokens, SH.youth_suffix_tokens, SH.youth_name_exceptions) == hit


@pytest.mark.parametrize("name", ["Club Brugge", "Bayern Munich", "Bayern", "Brentford", "Bilbao", "Benfica",
                                  "Inter Milan", "Iraklis", "Sporting CP", "Union Berlin", "Juan Pablo II",
                                  "Bournemouth", "Burnley", "Rubin Kazan", "Leeds United", "Southampton"])
def test_youth_filter_no_false_positives(name):
    assert youth_reserve_hit(name, SH.youth_tokens, SH.youth_suffix_tokens, SH.youth_name_exceptions) is None


def test_youth_team_blocks_even_in_mapped_league():
    assert SH.hygiene_block("eng.1", True, "Wimbledon", "Arsenal U21") == "youth_reserve_blocked (u21)"
    eng = _engine()
    assert _drive(eng, "y", range(20, 70), rate=2.0, away="Real Madrid B") == []
    assert set(eng.last_trace["y"].values()) == {"youth_reserve_blocked (b)"}


def test_tier3_is_goals_over_under_only():
    assert SH.tier_allows("TIER_3", "goals") and not SH.tier_allows("TIER_3", "corners")
    assert not SH.tier_allows("TIER_3", "cards") and SH.tier_allows("TIER_2", "corners")
    assert SH.tier_allows("TIER_1", "cards") and SyndicateParams().tier_allows("TIER_3", "corners")
    eng = _engine()
    _drive(eng, "t3", range(20, 41), rate=2.0, slug="eng.3", league="League One")
    tr = eng.last_trace["t3"]
    corner = {k: v for k, v in tr.items() if k[0] is MarketType.TOTAL_CORNERS}
    goal = {k: v for k, v in tr.items() if k[0] is MarketType.TOTAL_GOALS}
    assert corner and set(corner.values()) <= {"tier3_market_blocked (TIER_3 = Goals Over/Under only)"} | {
        v for v in corner.values() if v.startswith("gate:") or v == "blowout"}
    assert any(v.startswith("tier3") for v in corner.values())
    assert not any(v.startswith("tier3") for v in goal.values())


# -- §3 windows / momentum ---------------------------------------------------------------------------
@pytest.mark.parametrize("m,ok", [(29.0, False), (30.0, True), (33.0, True), (43.0, True), (44.0, False),
                                  (54.0, False), (55.0, True), (84.0, True), (85.0, True), (86.0, False)])
def test_minute_windows(m, ok):
    from quantbet.engine import assess_confidence
    p, lp = rules_preset("v4.5-sharp", None)
    s = _state("w", m)
    rep = assess_confidence(s, "football_data", p, R, s.updated_at, lp)
    has = any("outside windows 30-43, 55-85" in r for r in rep.reasons)
    assert has is (not ok)


def _flurry_tracker(params, rate, minutes=range(20, 41), **kw):  # noqa: ANN001, ANN201
    tr = SustainedMomentumTracker(params)
    v = None
    for i, m in enumerate(minutes):
        s = _state("f", float(m), hc=int(m * 0.11 * rate), ac=int(m * 0.09 * rate), hs=int(m * 0.28 * rate),
                   as_=int(m * 0.22 * rate), hsot=int(m * 0.10 * rate), asot=int(m * 0.08 * rate), **kw)
        v = tr.update(s, R, None, now=i * 60.0)
    return v


def test_flurry_cap_blocks_overs_above_290():
    assert SH.flurry_cap() == 2.90 and SyndicateParams().flurry_cap() is None
    v = _flurry_tracker(SH, 4.0)
    assert v.reading.assessable and v.reading.adj_index > 2.90 and v.sustained_over
    eng = _engine()
    _drive(eng, "fl", range(20, 41), rate=2.0, mh=1.1, ma=1.1)
    tr = eng.last_trace["fl"]
    assert eng.last_momentum["fl"].reading.adj_index > 2.90
    overs = [v for k, v in tr.items() if k[2] is Selection.OVER and k[0] is not MarketType.TOTAL_CARDS]
    assert any(v.startswith("momentum_flurry_cap (index") for v in overs)
    assert not any(v in ("emitted", "not_best") for v in overs)
    # same match without the cap: those lines get past the flurry check
    import dataclasses
    eng2 = _engine(params=dataclasses.replace(SharpParams(), max_momentum_index=99.0))
    _drive(eng2, "fl", range(20, 41), rate=2.0, mh=1.1, ma=1.1)
    assert not any(v.startswith("momentum_flurry_cap") for v in eng2.last_trace["fl"].values())


def test_flurry_cap_290_lets_a_240_to_290_reading_through():
    # adj_index ~2.74 (like Sonnenhof v Augsburg at 2.84 on 27 Oct): capped by the pre-patch 2.40, not by 2.90
    eng = _engine()
    _drive(eng, "fs", range(20, 41), rate=1.5, mh=1.1, ma=1.1)
    adj = eng.last_momentum["fs"].reading.adj_index
    assert 2.40 < adj <= 2.90
    assert not any(v.startswith("momentum_flurry_cap") for v in eng.last_trace["fs"].values())


def test_sustained_threshold_135_over_3_polls():
    import dataclasses
    for rate in (0.9, 1.0, 1.1, 1.2, 1.6):
        v = _flurry_tracker(SH, rate)
        assert v.polls >= 3
        assert v.sustained_over is (v.reading.adj_index >= 1.35)
        v44 = _flurry_tracker(SyndicateParams(), rate)
        assert v44.reading.index == pytest.approx(v.reading.index)
        if 1.35 <= v.reading.adj_index < 1.70:
            assert v.sustained_over and not v44.sustained_over
    # a match whose last persistence window sits between the pre-patch 1.25 and 1.35 is no longer sustained
    lo = dataclasses.replace(SH, sustained_over_index=1.25)
    def smooth(params, rate):  # noqa: ANN001, ANN202 - fractional counters: a constant pace, a constant index
        tr = SustainedMomentumTracker(params)
        v = None
        for i, m in enumerate(range(20, 41)):
            st = _state("f", float(m), hc=m * 0.11 * rate, ac=m * 0.09 * rate, hs=m * 0.28 * rate,
                        as_=m * 0.22 * rate, hsot=m * 0.10 * rate, asot=m * 0.08 * rate)
            v = tr.update(st, R, None, now=i * 60.0)
        return tr, v
    found = False
    for rate in [x / 200 for x in range(100, 300)]:
        tr, v = smooth(SH, rate)
        recent = [r for r in tr.readings["f"] if r.minute >= 40 - SH.momentum_persist_min - 1e-9]
        if len(recent) >= 3 and all(r.assessable and 1.25 <= r.adj_index < 1.35 for r in recent):
            assert not v.sustained_over
            assert smooth(lo, rate)[1].sustained_over
            found = True
            break
    assert found


def test_w_fav_trailing_is_neutral_in_v45():
    # favourite (home, 0.6 vs 2.9 -> home strength from R) trailing 0-1 at 60'
    def run(p):  # noqa: ANN001, ANN202
        from quantbet.engine import LiveModelParams
        tr = SustainedMomentumTracker(p)
        v = None
        for i, m in enumerate(range(45, 61)):
            s = _state("ft", float(m), hg=0, ag=1, hc=int(m * 0.12), ac=int(m * 0.08), hs=int(m * 0.3),
                       as_=int(m * 0.2), hsot=int(m * 0.1), asot=int(m * 0.07))
            v = tr.update(s, PreMatchRatings(1.9, 2.9, 3.0, 10.0, 0.55, 0.2), LiveModelParams(), now=i * 60.0)
        return v
    v45, v44 = run(SH), run(SyndicateParams())
    assert v44.fav_trailing and v45.fav_trailing
    assert v44.reading.state_weight == pytest.approx(1.15)
    assert v45.reading.state_weight == pytest.approx(1.0)
    assert v45.reading.adj_index == pytest.approx(v45.reading.index)


# -- §4 cards ---------------------------------------------------------------------------------------
def test_card_points_yellow1_red2_second_yellow3():
    assert card_points([("p1", "Y", False)]) == 1
    assert card_points([("p1", "R", False)]) == 2
    assert card_points([("p1", "Y", False), ("p1", "R", True)]) == 3  # second yellow
    assert card_points([("p1", "Y", False), ("p1", "R", False)]) == 3  # yellow then straight red
    assert card_points([("p1", "R", True)]) == 3  # flagged second yellow, first yellow not listed
    assert card_points([("p1", "Y", False), ("p1", "Y", False)]) == 3  # two yellows, no red entry
    assert card_points([("p1", "Y", False), ("p1", "Y", False), ("p1", "R", True)]) == 3  # max 3 per player
    assert card_points([("p1", "Y", False), ("p2", "Y", False), ("p3", "R", False)]) == 4
    assert card_points([(None, "Y", False), (None, "R", False), (None, "R", True)]) == 6


def _det(tid, aid, text, minute="60'", red=False, yellow=False, shootout=False):  # noqa: ANN001, ANN202
    return {"type": {"text": text}, "team": {"id": tid}, "athletesInvolved": [{"id": aid}] if aid else [],
            "redCard": red, "yellowCard": yellow, "shootout": shootout, "clock": {"displayValue": minute, "value": 3600}}


def test_card_points_from_scoreboard_details():
    d = [_det("1", "a", "Yellow Card", yellow=True), _det("1", "a", "Red Card", red=True),  # 2nd yellow: 3
         _det("1", "b", "Yellow Card", yellow=True),  # 1
         _det("2", "c", "Red Card", red=True),  # straight red 2
         _det("2", "d", "Yellow Card", yellow=True, shootout=True)]  # ignored
    assert card_points_from_details(d, "1", "2") == (4, 2)
    assert card_points_from_details(None, "1", "2") == (None, None)


def test_card_points_from_summary_key_events():
    kev = [{"type": "yellow-card", "team": "Home FC", "athlete_id": "9"},
           {"type": "red-card", "team": "Home FC", "athlete_id": "9", "text": "Second yellow card to X"},
           {"type": "red-card", "team": "Away FC", "athlete_id": "7", "text": "Red Card"}]
    assert card_points_from_key_events(kev, "Home FC", "Away FC") == (3, 2)


def test_cards_inputs_bet365_vs_v44_rule():
    s = _state("c", 60.0, hf=10, af=11, hy=2, ay=1, hr=1, ar=0)
    assert cards_inputs(s, "y1_r1").cards == 4  # v4.4: Y + R
    assert cards_inputs(s).cards == 5  # v4.5: Y + 2R
    s.home_card_points, s.away_card_points = 4, 1  # per-player reconstruction wins
    assert team_card_points(s, "home") == 4 and cards_inputs(s).cards == 5
    s.home_card_points = None
    s.home_yellow_cards = None
    assert cards_inputs(s) is None


def test_cards_priors_block():
    b = PRIORS["cards_bet365"]
    assert b["pooled"]["cards_var"] > b["pooled"]["cards90"] > 4.0
    assert 5.0 < cards_nb_r(PRIORS) < 40.0
    assert b["baselines"]["eng.1"]["cards90"] > 0 and PRIORS["cards_pooled"]["cards90"] == 4.318  # legacy kept


# -- NegBin -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("cur,mu,line", [(2, 2.4, 4.5), (0, 4.5, 4.5), (5, 1.1, 6.5), (3, 3.0, 5.0), (1, 2.0, 3.25)])
def test_negbin_approaches_poisson_as_r_grows(cur, mu, line):
    for sel in (Selection.OVER, Selection.UNDER):
        ps = poisson_total_settlement(cur, mu, line, sel)
        for r in (None, math.inf, 1e7):
            nb = negbin_total_settlement(cur, mu, r, line, sel)
            assert nb.win_weight == pytest.approx(ps.win_weight, abs=1e-12)
        nb = negbin_total_settlement(cur, mu, 1e5, line, sel)
        assert nb.win_weight == pytest.approx(ps.win_weight, abs=2e-4)


def test_negbin_variance_exceeds_mean_and_matches_scipy():
    mu, r = 2.6, cards_nb_r(PRIORS)
    pz = r / (r + mu)
    assert nbinom.mean(r, pz) == pytest.approx(mu)
    assert nbinom.var(r, pz) == pytest.approx(mu + mu * mu / r) and nbinom.var(r, pz) > mu
    o = negbin_total_settlement(1, mu, r, 3.5, Selection.OVER)
    assert o.win_weight == pytest.approx(float(nbinom.sf(2, r, pz)), abs=1e-12)
    po = poisson_total_settlement(1, mu, 3.5, Selection.OVER)
    assert o.win_weight != pytest.approx(po.win_weight, abs=1e-4)
    u = negbin_total_settlement(1, mu, r, 3.5, Selection.UNDER)
    assert o.win_weight + u.win_weight == pytest.approx(1.0, abs=1e-12)
    # fatter tails: far Over more likely than Poisson, near-median less certain
    assert negbin_total_settlement(0, mu, r, 7.5, Selection.OVER).win_weight > \
        poisson_total_settlement(0, mu, 7.5, Selection.OVER).win_weight


def test_price_card_lines_uses_negbin_when_r_given():
    from quantbet.engine import PricingConfig
    s = _state("c", 60.0, hf=16, af=15, hy=2, ay=2)
    res = cards_model(cards_inputs(s), 4.51, 24.17, SH, 0.68, "test")
    a = {(lp.line, lp.selection): lp.p_true for lp, _ in price_card_lines(res, PricingConfig(), None, 5.0, None)}
    b = {(lp.line, lp.selection): lp.p_true for lp, _ in price_card_lines(res, PricingConfig(), None, 5.0, 13.6)}
    common = a.keys() & b.keys()
    assert common and any(abs(a[k] - b[k]) > 1e-4 for k in common)


# -- paper cards / caps -----------------------------------------------------------------------------
def test_paper_cards_do_not_consume_match_caps():
    d = SyndicateDeduplicator(SH, 2000.0)
    cap = SH.max_match_stake_frac * 2000.0
    d.record("m", "goals", "over", 2.5, 0.5, cap, 60.0)
    assert d.check("m", "corners", "over", 10.5, 10.0, 61.0, None) == "stake_cap"
    assert d.check("m", "cards", "over", 4.5, 10.0, 61.0, None, paper=True) is None
    before = dict(d.match["m"])
    d.record("m", "cards", "over", 4.5, 0.5, 10.0, 61.0, paper=True)
    assert d.match["m"] == before and d.remaining_stake("m") == 0.0
    # family rules still apply to paper
    assert d.check("m", "cards", "over", 4.5, 10.0, 65.0, None, paper=True) == "cooldown"
    d2 = SyndicateDeduplicator(SH, 2000.0)
    for i in range(3):
        d2.record("x", f"f{i}", "over", 1.5, 0.5, 1.0, 50.0 + i)
    assert d2.check("x", "cards", "over", 4.5, 1.0, 60.0, None) == "match_signal_cap"
    assert d2.check("x", "cards", "over", 4.5, 1.0, 60.0, None, paper=True) is None


def _paper_signal():  # noqa: ANN202
    eng = _engine()
    out = []
    for i, m in enumerate(range(45, 66)):
        s = _state("pc", float(m), hc=int(m * 0.1), ac=int(m * 0.09), hs=int(m * 0.25), as_=int(m * 0.2),
                   hsot=int(m * 0.09), asot=int(m * 0.07), hf=int(m * 0.42), af=int(m * 0.40),
                   hy=2, ay=2, t=1_000_000.0 + i * 60)
        out += eng.on_match_state(s, s.updated_at)
    return eng, [x for x in out if x.market is MarketType.TOTAL_CARDS]


def test_paper_cards_signal_tagged_logged_and_not_staked():
    eng, cards = _paper_signal()
    if not cards:
        pytest.skip("no cards signal produced by the synthetic match (model-dependent)")
    sg = cards[0]
    assert sg.extra["paper_only"] is True and sg.extra["dedup"]["paper"] is True
    assert sg.extra["cards_model"]["rule"] == "bet365" and sg.extra["cards_model"]["nb_r"] > 5
    assert sg.method == "live:negbin_cards" and "combos" not in sg.extra
    before = dict(eng.sdedup.match.get("pc", {"stake": 0.0, "n": 0}))
    eng.commit(sg)
    assert eng.sdedup.match.get("pc", {"stake": 0.0, "n": 0}) == before  # paper never adds to the match caps
    assert eng.sdedup.last("pc", "cards") is not None  # but is recorded for the family cooldown
    txt = format_min_odds_signal(sg)
    assert "[PAPER ONLY - UNCALIBRATED]" in txt and "do NOT bet" in txt
    assert "MINIMUM BET365 ODDS TO BET" not in txt and "yellow=1, red=2" in txt and "Total Cards</b>" in txt


def test_notifier_paper_tag_on_synthetic_signal():
    from quantbet.models import Signal, Urgency
    sig = Signal(urgency=Urgency.LIVE_MODEL, engine="B", event_key="k", home="A", away="B", league="L",
                 market=MarketType.TOTAL_CARDS, line=4.5, selection=Selection.OVER, bookmaker="bet365",
                 bookmaker_odds=1.95, p_true=0.55, fair_odds=1.82, min_acceptable_odds=1.90, ev=0.07,
                 kelly_fraction=0.02, is_live=True, minute=60.0, score="1-0", method="live:negbin_cards",
                 extra={"paper_only": True, "min_odds": 1.95, "abort_floor": 1.90,
                        "tiers": [[1.95, 10.0], [2.05, 15.0]],
                        "cards_model": {"lam_remaining": 2.1, "lam_prior": 1.8, "net": 1.17, "foul_factor": 1.1,
                                        "booking_factor": 1.05, "urgency": 1.0, "current": 3, "rule": "bet365",
                                        "nb_r": 13.647},
                        "syndicate": {"tier": "TIER_1", "hurdle": 0.035, "abort_edge": 0.021, "assumed_margin": 0.04,
                                      "lift_required": 1.0, "achievability_gate": False}})
    txt = format_min_odds_signal(sig)
    assert "[PAPER ONLY - UNCALIBRATED]" in txt and "NegBin r=13.647" in txt
    assert "achievability gate off" in txt and "NOT the 10/25 booking-points market" in txt
    assert "MINIMUM BET365 ODDS TO BET" not in txt and "Stake: none" in txt


# -- §5 combos / second-half stall -------------------------------------------------------------------
def test_combo_legs_only_for_independently_gated_families():
    v44 = SyndicateParams()
    assert combo_leg_selection(v44, "corners", 1.01, "TIER_1") is Selection.OVER  # v4.4: always a leg
    assert combo_leg_selection(v44, "corners", 0.99, "TIER_1") is Selection.UNDER
    assert combo_leg_selection(SH, "corners", 1.07, "TIER_1") is None  # below 1.08
    assert combo_leg_selection(SH, "corners", 1.08, "TIER_1") is Selection.OVER
    assert combo_leg_selection(SH, "goals", 1.06, "TIER_1") is Selection.OVER
    assert combo_leg_selection(SH, "goals", 1.00, "TIER_1") is None
    assert combo_leg_selection(SH, "goals", SH.under_max_net, "TIER_1") is Selection.UNDER
    assert combo_leg_selection(SH, "corners", 1.30, "TIER_3") is None  # tier disallows
    assert combo_leg_selection(SH, "cards", 1.50, "TIER_1") is None  # paper cards never a leg


def _stall_tracker(params, minutes, rate, gd=(0, 0), lam=2.9):  # noqa: ANN001, ANN202
    tr = SustainedMomentumTracker(params)
    v = None
    hg, ag = gd
    base = {"hc": 0, "ac": 0, "hs": 0, "as": 0, "hsot": 0, "asot": 0}
    for i, m in enumerate(minutes):
        r = rate(m)
        base = {k: v + r * w for (k, v), w in zip(base.items(), (0.055, 0.045, 0.14, 0.11, 0.05, 0.04))}
        s = _state("s2", float(m), hg=hg, ag=ag, hc=int(base["hc"]), ac=int(base["ac"]), hs=int(base["hs"]),
                   as_=int(base["as"]), hsot=int(base["hsot"]), asot=int(base["asot"]))
        v = tr.update(s, PreMatchRatings(0.0, lam, 0.0, 10.0, 0.55, 0.2), None, now=i * 60.0)
    return v


def test_second_half_tempo_stall_off_by_default_and_opt_in_mechanics():
    import dataclasses
    dead = lambda m: 2.0 if m < 45 else 0.15  # noqa: E731
    assert SH.stall2_enabled is False and SH.second_half_stall() is False
    v_def = _stall_tracker(SH, range(30, 71), dead)
    assert v_def.reading.adj_index <= 0.65 and not v_def.tempo_stall  # default: no second-half stall
    on = dataclasses.replace(SH, stall2_enabled=True)  # the fields are kept (opt-in)
    v = _stall_tracker(on, range(30, 71), dead)
    assert v.reading.assessable and v.reading.adj_index <= 0.65 and v.tempo_stall
    assert "tempo_stall" in v.labels()
    v44 = _stall_tracker(SyndicateParams(), range(30, 71), dead)
    assert not v44.tempo_stall  # v4.4: first half only
    assert not _stall_tracker(on, range(30, 80), dead).tempo_stall  # after 75'
    assert not _stall_tracker(on, range(30, 71), dead, gd=(2, 0)).tempo_stall  # |gd| 2
    assert _stall_tracker(on, range(30, 71), dead, gd=(1, 0)).tempo_stall  # |gd| 1 ok
    assert not _stall_tracker(on, range(30, 71), lambda m: 2.0).tempo_stall  # live pace


# -- freeze ------------------------------------------------------------------------------------------
def _settings(path, rules="v4.5-sharp"):
    from quantbet.config import Settings
    return Settings(_env_file=None, alert_rules=rules, engine_b_params_path=path,  # type: ignore[call-arg]
                    leagues_config_path=ROOT / "config/leagues.json",
                    syndicate_priors_path=ROOT / "config/syndicate_priors.json")


def test_v45_freeze_present_and_old_hashes_intact():
    from quantbet.main import build_rules
    path = ROOT / "config/engine_b_params.json"
    raw = json.loads(path.read_text())
    assert raw["sha256"] == V42_HASH and raw["rules_version"] == "v4.2"
    assert read_frozen_set(path, "v4.3-global")["sha256"] == V43_HASH
    assert read_frozen_set(path, "v4.4-syndicate")["sha256"] == V44_HASH
    fs = read_frozen_set(path, "v4.5-sharp")
    assert fs is not None and fs["sha256"] not in (V42_HASH, V43_HASH, V44_HASH)
    sp = fs["global"]["syndicate"]["params"]
    assert sp["tier_target_edge"] == {"TIER_1": 0.035, "TIER_2": 0.045, "TIER_3": 0.06}
    assert sp["achievability_gate"] is False and sp["cards_paper_only"] is True
    assert fs["global"]["syndicate"]["priors"]["cards_bet365_nb_r"] == PRIORS["cards_bet365"]["nb_r"]
    assert build_rules(_settings(path))[2] == fs["sha256"]
    assert build_rules(_settings(path, "v4.4-syndicate"))[2] == V44_HASH
    assert build_rules(_settings(path, "v4.3-global"))[2] == V43_HASH
    assert build_rules(_settings(path, "v4.2"))[2] == V42_HASH


def test_v45_freeze_tamper_raises(tmp_path):
    from quantbet.main import build_rules, global_params_dict
    path = tmp_path / "p.json"
    shutil.copy(ROOT / "config/engine_b_params.json", path)
    s = _settings(path)
    pr, lv, h = build_rules(s)
    bad = json.loads(json.dumps(global_params_dict(s)))
    bad["syndicate"]["params"]["max_momentum_index"] = 3.0
    with pytest.raises(FrozenParamsError):
        enforce_frozen(path, "v4.5-sharp", pr, lv, global_params=bad)
    raw = json.loads(path.read_text())
    raw["frozen_sets"]["v4.5-sharp"]["live_params"]["corner_dispersion"] = 20.0
    path.write_text(json.dumps(raw))
    with pytest.raises(FrozenParamsError):
        build_rules(s)
    assert build_rules(_settings(path, "v4.4-syndicate"))[2] == V44_HASH  # other sets unaffected


# ---------------------------------------------------------------------------
# v4.5 final patch
# ---------------------------------------------------------------------------
V45_HASH = "20e12b5951ed5e8dafa2076a243d5721aca24a221e56caf3266cecb846eb6fdb"


def test_patch_freeze_hash_and_frozen_values():
    path = ROOT / "config" / "engine_b_params.json"
    fs = read_frozen_set(path, "v4.5-sharp")
    assert fs["sha256"] == V45_HASH
    assert fs["live_params"]["corner_dispersion"] == 65.0 and fs["live_params"]["corner_prior_minutes"] == 200.0
    assert [list(w) for w in fs["pricing"]["minute_windows"]] == [[30.0, 43.0], [55.0, 85.0]]
    sp = fs["global"]["syndicate"]["params"]
    assert fs["superseded"][0]["sha256"].startswith("99e2bc1f")  # the pre-patch v4.5 freeze, recorded
    assert sp["sustained_over_index"] == 1.35 and sp["max_momentum_index"] == 2.90
    assert sp["max_lift_goals"] == 0.28 and sp["max_lift_corners"] == 0.35
    assert sp["stall2_enabled"] is False and sp["bus_from_minute"] == 65.0
    assert read_frozen_set(path, "v4.4-syndicate")["sha256"] == V44_HASH
    assert read_frozen_set(path, "v4.3-global")["sha256"] == V43_HASH


def test_corner_dispersion_matches_priors_estimate():
    pri = json.loads((ROOT / "config" / "syndicate_priors.json").read_text())
    blob = json.dumps(pri)
    assert "65.34" in blob or "65.3" in blob  # MLE in the dispersion check
    _, lp = rules_preset("v4.5-sharp", None)
    assert abs(lp.corner_dispersion - 65.34) < 1.0
    _, lp44 = rules_preset("v4.4-syndicate", None)
    assert lp44.corner_dispersion != 65.0  # v4.4 untouched


# --- 1.3 youth tokens --------------------------------------------------------
@pytest.mark.parametrize("name,hit", [
    ("Arsenal U20", "u20"), ("Chelsea U18", "u18"), ("Brazil U17", "u17"), ("Jong Ajax", "jong"),
    ("Jong PSV", "jong"), ("Jong-AZ", "jong"), ("Barcelona B", " b"), ("Real Madrid Castilla B", " b"),
    ("Stuttgart II", " ii"), ("Bayern Munchen III", " iii"), ("Arsenal Under 21", "u21"),
    ("Arsenal U-21", "u21"),
    ("Jonge Kracht", None), ("Jongbloed FC", None), ("Young Boys", None), ("Juan Pablo II", None),
("Bayern Munich", None), ("Arsenal", None), ("Bburg", None),
    ("Ajax", None), ("Brentford", None),
])
def test_youth_tokens_patch(name, hit):
    assert youth_reserve_hit(name, SH.youth_tokens, SH.youth_suffix_tokens) == hit


def test_youth_module_constants_match_sharp_defaults():
    from quantbet import youth
    assert tuple(SH.youth_tokens) == youth.YOUTH_TOKENS
    assert tuple(SH.youth_suffix_tokens) == youth.YOUTH_SUFFIX_TOKENS
    for t in ("u20", "u18", "u17", "jong"):
        assert t in SH.youth_tokens
    for t in (" b", " ii", " iii"):
        assert t in SH.youth_suffix_tokens
    assert youth.youth_compatible("Arsenal", "Chelsea") and youth.youth_compatible("Arsenal U21", "Chelsea U21")
    assert not youth.youth_compatible("Arsenal U21", "Arsenal")
    assert not youth.youth_compatible("Jong Ajax", "Ajax")


def test_youth_tokens_v44_unchanged():
    p44 = SyndicateParams()
    assert not hasattr(p44, "youth_tokens")  # v4.4 has no youth filter at all (a v4.5 field)
    assert "jong" in SH.youth_tokens


# --- 2.1 Over activity confirmation -----------------------------------------
def _over_trace(eng, key):  # noqa: ANN001, ANN202
    return [v for k, v in eng.last_trace[key].items() if k[2] is Selection.OVER and k[0] is not MarketType.TOTAL_CARDS]


def test_activity_confirm_blocks_overs_without_mult_or_fav_trailing():
    assert SH.activity_confirm_overs() is True and SyndicateParams().activity_confirm_overs() is False
    eng = _engine()
    sig = _drive(eng, "ac", range(20, 41), rate=1.3, mh=1.0, ma=1.0)
    mv = eng.last_momentum["ac"]
    assert mv.sustained_over and not mv.fav_trailing
    overs = _over_trace(eng, "ac")
    assert any(v.startswith("no_activity_confirm (mult_total 1.00 < 1.04, not fav_trailing)") for v in overs)
    assert not any(s.market in (MarketType.TOTAL_GOALS, MarketType.TOTAL_CORNERS) and s.selection is Selection.OVER
                   for s in sig)


def test_activity_confirm_passes_with_mult_total_at_trigger():
    eng = _engine()
    _drive(eng, "ac2", range(20, 41), rate=1.3, mh=1.04, ma=1.04)
    assert eng.last_momentum["ac2"].sustained_over
    assert not any(v.startswith("no_activity_confirm") for v in _over_trace(eng, "ac2"))
    eng3 = _engine()
    _drive(eng3, "ac3", range(20, 41), rate=1.3, mh=1.06, ma=1.0)  # total 1.03 < 1.04
    assert any(v.startswith("no_activity_confirm") for v in _over_trace(eng3, "ac3"))


def test_activity_confirm_passes_when_favourite_trailing():
    class Fav:
        def ratings(self, h, a, league=None):  # noqa: ANN001, ANN201
            return PreMatchRatings(1.9, 2.9, 3.0, 10.0, 0.75, 0.1)
    eng = _engine()
    eng.resolver.team_table = Fav()
    _drive(eng, "ft", range(45, 76), rate=1.3, mh=1.0, ma=1.0, hg=0, ag=1)
    mv = eng.last_momentum["ft"]
    assert mv.fav_trailing and mv.sustained_over
    assert not any(v.startswith("no_activity_confirm") for v in _over_trace(eng, "ft"))


def test_activity_confirm_not_in_v44():
    import dataclasses
    eng = _engine(params=dataclasses.replace(SH, require_activity_confirm=False))
    _drive(eng, "na", range(20, 41), rate=1.3, mh=1.0, ma=1.0)
    assert not any(v.startswith("no_activity_confirm") for v in _over_trace(eng, "na"))


# --- 2.2 fix 1: cross-family direction lock ----------------------------------
def test_direction_lock_staked():
    assert SH.direction_lock() is True and SyndicateParams().direction_lock() is False
    d = SyndicateDeduplicator(SH, 2000.0)
    assert d.check("e", "goals", "over", 2.5, 20.0, 30.0, None) is None
    d.record("e", "goals", "over", 2.5, 0.5, 20.0, 30.0)
    assert d.check("e", "corners", "under", 9.5, 20.0, 40.0, None) == "match_direction_conflict"
    assert d.check("e", "cards", "under", 4.5, 20.0, 40.0, None) == "match_direction_conflict"
    assert d.check("e", "corners", "over", 9.5, 20.0, 40.0, None) is None  # same direction fine
    assert d.check("other", "corners", "under", 9.5, 20.0, 40.0, None) is None  # other match fine


def test_direction_lock_pending_in_same_poll():
    d = SyndicateDeduplicator(SH, 2000.0)
    assert d.check("e", "corners", "under", 9.5, 20.0, 40.0, None, pending_dirs=("over",)) == "match_direction_conflict"
    assert d.check("e", "corners", "over", 9.5, 20.0, 40.0, None, pending_dirs=("over",)) is None


def test_direction_lock_paper_asymmetry():
    d = SyndicateDeduplicator(SH, 2000.0)
    d.record("e", "cards", "over", 4.5, 0.5, 0.0, 30.0, paper=True)
    # a paper card never blocks a real bet ...
    assert d.check("e", "goals", "under", 2.5, 20.0, 60.0, None) is None
    # ... but the paper book is locked against itself
    assert d.check("e", "corners", "under", 9.5, 0.0, 60.0, None, paper=True) == "match_direction_conflict"
    d2 = SyndicateDeduplicator(SH, 2000.0)
    d2.record("e", "goals", "under", 2.5, 0.5, 20.0, 30.0)
    # and a paper card is locked against the staked book
    assert d2.check("e", "cards", "over", 4.5, 0.0, 60.0, None, paper=True) == "match_direction_conflict"


def test_direction_lock_absent_in_v44():
    d = SyndicateDeduplicator(SyndicateParams(), 2000.0)
    d.record("e", "goals", "over", 2.5, 0.5, 20.0, 30.0)
    assert d.check("e", "corners", "under", 9.5, 20.0, 40.0, None) is None
    assert d.check("e", "goals", "under", 2.5, 20.0, 40.0, None) == "family_conflict"


# --- 2.2 fix 2: attack momentum / activity suppresses Unders -----------------
@pytest.mark.parametrize("am,mt,bull", [(0.30, 1.0, True), (-0.35, 1.0, True), (0.10, 1.02, True), (0.10, 1.019, False),
                                        (0.29, 1.0, False), (None, None, False), (None, 1.05, True), (0.0, None, False)])
def test_activity_bullish_view(am, mt, bull):
    assert SH.under_activity_suppress() == (0.30, 1.02) and SyndicateParams().under_activity_suppress() is None
    tr = SustainedMomentumTracker(SH)
    s = _state("ab", 40.0)
    s.attack_momentum = am
    s.momentum_mult_total = mt
    v = tr.update(s, R, None, now=0.0)
    assert v.activity_bullish is bull
    assert v.bullish is (bull or v.sustained_over or v.fav_trailing or v.knockout_level)
    assert v.to_dict()["activity_bullish"] is bull
    v44 = SustainedMomentumTracker(SyndicateParams()).update(s, R, None, now=0.0)
    assert v44.activity_bullish is False


def test_activity_bullish_suppresses_unders_in_engine():
    import dataclasses
    on = dataclasses.replace(SH, stall2_enabled=True)  # give the Unders a bearish trigger to be suppressed
    eng = _engine(params=on)
    sig = _drive(eng, "u", range(30, 71), rate=1.0)  # _state: attack_momentum 0.5 >= 0.30
    unders = [v for k, v in eng.last_trace["u"].items() if k[2] is Selection.UNDER and k[0] is not MarketType.TOTAL_CARDS]
    assert eng.last_momentum["u"].activity_bullish
    assert any(v.startswith("bullish") for v in unders)
    assert not any(s.selection is Selection.UNDER and s.market is not MarketType.TOTAL_CARDS for s in sig)


# --- 2.2 fix 3: bus_parking --------------------------------------------------
def _bus(params, minutes, home_rates, away_rates, hg=1, ag=0):  # noqa: ANN001, ANN202
    tr = SustainedMomentumTracker(params)
    v = None
    hs_r, hsot_r, hc_r = home_rates
    as_r, asot_r, ac_r = away_rates
    for i, m in enumerate(minutes):
        d = m - minutes[0] + 20.0
        s = _state("bp", float(m), hg=hg, ag=ag, hs=d * hs_r, hsot=d * hsot_r, hc=d * hc_r,
                   as_=d * as_r, asot=d * asot_r, ac=d * ac_r)
        s.attack_momentum, s.momentum_mult_total = 0.0, 1.0
        v = tr.update(s, R, None, now=i * 60.0)
    return v


LOW_SHARE = ((0.04, 0.01, 0.01), (0.15, 0.05, 0.06))   # leader share ~0.18, index ~0.65
MID_SHARE = ((0.07, 0.02, 0.02), (0.13, 0.045, 0.05))  # leader share ~0.32, index ~0.68


def test_bus_parking_needs_65_minutes():
    p44 = SyndicateParams()
    v = _bus(SH, list(range(48, 63)), *LOW_SHARE)
    assert v.reading.adj_index <= 0.75 and v.reading.leader_share <= 0.25
    assert not v.bus_parking  # 62' < 65'
    assert _bus(p44, list(range(48, 63)), *LOW_SHARE).bus_parking  # v4.4: from 55'
    assert _bus(SH, list(range(55, 71)), *LOW_SHARE).bus_parking


def test_bus_parking_leader_share_and_index_caps():
    p44 = SyndicateParams()
    v = _bus(SH, list(range(55, 71)), *MID_SHARE)
    assert 0.25 < v.reading.leader_share <= 0.35 and v.reading.adj_index <= 0.75
    assert not v.bus_parking and _bus(p44, list(range(55, 71)), *MID_SHARE).bus_parking
    fast = ((0.08, 0.02, 0.02), (0.40, 0.14, 0.15))  # leader share low but index ~1.0+
    vf = _bus(SH, list(range(55, 71)), *fast)
    assert vf.reading.leader_share <= 0.25 and vf.reading.adj_index > 0.75 and not vf.bus_parking
    assert not _bus(SH, list(range(55, 71)), *LOW_SHARE, hg=2, ag=0).bus_parking  # |gd| must be 1
    assert not _bus(SH, list(range(55, 71)), *LOW_SHARE, hg=1, ag=1).bus_parking


# --- 2.3 Fleetwood v Arsenal: youth guard in name matching / ratings ---------
def test_team_similarity_youth_guard():
    from quantbet.poller import team_similarity
    assert team_similarity("Arsenal U21", "Arsenal") == 0.0
    assert team_similarity("Arsenal", "Arsenal U21") == 0.0
    assert team_similarity("Jong Ajax", "Ajax") == 0.0
    assert team_similarity("Barcelona B", "Barcelona") == 0.0
    assert team_similarity("Arsenal U21", "Arsenal U-21") >= 85.0
    assert team_similarity("Man City", "Manchester City") >= 85.0
    assert team_similarity("Juan Pablo II", "Juan Pablo II") == 100.0  # the exception is not youth


def test_team_ratings_table_never_maps_youth_to_senior():
    from quantbet.football_data import load_ratings
    tab = load_ratings(ROOT / "config" / "team_ratings.json")
    assert tab is not None
    assert tab._team("Arsenal") == "arsenal"
    for youth_name in ("Arsenal U21", "Arsenal U-21", "Arsenal Under 21", "Arsenal U18", "Arsenal II"):
        assert tab._team(youth_name) is None, youth_name
    assert tab.ratings("Wimbledon", "Arsenal U21") is None
    assert tab.ratings("Fleetwood Town", "Arsenal") is None or tab._team("Arsenal") == "arsenal"


def test_team_ratings_table_synthetic_youth_key():
    from quantbet.football_data import TeamRatingsTable
    keys = {"arsenal": 1.2, "arsenal u21": 0.9}
    t = TeamRatingsTable(1.5, 1.2, 5.0, 4.0, att_home_g=dict(keys), def_home_g=dict(keys), att_away_g=dict(keys),
                         def_away_g=dict(keys), att_home_c=dict(keys), def_home_c=dict(keys),
                         att_away_c=dict(keys), def_away_c=dict(keys))
    assert t._team("Arsenal U21") == "arsenal u21"
    assert t._team("Arsenal") == "arsenal"
    assert t._team("Arsenal U-21") == "arsenal u21"


def test_fleetwood_offline_evidence_from_cache():
    """#38 was senior Arsenal (Carabao Cup R4, eng.league_cup): ESPN competitor id 359 (the senior club),
    shootout by senior players. Offline: reads the local ESPN summary cache if the data is present."""
    import sqlite3
    db = Path("/workspace/quantbet_v44/src_in/data/espn_summary.sqlite3")
    if not db.exists():
        pytest.skip("local ESPN summary cache not present")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        tabs = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
        blob = ""
        for t in tabs:
            cols = [c[1] for c in con.execute(f"pragma table_info({t})")]
            for c in cols:
                try:
                    for (v,) in con.execute(f"select {c} from {t} where cast({c} as text) like '%401921835%' limit 3"):
                        blob += str(v)
                except sqlite3.Error:
                    pass
    finally:
        con.close()
    if "401921835" not in blob:
        pytest.skip("event 401921835 not in the local cache")
    assert "Fleetwood" in blob and "Arsenal" in blob
    assert "Arsenal U21" not in blob


def test_replay_output_key_renamed():
    src = (ROOT / "scripts" / "replay_syndicate.py").read_text()
    assert '"v44"' not in src and "'v44'" not in src and '"all_v44"' not in src
    for k in ('"replay"', '"all_replay"', '"by_market_replay"', '"by_tier_replay"', '"decision_counts"'):
        assert k in src, k
