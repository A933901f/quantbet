"""v4.3-global: league registry/tiers, live-stats integrity gate, auto-discovery, slug priors, tier sizing,
card tag, separate freeze (v4 / v4.1 / v4.2 untouched), Football-Data extra-league loader."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import shutil
import time
from pathlib import Path

import pytest

from quantbet.engine import PreMatchRatings, SignalEngine, rules_preset
from quantbet.espn import LEAGUE_NAMES, split_all_scoreboard
from quantbet.free_stats_poller import FreePollerConfig, FreeStatsPoller
from quantbet.frozen import FrozenParamsError, enforce_frozen, param_set, params_hash, read_frozen_set, write_frozen_set
from quantbet.global_layer import (GlobalParams, GlobalRatingsResolver, GlobalSignalEngine, LeagueInfo, LeagueRegistry,
                                   liquidity_tag, load_registry, telemetry_check)
from quantbet.models import Selection
from quantbet.risk import RiskConfig, RiskManager, round_stake
from test_free_stack import FakeTransport, fetcher, js
from test_rules_v4 import R_FAV, _st

ROOT = Path(__file__).resolve().parents[1]
V42_HASH = "40e806b910db93de34f9b26d9ebf361168077f1f38a8fe8d7dbc4e7cb6729aec"
TIER_KW = dict(tier_kelly_mult=(("TIER_1", 1.0), ("TIER_2", 1.0), ("TIER_3", 0.5)), tier_max_units=(("TIER_3", 15.0),))


# -- registry ------------------------------------------------------------------------
def test_registry_has_40_plus_leagues_and_documented_tiers():
    reg = load_registry(ROOT / "config/leagues.json")
    assert len(reg.supported_slugs()) >= 40
    for s in ("eng.1", "esp.1", "ger.1", "ita.1", "fra.1", "uefa.champions", "uefa.europa", "usa.1", "bra.1"):
        assert reg.tier_of(s) == "TIER_1", s
    for s in ("eng.2", "esp.2", "ger.2", "ita.2", "fra.2", "ned.1", "mex.1", "arg.1", "den.1", "nor.1", "swe.1",
              "aus.1", "jpn.1", "por.1", "bel.1", "tur.1", "sco.1"):
        assert reg.tier_of(s) == "TIER_2", s
    assert reg.tier_of("eng.3") == "TIER_3" and reg.tier_of("espn.3932") == "TIER_3" and reg.tier_of(None) == "TIER_3"
    assert not reg.leagues["kor.1"].supported and "kor.1" not in reg.supported_slugs()
    assert {s: reg.corner_baseline(s) for s in ("aus.1", "eng.2", "bra.1", "usa.1", "mex.1", "arg.1", "jpn.1")} == {
        "aus.1": 11.2, "eng.2": 10.4, "bra.1": 10.5, "usa.1": 9.8, "mex.1": 9.7, "arg.1": 9.6, "jpn.1": 9.6}
    assert reg.corner_baseline("espn.999") == 9.8 and reg.corner_baseline("col.1") == 9.8
    assert reg.priority("eng.1") < reg.priority("arg.1") < reg.priority("eng.3") < reg.priority("espn.1")
    ex = json.loads((ROOT / "config/leagues.example.json").read_text())
    assert len(ex["leagues"]) >= 40 and "high_liquidity_leagues" in ex


# -- Live Stats Integrity Gate ---------------------------------------------------------
Z = {"wonCorners": 0.0, "totalShots": 0.0, "shotsOnTarget": 0.0}


def test_telemetry_gate_rules():
    assert telemetry_check({}, {}, 29.0) == (True, "pre_gate")
    assert telemetry_check({}, {}, 30.0) == (False, "missing_keys")
    assert telemetry_check({"wonCorners": 3}, {"wonCorners": 1}, 40.0) == (False, "missing_keys")
    assert telemetry_check(Z, Z, 37.0) == (False, "all_zero")
    assert telemetry_check(dict(Z, totalShots=1.0), Z, 37.0) == (True, "ok")
    assert telemetry_check(Z, dict(Z, wonCorners=2.0), 70.0) == (True, "ok")
    assert telemetry_check(dict(Z, shotsOnTarget=2.0), Z, 50.0) == (False, "all_zero")  # SOT alone is not activity


def _ev(eid, lid, home, away, minute, stats, state="in"):
    st = [{"name": k, "displayValue": str(v)} for k, v in stats.items()] if stats is not None else None
    comp = {"competitors": [
        {"homeAway": "home", "score": "0", "team": {"id": f"{eid}1", "displayName": home}, **({"statistics": st} if st else {})},
        {"homeAway": "away", "score": "0", "team": {"id": f"{eid}2", "displayName": away}, **({"statistics": st} if st else {})}]}
    return {"id": eid, "uid": f"s:600~l:{lid}~e:{eid}", "date": "2026-10-25T23:00Z",
            "status": {"clock": minute * 60, "displayClock": f"{minute}'", "period": 1 if minute <= 45 else 2,
                       "type": {"state": state, "name": "STATUS_IN_PROGRESS" if state == "in" else "STATUS_SCHEDULED",
                                "detail": f"{minute}'", "shortDetail": f"{minute}'"}},
            "competitions": [comp]}


ALL = {"leagues": [{}], "events": [
    _ev("1", "745", "River Plate", "Racing Club", 58, {"wonCorners": 5, "totalShots": 5, "shotsOnTarget": 4}),
    _ev("2", "3932", "Tapatio", "Morelia", 37, dict(Z)),                       # fake zeros (unlisted)
    _ev("3", "5487", "Coastal Carolina", "Old Dominion", 63, None),           # no stats at all (unlisted)
    _ev("4", "8301", "San Diego Wave", "Boston Legacy", 28, {"wonCorners": 1, "totalShots": 4, "shotsOnTarget": 2}),
    _ev("5", "670", "Garcilaso", "Los Chankas", 36, {"wonCorners": 2, "totalShots": 4, "shotsOnTarget": 1}),
    _ev("6", "3906", "Sydney FC", "Adelaide", 0, dict(Z), state="pre"),
]}


def _poller(cfg, transport):
    got = []
    p = FreeStatsPoller(fetcher(transport), got.append, cfg=cfg)
    p._ids_learnt = True
    return p, got


def test_split_all_scoreboard_auto_discovery():
    from quantbet.espn import register_league_id
    register_league_id("745", "arg.1")
    plain = split_all_scoreboard(ALL, ["arg.1"])
    assert set(plain) == {"arg.1"}
    auto = split_all_scoreboard(ALL, ["arg.1"], auto_discover=True)
    assert {"arg.1", "espn.3932", "espn.5487", "espn.8301", "espn.670"} <= set(auto)
    assert "espn.3906" not in auto  # pre-match events of unlisted leagues are not ingested
    assert LEAGUE_NAMES["espn.8301"].startswith("ESPN league")


def test_poller_gate_skips_and_logs_no_live_telemetry(caplog):
    from quantbet.espn import register_league_id
    register_league_id("745", "arg.1")
    t = FakeTransport({"soccer/all/scoreboard": js(ALL)})
    cfg = FreePollerConfig(espn_leagues=("arg.1",), use_espn_summary=False, auto_discover=True, telemetry_gate_minute=30.0,
                           espn_all_limit=500, fallback_listed_only=True, max_events=40, synthetic_momentum=False)
    p, _ = _poller(cfg, t)
    with caplog.at_level(logging.INFO):
        states = asyncio.run(p.poll_espn_once())
    assert any("limit=500" in u for u, _ in t.calls)
    names = {s.home for s in states}
    assert names == {"River Plate", "San Diego Wave", "Garcilaso"}  # 28' passes pre-gate; zeros / no-stats skipped
    slugs = {s.home: s.league_slug for s in states}
    assert slugs["River Plate"] == "arg.1" and slugs["San Diego Wave"] == "espn.8301"
    msgs = [r for r in caplog.records if r.getMessage() == "skipped: no_live_telemetry"]
    assert {getattr(r, "why", None) for r in msgs} == {"all_zero", "missing_keys"} and len(msgs) == 2
    assert {v["match"] for v in p.telemetry_skips.values()} == {"Tapatio v Morelia", "Coastal Carolina v Old Dominion"}
    # second cycle: still skipped, but logged only once per match/reason
    caplog.clear()
    with caplog.at_level(logging.INFO):
        asyncio.run(p.poll_espn_once(now=time.time() + 120))
    assert not [r for r in caplog.records if r.getMessage() == "skipped: no_live_telemetry"]


def test_poller_default_config_is_v42_behaviour():
    from quantbet.espn import register_league_id
    register_league_id("745", "arg.1")
    t = FakeTransport({"soccer/all/scoreboard": js(ALL)})
    p, _ = _poller(FreePollerConfig(espn_leagues=("arg.1",), use_espn_summary=False, synthetic_momentum=False), t)
    states = asyncio.run(p.poll_espn_once())
    assert [s.home for s in states] == ["River Plate"]
    assert all("limit=" not in u for u, _ in t.calls)


# -- priors ------------------------------------------------------------------------------
class _Table:
    def ratings(self, home, away, league=None):  # noqa: ANN001
        return R_FAV


def test_resolver_pickcenter_prior_fav_prob_and_lowconf_fallback():
    from quantbet.engine import SharpPrices
    reg = load_registry(ROOT / "config/leagues.json")
    sp = SharpPrices(home=1.54, draw=4.4, away=4.9, total_line=3.5, over=2.05, under=1.74)
    res = GlobalRatingsResolver(reg, None, _Table(), prior_lookup=lambda k: (sp, "espn_draftkings") if k == "a" else None,
                                slug_lookup=lambda k: {"a": "usa.1", "b": "aus.1", "c": "espn.680", "d": "eng.1"}[k])
    r, src = res.resolve("a", "LAFC", "LA Galaxy")
    assert src == "espn_draftkings" and r.corner_total == 9.8
    info = res.prior_info["a"]
    assert info["confidence"] == "market" and 0.6 < info["fav_prob"] < 0.7 and info["fav"] == "home"
    assert 2.9 < info["lambda_goals_prior"] < 3.8
    r2, src2 = res.resolve("b", "Sydney", "Adelaide")
    assert src2 == "league_default" and r2.corner_total == 11.2 and res.prior_info["b"]["confidence"] == "low"
    r3, src3 = res.resolve("c", "Nacional", "Liverpool")  # never the EPL team table for an Uruguayan "Liverpool"
    assert src3 == "league_default" and r3.goal_total == reg.default_goals_baseline and r3.corner_total == 9.8
    _, src4 = res.resolve("d", "Liverpool", "Arsenal")
    assert src4 == "football_data"


def test_lowconf_prior_is_gated_no_alerts():
    reg = LeagueRegistry({"x.1": LeagueInfo("x.1", "X", tier="TIER_1")})
    eng = _geng(reg, table_slugs=())  # no table, no pickcenter -> league_default (low confidence)
    st = _st(minute=35, hg=2, ag=2, hc=3, ac=2, mh=1.25, ma=1.25, league_slug="x.1", key="lc")
    assert eng.on_match_state(st, st.updated_at) == []
    assert any("default" in w for w in eng.last_trace["lc"].values())


# -- tier sizing ----------------------------------------------------------------------------
def test_risk_tier3_is_eighth_kelly_capped_15_units_rounded():
    rm = RiskManager(RiskConfig(bankroll=2000, unit_size=10, **TIER_KW))
    base = RiskManager(RiskConfig(bankroll=2000, unit_size=10))
    f = 0.02  # quarter-Kelly fraction -> 40 (T1) vs 20 (T3)
    assert rm.stake_from_kelly(f, "TIER_1") == base.stake_from_kelly(f) == 40
    assert rm.stake_from_kelly(f, "TIER_3") == 20
    assert rm.stake_from_kelly(0.0137, "TIER_3") == 15  # 13.7 -> nearest 5
    big = RiskManager(RiskConfig(bankroll=100_000, unit_size=10, **TIER_KW))
    assert big.stake_from_kelly(0.02, "TIER_1") == 2000  # 3% cap = 3000 not binding
    assert big.stake_from_kelly(0.02, "TIER_3") == 150  # 1000 -> 15 units cap
    assert big.tier_cap("TIER_3") == 150 and big.tier_cap("TIER_2") == 3000
    for raw in (7.0, 12.4, 99.0, 131.0):
        v = big.stake_from_kelly(raw / 100_000 / 0.5, "TIER_3")
        assert v <= 150 and (v % 5 == 0) and (v < 100 or v % 10 == 0)
    assert round_stake(149.0, big.cfg, 150) == 150 and round_stake(156, big.cfg, 150) == 150


def _geng(reg, table_slugs=("t1.x", "t3.x"), bankroll=2000.0):
    pricing, live = rules_preset("v4.3-global", None, bankroll=bankroll)
    rm = RiskManager(RiskConfig(bankroll=bankroll, unit_size=10, **TIER_KW))
    res = GlobalRatingsResolver(reg, None, _Table(), team_table_slugs=table_slugs)
    return GlobalSignalEngine(reg, rm, res, pricing, SignalEngine(live_params=live), sizer=lambda f, o: rm.stake_from_kelly(f))


def test_engine_tier3_halves_stakes_and_tags_card():
    from quantbet.notifier import format_min_odds_signal
    reg = LeagueRegistry({"t1.x": LeagueInfo("t1.x", "One", tier="TIER_1"), "t3.x": LeagueInfo("t3.x", "Three", tier="TIER_3")})
    out = {}
    for slug in ("t1.x", "t3.x"):
        for bank in (2000.0, 100_000.0):
            eng = _geng(reg, bankroll=bank)
            st = _st(minute=35, hg=2, ag=2, hc=3, ac=2, mh=1.25, ma=1.25, league_slug=slug, key=f"{slug}{bank}")
            sigs = eng.on_match_state(st, st.updated_at)
            assert sigs and all(s.selection is Selection.OVER for s in sigs)
            out[(slug, bank)] = sigs
    for bank in (2000.0, 100_000.0):
        t1, t3 = out[("t1.x", bank)], out[("t3.x", bank)]
        assert [(s.market, s.line) for s in t1] == [(s.market, s.line) for s in t3]  # same v4.2 selection
        for a, b in zip(t1, t3):
            assert a.extra["liquidity_tier"] == "TIER_1" and b.extra["liquidity_tier"] == "TIER_3"
            assert max(x[1] for x in b.extra["tiers"]) <= max(x[1] for x in a.extra["tiers"])
            assert all(st_ <= 150 for _, st_ in b.extra["tiers"])
            assert a.extra["tiers"][0][1] >= 2 * b.extra["tiers"][0][1] - 5  # ~half, modulo 5/10 rounding
    big3 = out[("t3.x", 100_000.0)]
    assert max(x[1] for s in big3 for x in s.extra["tiers"]) == 150  # the 15-unit cap binds at a large bankroll
    txt = format_min_odds_signal(out[("t3.x", 2000.0)][0])
    assert "[LIQUIDITY: TIER 3 - SMALL LEAGUE]" in txt
    assert "[LIQUIDITY: TIER 1 - MAJOR LEAGUE]" in format_min_odds_signal(out[("t1.x", 2000.0)][0])
    assert liquidity_tag("TIER_2") == "[LIQUIDITY: TIER 2 - MID LIQUIDITY]"


def test_risk_size_respects_tier_and_card_without_tier_unchanged():
    from quantbet.notifier import format_min_odds_signal
    reg = LeagueRegistry({"t3.x": LeagueInfo("t3.x", "Three", tier="TIER_3")})
    eng = _geng(reg, table_slugs=("t3.x",))
    st = _st(minute=35, hg=2, ag=2, hc=3, ac=2, mh=1.25, ma=1.25, league_slug="t3.x", key="rs")
    sig = eng.on_match_state(st, st.updated_at)[0]
    rm = eng.risk
    plain = RiskManager(RiskConfig(bankroll=2000, unit_size=10))
    s1 = rm.size(sig)
    assert s1.stake_currency <= 150 and any("TIER_3" in n for n in s1.risk_notes)
    sig.extra.pop("liquidity_tier")
    assert "[LIQUIDITY" not in format_min_odds_signal(sig)
    assert plain.size(sig).stake_currency >= s1.stake_currency


# -- freeze --------------------------------------------------------------------------------
def test_v43_freeze_is_separate_and_v42_hash_unchanged(tmp_path):
    from quantbet.config import Settings
    from quantbet.main import build_rules, global_params_dict
    path = tmp_path / "p.json"
    shutil.copy(ROOT / "config/engine_b_params.json", path)
    top_before = {k: v for k, v in json.loads(path.read_text()).items() if k != "frozen_sets"}
    assert top_before["sha256"] == V42_HASH and top_before["rules_version"] == "v4.2"
    p42, l42 = rules_preset("v4.2", None)
    p43, l43 = rules_preset("v4.3-global", None)
    assert param_set("v4.2", p42, l42)["pricing"] == param_set("v4.3-global", p43, l43)["pricing"]  # same maths
    s = Settings(_env_file=None, alert_rules="v4.3-global", engine_b_params_path=path)  # type: ignore[call-arg]
    gp = global_params_dict(s)
    pr, lv, _ = build_rules(Settings(_env_file=None, alert_rules="v4.2", engine_b_params_path=path))  # type: ignore[call-arg]
    from quantbet.main import load_live_params
    pr3, lv3 = rules_preset("v4.3-global", load_live_params(path), **{k: getattr(pr, k) for k in ("target_edge", "abort_edge", "kelly_multiplier", "min_minute", "max_minute", "realert_cooldown_s", "bankroll", "max_match_stake_frac", "family_cooldown_min")})
    h = write_frozen_set(path, "v4.3-global", pr3, lv3, gp)
    after = json.loads(path.read_text())
    assert {k: v for k, v in after.items() if k != "frozen_sets"} == top_before  # v4.2 entry untouched
    assert read_frozen_set(path, "v4.3-global")["sha256"] == h != V42_HASH
    assert build_rules(s)[2] == h
    assert build_rules(Settings(_env_file=None, alert_rules="v4.2", engine_b_params_path=path))[2] == V42_HASH  # type: ignore[call-arg]
    for v in ("v4", "v4.1"):  # superseded sets still runnable
        build_rules(Settings(_env_file=None, alert_rules=v, engine_b_params_path=path))  # type: ignore[call-arg]
    bad = dict(gp, telemetry_gate_minute=25.0)
    with pytest.raises(FrozenParamsError):
        enforce_frozen(path, "v4.3-global", pr3, lv3, global_params=bad)
    assert params_hash(param_set("v4.3-global", pr3, lv3, gp)) == h


def test_repo_freeze_file_has_v43_entry_matching_config():
    from quantbet.config import Settings
    from quantbet.main import build_rules
    fs = read_frozen_set(ROOT / "config/engine_b_params.json", "v4.3-global")
    assert fs is not None and fs["global"]["tier_kelly_mult"]["TIER_3"] == 0.5 and fs["global"]["tier_max_units"]["TIER_3"] == 15
    s = Settings(_env_file=None, alert_rules="v4.3-global", engine_b_params_path=ROOT / "config/engine_b_params.json",  # type: ignore[call-arg]
                 leagues_config_path=ROOT / "config/leagues.json")
    assert build_rules(s)[2] == fs["sha256"]
    assert json.loads((ROOT / "config/engine_b_params.json").read_text())["sha256"] == V42_HASH


def test_cli_accepts_v43():
    from quantbet.main import parse_args
    assert parse_args(["--dry-run", "--rules", "v4.3-global"]).rules == "v4.3-global"


# -- Football-Data extra leagues -------------------------------------------------------------
def test_load_extra_csv_maps_columns_and_has_no_corners():
    from quantbet.football_data import csv_url, league_baselines, load_extra_csv
    csv = ("Country,League,Season,Date,Time,Home,Away,HG,AG,Res,PSCH,PSCD,PSCA\n"
           "Argentina,Liga Profesional,2025,25/01/2025,20:00,River Plate,Racing Club,2,1,H,1.8,3.4,4.5\n"
           "Argentina,Liga Profesional,2024,25/01/2024,20:00,Boca,Velez,0,0,D,2.0,3.1,3.9\n")
    df = load_extra_csv(io.StringIO(csv), "ARG", seasons=["2025"])
    assert list(df["HomeTeam"]) == ["River Plate"] and df["FTHG"].iloc[0] == 2 and df["Div"].iloc[0] == "ARG"
    b = league_baselines(df)
    assert b["goals_avg"] == 3.0 and b["corners_avg"] is None
    assert csv_url("ARG", 2025).endswith("/new/ARG.csv") and csv_url("E1", 2025).endswith("/mmz4281/2526/E1.csv")
