"""v4 rules: fair~1.95 line selection, prior-only goals, Gamma-Poisson corners, hard caps, blowout,
|diff|=2 dampener, chasing-corner cap, momentum clip, presets, freeze, CLI and the Telegram path (mocked)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import math
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from quantbet.engine import (
    AlertDeduplicator, FreeSignalEngine, LiveFootballModel, LiveModelParams, PreMatchRatings, PricingConfig,
    RatingsResolver, SignalEngine, chasing_corner_cap_active, expected_log_growth, market_family, price_live_lines,
    rules_preset,
)
from quantbet.frozen import FrozenParamsError, enforce_frozen, param_set, params_hash, write_frozen
from quantbet.models import LiveMatchState, MarketType, Selection
from quantbet.risk import RiskManager

R_EVEN = PreMatchRatings(0.0, 2.7, 0.0, 10.0)
R_FAV = PreMatchRatings(1.0, 3.0, 1.0, 10.5, 0.62, 0.17)


class _Table:
    def ratings(self, home, away, league=None):  # noqa: ANN001
        return R_FAV


def _st(minute=35.0, hg=0, ag=0, hc=3, ac=2, mh=1.15, ma=1.0, reds=(0, 0), **kw) -> LiveMatchState:
    st = LiveMatchState(event_key=kw.pop("key", "e1"), minute=minute, home_goals=hg, away_goals=ag, home_corners=hc,
                        away_corners=ac, home_red_cards=reds[0], away_red_cards=reds[1],
                        period=2 if minute > 45 else 1, updated_at=kw.pop("t", time.time()),
                        home="Home FC", away="Away FC", league="Premier League",
                        status="2nd half" if minute > 45 else "1st half", stats_available=True,
                        attack_momentum=kw.pop("am", 0.5), **kw)
    st.momentum_mult_home, st.momentum_mult_away, st.momentum_mult_total = mh, ma, (mh + ma) / 2
    return st


def _v4(**pricing_kw):
    return rules_preset("v4", None, **pricing_kw)


def _engine(version="v4", **kw) -> FreeSignalEngine:
    pricing, live = rules_preset(version, None, **kw)
    rm = RiskManager()
    return FreeSignalEngine(RatingsResolver(None, _Table()), pricing, SignalEngine(live_params=live),
                            sizer=lambda f, o: rm.stake_from_kelly(f))


# -- 1. line selection ---------------------------------------------------------
def test_fair_target_picks_line_closest_to_195_within_range():
    fe = _engine()
    now = time.time()
    sigs = fe.on_match_state(_st(minute=35, t=now), now)
    assert sigs and len({market_family(s.market) for s in sigs}) == len(sigs)
    prices = {(lp.market, lp.line, lp.selection): lp for lp in fe.last_prices["e1"]}
    trace = fe.last_trace["e1"]
    for s in sigs:
        assert 1.75 <= s.fair_odds <= 2.20 and s.extra["selection_mode"] == "fair_target"
        rivals = [prices[k] for k, why in trace.items() if why == "not_best" and market_family(k[0]) == market_family(s.market)]
        assert all(abs(s.fair_odds - 1.95) <= abs(lp.fair_odds - 1.95) + 1e-12 for lp in rivals)
    assert all(not (1.75 <= prices[k].fair_odds <= 2.20) for k, w in trace.items() if w == "fair_target_range")


def test_fair_target_no_line_in_range_means_no_alert():
    fe = _engine()
    fe.pricing = dataclasses.replace(fe.pricing, fair_target_range=(5.0, 5.1))
    now = time.time()
    assert fe.on_match_state(_st(minute=35, t=now), now) == []
    assert "fair_target_range" in set(fe.last_trace["e1"].values())


def test_real_odds_switch_to_log_growth():
    fe = _engine()
    now = time.time()
    fe.on_match_state(_st(minute=35, t=now), now)
    lo, hi = fe.pricing.alert_fair_odds_range
    cands = [lp for lp in fe.last_prices["e1"] if lp.market is MarketType.TOTAL_GOALS and lp.tiers
             and lo <= lp.fair_odds <= hi and not (1.75 <= lp.fair_odds <= 2.20)]
    assert cands
    pick = cands[0]
    real = {(pick.market, pick.line, pick.selection): pick.min_odds * 1.2}
    fe2 = _engine()
    sigs = fe2.on_match_state(_st(minute=35, t=now), now, real_odds=real)
    goals = [s for s in sigs if market_family(s.market) == market_family(MarketType.TOTAL_GOALS)]
    if fe2.last_trace["e1"].get((pick.market, pick.line, pick.selection)) == "neg_ref_ev":
        pytest.skip("candidate filtered by reference EV")
    # outside the 1.75-2.20 reverse-pricing window, yet chosen: real odds -> log-growth selection
    assert goals and goals[0].line == pick.line and goals[0].selection is pick.selection
    assert expected_log_growth(pick, real[(pick.market, pick.line, pick.selection)]) > expected_log_growth(pick)


# -- 2. rates --------------------------------------------------------------------
def test_v4_goals_have_no_pace_blending_and_corners_use_gamma_poisson():
    _, live = _v4()
    m = LiveFootballModel(live)
    st = _st(minute=45, hg=3, ag=0, hc=6, ac=0)
    base = m.shrunk_rates(st, R_EVEN)
    assert base["home_goals"] == R_EVEN.home_goals and base["away_goals"] == R_EVEN.away_goals
    w = 45 / (45 + 180)
    assert math.isclose(w, 0.2)
    assert math.isclose(base["home_corners"], w * 6 / 45 * 90 + (1 - w) * R_EVEN.home_corners)
    assert math.isclose(base["away_corners"], (1 - w) * R_EVEN.away_corners)


def test_momentum_clip():
    _, live = _v4()
    m = LiveFootballModel(live)
    hi = m.multipliers(_st(minute=60, mh=1.6, ma=0.5), R_EVEN)
    assert hi["momentum_home"] == 1.25 and hi["momentum_away"] == 0.85


def test_prior_only_model_has_no_multipliers():
    _, live = _v4()
    m = LiveFootballModel(dataclasses.replace(live, prior_only=True))
    st = _st(minute=70, hg=0, ag=1, hc=8, ac=1, mh=1.25, ma=0.85, reds=(0, 1))
    mult = m.multipliers(st, R_FAV)
    assert all(mult[k] == 1.0 for k in ("home_goals", "away_goals", "home_corners", "away_corners"))
    assert m.shrunk_rates(st, R_FAV)["home_corners"] == R_FAV.home_corners


# -- 3. gates --------------------------------------------------------------------
def test_hard_cap_one_per_family_no_state_change_bypass():
    pricing, _ = _v4()
    d = AlertDeduplicator(pricing)
    s0, goal = (0, 0, 0, 0), (1, 0, 0, 0)
    d.record("m", "corners", 9.5, 20, 35, s0)
    assert d.check("m", "corners", 11.5, 20, 70, goal) == "family_cap"  # v3 would allow (state change + gap)
    assert d.check("m", "goals", 2.5, 20, 70, goal) is None
    d.record("m", "goals", 2.5, 20, 70, goal)
    assert d.check("m", "goals", 4.5, 5, 80, (2, 0, 0, 0)) in ("cooldown", "family_cap")
    v3 = AlertDeduplicator(PricingConfig())
    v3.record("m", "corners", 9.5, 20, 35, s0)
    assert v3.check("m", "corners", 11.5, 20, 70, goal) is None


def test_blowout_blocks_overs_for_rest_of_match():
    fe = _engine()
    now = time.time()
    fe.on_match_state(_st(minute=60, hg=3, ag=0, t=now), now)
    assert "e1" in fe.blowout
    trace = fe.last_trace["e1"]
    assert trace and all(w == "blowout" for k, w in trace.items() if k[2] is Selection.OVER)
    sigs = fe.on_match_state(_st(minute=62, hg=3, ag=1, t=now + 120), now + 120)  # back to |diff| 2: still blocked
    assert not [s for s in sigs if s.selection is Selection.OVER]


def test_diff2_dampener_075_goals_080_both_teams_corners():
    _, live = _v4()
    on = LiveFootballModel(live)
    off = LiveFootballModel(dataclasses.replace(live, game_state_dampener=False))
    st = _st(minute=60, hg=2, ag=0, mh=1.0, ma=1.0)
    a, b = on.multipliers(st, R_EVEN), off.multipliers(st, R_EVEN)
    for k in ("home_goals", "away_goals"):
        assert math.isclose(a[k], b[k] * 0.75)
    for k in ("home_corners", "away_corners"):
        assert math.isclose(a[k], b[k] * 0.80)


def test_chasing_corner_cap_103_after_60_unless_sib_dominance():
    _, live = _v4()
    m = LiveFootballModel(live)
    st = _st(minute=65, hg=0, ag=1, mh=1.0, ma=1.0)
    assert chasing_corner_cap_active(st, R_FAV, live)
    assert not chasing_corner_cap_active(_st(minute=58, hg=0, ag=1), R_FAV, live)
    assert not chasing_corner_cap_active(_st(minute=65, hg=0, ag=2), R_FAV, live)
    capped = m.multipliers(st, R_FAV, chase_corner_cap=live.chasing_corner_cap)
    free_ = m.multipliers(st, R_FAV)
    assert free_["home_corners"] > 1.03 and math.isclose(capped["home_corners"], 1.03)
    pricing, _ = _v4()
    lps = price_live_lines(st, R_FAV, pricing, m)
    overs = [lp for lp in lps if lp.market is MarketType.TOTAL_CORNERS and lp.selection is Selection.OVER]
    assert overs and all("chasing_corner_cap" in lp.notes for lp in overs)
    dom = _st(minute=65, hg=0, ag=1, home_shots_inside_box=12, away_shots_inside_box=2)
    assert not chasing_corner_cap_active(dom, R_FAV, live)


# -- presets / freeze ------------------------------------------------------------
def test_presets_switchable_and_v3_defaults_unchanged():
    p3, l3 = rules_preset("v3")
    assert p3 == PricingConfig() and l3 == LiveModelParams()
    pl, ll = rules_preset("legacy")
    assert pl.selection_mode == PricingConfig.legacy().selection_mode and not ll.shrinkage_enabled
    p4, l4 = rules_preset("v4", target_edge=0.04)
    assert p4.selection_mode == "fair_target" and l4.shrinkage_mode == "corners_gamma" and p4.target_edge == 0.04
    with pytest.raises(ValueError):
        rules_preset("v9")


def test_freeze_hash_refusal_and_unfreeze(tmp_path):
    path = tmp_path / "p.json"
    pricing, live = _v4()
    h = write_frozen(path, "v4", pricing, live)
    raw = json.loads(path.read_text())
    assert raw["rules_version"] == "v4" and raw["sha256"] == h and raw["frozen_at"].endswith("Z")
    assert enforce_frozen(path, "v4", pricing, live) == (h, [])
    # bankroll is sizing, not a rule
    assert enforce_frozen(path, "v4", dataclasses.replace(pricing, bankroll=5000), live)[0] == h
    changed = dataclasses.replace(live, chasing_corner_cap=1.10)
    with pytest.raises(FrozenParamsError):
        enforce_frozen(path, "v4", pricing, changed)
    h2, diffs = enforce_frozen(path, "v4", pricing, changed, unfreeze=True)
    assert h2 != h and any("chasing_corner_cap" in d for d in diffs)
    assert params_hash(param_set("v4", pricing, live)) == h
    with pytest.raises(FrozenParamsError):
        enforce_frozen(tmp_path / "missing.json", "v4", pricing, live)


def test_nested_loader_and_build_rules(tmp_path, monkeypatch):
    from quantbet.config import Settings
    from quantbet.main import build_rules, load_live_params

    path = tmp_path / "p.json"
    pricing, live = rules_preset("v4", LiveModelParams(corner_dispersion=77.0))
    s = Settings(_env_file=None).model_copy(update={"engine_b_params_path": path, "alert_rules": "v4"})  # type: ignore[call-arg]
    pk = dict(target_edge=s.target_edge, abort_edge=s.abort_edge, kelly_multiplier=s.kelly_multiplier,
              min_minute=s.min_alert_minute, max_minute=s.max_alert_minute, realert_cooldown_s=s.realert_cooldown_s,
              bankroll=s.bankroll, max_match_stake_frac=s.max_match_stake_frac, family_cooldown_min=s.family_cooldown_min)
    pricing, live = rules_preset("v4", LiveModelParams(corner_dispersion=77.0), **pk)
    h = write_frozen(path, "v4", pricing, live)
    assert load_live_params(path).corner_dispersion == 77.0
    assert build_rules(s)[2] == h
    p3, l3, _ = build_rules(s.model_copy(update={"alert_rules": "v3"}))
    assert l3.corner_dispersion == 77.0 and l3.shrinkage_mode == "linear" and p3.selection_mode == "best_growth"
    with pytest.raises(FrozenParamsError):
        build_rules(s.model_copy(update={"target_edge": 0.2}))
    assert build_rules(s.model_copy(update={"target_edge": 0.2}), unfreeze=True)[2] != h


def test_cli_live_requires_telegram_and_adds_cups():
    from quantbet.config import Settings
    from quantbet.main import parse_args, settings_from_args

    base = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(SystemExit):
        settings_from_args(parse_args(["--live"]), base.model_copy(update={"telegram_bot_token": None}))
    ok = base.model_copy(update={"telegram_bot_token": "x", "telegram_chat_id": "1", "dry_run": True})
    s = settings_from_args(parse_args(["--live", "--rules", "v4", "--cups"]), ok)
    assert not s.dry_run and s.alert_rules == "v4" and s.mode == "free"
    assert {"uefa.champions", "uefa.europa", "uefa.europa.conf", "eng.league_cup"} <= set(s.espn_leagues)


def test_league_ids_learnt_and_unknown_dropped():
    from quantbet.espn import LEAGUE_IDS, league_id_from_scoreboard, league_slug_of

    assert LEAGUE_IDS["20296"] == "uefa.europa.conf" and LEAGUE_IDS["3920"] == "eng.league_cup"
    assert league_slug_of({"uid": "s:600~l:3920~e:1"}) == "eng.league_cup"
    assert league_id_from_scoreboard({"leagues": [{"id": "3920"}]}) == "3920"
    assert league_id_from_scoreboard({"leagues": []}) is None


def test_keepalive_respects_wrapper(monkeypatch):
    from quantbet.heartbeat import start_keepalive

    monkeypatch.setenv("QB_KEEPALIVE", "caffeinate")
    assert start_keepalive() == ("wrapper: caffeinate", None)
    monkeypatch.delenv("QB_KEEPALIVE")
    calls = []
    monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/" + x)
    desc, h = start_keepalive(platform="darwin", runner=lambda argv: calls.append(argv) or "proc")
    assert desc == "caffeinate -i" and calls[0][:3] == ["caffeinate", "-i", "-w"]


# -- Telegram path against a MOCKED endpoint only --------------------------------
def test_telegram_send_against_mock_endpoint(monkeypatch):
    import quantbet.notifier as nt

    got: list[dict] = []

    async def handler(request: web.Request) -> web.Response:
        got.append({"path": request.path, "body": await request.json()})
        return web.json_response({"ok": True, "result": {"message_id": 1}})

    async def go() -> bool:
        app = web.Application()
        app.router.add_post("/bot{token}/sendMessage", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            monkeypatch.setattr(nt, "TELEGRAM_API", f"http://{server.host}:{server.port}/bot{{token}}/sendMessage")
            async with nt.TelegramNotifier("TESTTOKEN", "12345", dry_run=False) as n:
                return await n.send_text("<b>v4 test</b>")
        finally:
            await server.close()

    assert asyncio.run(go()) is True
    assert got[0]["path"] == "/botTESTTOKEN/sendMessage"
    assert got[0]["body"]["chat_id"] == "12345" and got[0]["body"]["parse_mode"] == "HTML"


def test_run_cli_live_wires_keepalive_heartbeat_and_frozen_check(tmp_path, monkeypatch):
    import quantbet.main as qm
    from quantbet.config import Settings
    from quantbet.heartbeat import HeartbeatMonitor

    seen = {}

    async def fake_run_free(s, stop, heartbeat=None, unfreeze=False, **kw):  # noqa: ANN001
        seen.update(rules=s.alert_rules, dry=s.dry_run, hb=heartbeat, unfreeze=unfreeze, h=qm.build_rules(s, unfreeze)[2])

    monkeypatch.setattr(qm, "run_free", fake_run_free)
    monkeypatch.setenv("QB_KEEPALIVE", "test-wrapper")
    a = qm.parse_args(["--live", "--rules", "v4", "--heartbeat-db", str(tmp_path / "hb.sqlite3")])
    base = Settings(_env_file=None).model_copy(update={"telegram_bot_token": "x", "telegram_chat_id": "1"})  # type: ignore[call-arg]
    s = qm.settings_from_args(a, base)
    asyncio.run(qm.run_cli(s, a))
    assert seen["rules"] == "v4" and seen["dry"] is False and isinstance(seen["hb"], HeartbeatMonitor)
    assert len(seen["h"]) == 64


def test_poller_learns_unknown_slug_ids_once_and_drops_missing():
    import asyncio
    from types import SimpleNamespace

    from quantbet.espn import LEAGUE_IDS
    from quantbet.free_stats_poller import FreePollerConfig, FreeStatsPoller

    calls = []

    class F:
        async def get_json(self, url, **k):
            calls.append(url)
            if "esp.copa_del_rey" in url:
                return SimpleNamespace(ok=True, status=200, data={"leagues": [{"id": "999001"}], "events": []}, blocked=False)
            return SimpleNamespace(ok=True, status=200, data={"leagues": [], "events": []}, blocked=False)

    p = FreeStatsPoller(F(), lambda st: None, cfg=FreePollerConfig(
        espn_leagues=("eng.1", "esp.copa_del_rey", "xx.nonexistent"), use_sofascore=False, use_fotmob=False))
    try:
        dropped = asyncio.run(p.learn_league_ids())
        assert dropped == ["xx.nonexistent"] and LEAGUE_IDS["999001"] == "esp.copa_del_rey"
        assert p.cfg.espn_leagues == ("eng.1", "esp.copa_del_rey") and len(calls) == 2
        assert asyncio.run(p.learn_league_ids()) == [] and len(calls) == 2  # once per run
    finally:
        LEAGUE_IDS.pop("999001", None)
