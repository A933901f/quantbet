"""Tier 1 ESPN / DeltaTracker / Tier 2 API-Football budget / Tier 3 FotMob page / end-to-end."""

from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pytest

from quantbet import notifier as notifier_mod
from quantbet.api_football_free import (
    ApiFootballBudget,
    ApiFootballConfig,
    ApiFootballFree,
    interest_flags,
    parse_fixture_statistics,
    parse_live_fixtures,
)
from quantbet.delta_tracker import DeltaConfig, DeltaTracker
from quantbet.engine import FreeSignalEngine, PreMatchRatings, PricingConfig, RatingsResolver
from quantbet.espn import (
    EspnEndpoints,
    american_to_decimal,
    build_state_from_espn,
    espn_clock,
    parse_espn_pickcenter,
    parse_espn_scoreboard,
    parse_espn_summary,
)
from quantbet.fotmob_page import FotmobPageEnricher, extract_next_data, parse_fotmob_page, parse_suggest
from quantbet.free_stats_poller import FreePollerConfig, FreeStatsPoller
from quantbet.models import LiveMatchState, Urgency
from quantbet.notifier import TelegramNotifier, format_signal
from quantbet.polite_http import PoliteFetcher, ResponseCache
from quantbet.risk import RiskConfig, RiskManager

FX = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FX / name).read_text())


def run(coro):
    return asyncio.run(coro)


class Transport:
    def __init__(self, routes, default=(404, "text/html", "nf", {})):
        self.routes, self.default, self.calls = routes, default, []

    async def __call__(self, url, headers):
        self.calls.append((url, headers))
        for k, v in self.routes.items():
            if k in url:
                return v
        return self.default


def js(obj, status=200):
    return (status, "application/json", json.dumps(obj), {})


def fetcher(t) -> PoliteFetcher:
    return PoliteFetcher("quantbet-test/1.0 (test@example.com)", ResponseCache(), min_interval_s=0.0,
                         global_min_gap_s=0.0, jitter_s=0.0, transport=t)


# ---------------------------------------------------------------------------
# ESPN parsing (real responses captured 2026-10-25, trimmed)
# ---------------------------------------------------------------------------
def test_espn_scoreboard_live_event_fields():
    evs = parse_espn_scoreboard(load("espn_scoreboard_ita1_live.json"))
    live = [e for e in evs if e.live]
    assert len(live) == 1 and len(evs) == 5
    ev = live[0]
    assert (ev.id, ev.league, ev.league_name) == ("401875036", "ita.1", "Italian Serie A")
    assert (ev.home, ev.away, ev.home_score, ev.away_score) == ("Internazionale", "Fiorentina", 0, 0)
    assert ev.status_name == "STATUS_FIRST_HALF" and ev.state == "in"
    assert ev.start == datetime(2026, 10, 25, 11, 30, tzinfo=timezone.utc)
    # the scoreboard already carries per-team stats
    assert ev.home_stats["wonCorners"] == 3 and ev.home_stats["shotsOnTarget"] == 3 and ev.home_stats["totalShots"] == 7
    assert ev.away_stats["possessionPct"] == pytest.approx(48.4)
    assert ev.clock == (43.0, 1, None, "1st half")
    assert all(e.state == "pre" for e in evs if not e.live)


def test_espn_scoreboard_red_cards_from_details_and_ft_clock():
    evs = {e.id: e for e in parse_espn_scoreboard(load("espn_scoreboard_ucl_ft.json"))}
    inter = evs["401915390"]  # Inter v Shakhtar: Inter red card 68'
    assert (inter.home, inter.home_reds, inter.away_reds) == ("Internazionale", 1, 0)
    bay = evs["401915312"]  # Bayern 2-2 Arsenal
    assert (bay.home_score, bay.away_score) == (2, 2)
    assert bay.clock == (93.0, 2, 3.0, "ended")  # displayClock "90'+3'"


@pytest.mark.parametrize(
    "status,expected",
    [
        ({"clock": 2580.0, "displayClock": "43'", "period": 1, "type": {"name": "STATUS_FIRST_HALF", "state": "in"}}, (43.0, 1, None, "1st half")),
        ({"clock": 2700.0, "displayClock": "45'+2'", "period": 1, "type": {"name": "STATUS_FIRST_HALF", "state": "in"}}, (47.0, 1, 2.0, "1st half")),
        ({"clock": 2700.0, "displayClock": "HT", "period": 1, "type": {"name": "STATUS_HALFTIME", "state": "in", "description": "Halftime"}}, (45.0, 2, None, "halftime")),
        ({"clock": 3690.0, "displayClock": "62'", "period": 2, "type": {"name": "STATUS_SECOND_HALF", "state": "in"}}, (61.5, 2, None, "2nd half")),
        ({"clock": 5400.0, "displayClock": "90'+5'", "period": 2, "type": {"name": "STATUS_SECOND_HALF", "state": "in"}}, (95.0, 2, 5.0, "2nd half")),
        ({"displayClock": "0'", "period": 0, "type": {"name": "STATUS_SCHEDULED", "state": "pre"}}, (0.0, 1, None, "not started")),
    ],
)
def test_espn_clock(status, expected):
    assert espn_clock(status) == expected


def test_espn_summary_boxscore_names_and_pickcenter():
    s = parse_espn_summary(load("espn_summary_live.json"))
    assert (s.event_id, s.home, s.away) == ("401875036", "Internazionale", "Fiorentina")
    for k in ("wonCorners", "totalShots", "shotsOnTarget", "possessionPct", "redCards", "yellowCards", "foulsCommitted"):
        assert k in s.home_stats and k in s.away_stats
    assert (s.home_stats["wonCorners"], s.home_stats["shotsOnTarget"], s.away_stats["totalShots"]) == (3, 3, 3)
    assert s.status["type"]["name"] == "STATUS_FIRST_HALF"
    assert s.prior_provider == "DraftKings"
    assert (s.prior.home, s.prior.draw, s.prior.away) == (pytest.approx(1.3636, abs=1e-4), 5.0, 6.5)
    assert (s.prior.total_line, s.prior.over, s.prior.under) == (3.5, 2.0, 1.8)


def test_espn_summary_red_card_key_event():
    s = parse_espn_summary(load("espn_summary_redcard.json"))
    assert (s.home, s.home_reds_events, s.away_reds_events) == ("Internazionale", 1, 0)
    reds = [k for k in s.key_events if k["type"] == "red-card"]
    assert reds and reds[0]["minute"] == 68 and reds[0]["team"] == "Internazionale"
    assert s.home_stats["redCards"] == 1
    assert {"goal", "yellow-card", "kickoff"} & {k["type"] for k in s.key_events}


def test_american_odds():
    assert american_to_decimal(-275) == pytest.approx(1.3636, abs=1e-4)
    assert american_to_decimal("+550") == 6.5 and american_to_decimal("EVEN") == 2.0
    assert american_to_decimal(None) is None and american_to_decimal(50) is None
    assert parse_espn_pickcenter([]) == (None, "")


def test_build_state_from_espn_merges_summary():
    ev = [e for e in parse_espn_scoreboard(load("espn_scoreboard_ita1_live.json")) if e.live][0]
    st = build_state_from_espn(ev, parse_espn_summary(load("espn_summary_live.json")), "k", now=100.0)
    assert isinstance(st, LiveMatchState) and st.source == "espn" and st.source_event_id == "401875036"
    assert (st.minute, st.period, st.status, st.score) == (43.0, 1, "1st half", "0-0")
    assert (st.home_corners, st.away_corners, st.home_shots_on_target, st.away_shots_on_target) == (3, 0, 3, 0)
    assert (st.home_shots, st.away_shots, st.possession_home) == (7, 3, pytest.approx(51.6))
    assert st.stats_available and st.attack_momentum is None and st.home_xg is None
    assert st.league == "Italian Serie A" and st.updated_at == 100.0


def test_build_state_red_card_max_of_sources():
    sb = {e.id: e for e in parse_espn_scoreboard(load("espn_scoreboard_ucl_ft.json"))}["401915390"]
    st = build_state_from_espn(sb, parse_espn_summary(load("espn_summary_redcard.json")), "k", 0.0)
    assert (st.home_red_cards, st.away_red_cards, st.status) == (1, 0, "ended")


def test_espn_endpoints():
    ep = EspnEndpoints()
    assert ep.scoreboard("eng.1") == "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard"
    assert ep.summary("ita.1", 401875036).endswith("/ita.1/summary?event=401875036")


# ---------------------------------------------------------------------------
# DeltaTracker
# ---------------------------------------------------------------------------
H0 = {"sot": 0, "shots": 0, "corners": 0}


def test_delta_velocity_uses_last_10_minutes():
    t = DeltaTracker(DeltaConfig(prior_min=0.0))
    for m, hs, as_ in [(50, (2, 6, 3), (2, 5, 3)), (55, (3, 8, 4), (2, 5, 3)), (60, (5, 11, 6), (2, 6, 3))]:
        r = t.update("e", m, dict(zip(("sot", "shots", "corners"), hs)), dict(zip(("sot", "shots", "corners"), as_)))
    assert r.basis == "window" and r.span_min == 10
    assert r.velocity["home_sot"] == pytest.approx(0.3) and r.velocity["away_sot"] == 0.0
    assert r.velocity["home_shots"] == pytest.approx(0.5) and r.velocity["away_shots"] == pytest.approx(0.1)
    assert r.velocity["home_corners"] == pytest.approx(0.3)
    assert r.velocity["home_sot_last5"] == pytest.approx(0.4)  # second 5-min interval
    assert r.window_counts["home_sot"] == pytest.approx(3.0)
    # activity_10m = 1.5*dCorners + 1.2*dSOT + 0.5*dShotsOff (shots_off = shots - sot)
    act_h = 1.5 * 3 + 1.2 * 3 + 0.5 * (5 - 3)
    act_a = 1.5 * 0 + 1.2 * 0 + 0.5 * (1 - 0)
    assert r.activity_10m["home"] == pytest.approx(act_h) and r.activity_10m["away"] == pytest.approx(act_a)
    assert r.activity_10m["total"] == pytest.approx(act_h + act_a)
    clip = lambda x: min(1.25, max(0.85, x))  # noqa: E731
    assert r.mult["home"] == pytest.approx(clip(1 + 0.08 * (act_h / 1.75 - 1)), abs=1e-4) == 1.25
    assert r.mult["away"] == pytest.approx(clip(1 + 0.08 * (act_a / 1.75 - 1)), abs=1e-4)
    assert r.mult["total"] == pytest.approx(clip(1 + 0.08 * ((act_h + act_a) / 3.5 - 1)), abs=1e-4)
    assert r.momentum == pytest.approx((act_h - act_a) / (act_h + act_a), abs=1e-3)


def test_activity_normalised_to_10_minutes_and_baseline():
    t = DeltaTracker()
    t.update("e", 60, {"sot": 0, "shots": 0, "corners": 0}, {"sot": 0, "shots": 0, "corners": 0})
    # 5-minute span with a "normal" pace: per 10 min home 0.5 corners, 0.5 sot, 1 off-target shot
    r = t.update("e", 65, {"sot": 0.25, "shots": 0.75, "corners": 0.25}, {"sot": 0.25, "shots": 0.75, "corners": 0.25})
    assert r.span_min == 5 and r.window_counts["home_corners"] == pytest.approx(0.5)
    a = 1.5 * 0.5 + 1.2 * 0.5 + 0.5 * 1.0
    assert r.activity_10m["home"] == pytest.approx(a, abs=1e-3) and r.activity_10m["total"] == pytest.approx(2 * a, abs=2e-3)
    assert r.mult["total"] == pytest.approx(1 + 0.08 * (2 * a / 3.5 - 1), abs=1e-3)
    # shots_off never negative (SOT > shots data glitch)
    r2 = DeltaTracker().update("g", 30, {"sot": 5, "shots": 3, "corners": 0}, {"sot": 0, "shots": 0, "corners": 0})
    assert r2.activity_10m["home"] == pytest.approx(1.2 * 5 / 30 * 10, abs=1e-3)
    # floor of the multiplier
    r3 = DeltaTracker().update("z", 40, H0, H0)
    assert r3.mult["total"] == 0.85 or r3.mult["total"] == pytest.approx(1 - 0.08)


def test_engine_b_uses_only_the_activity_multiplier():
    from quantbet.engine import LiveFootballModel

    m = LiveFootballModel()
    st = LiveMatchState(event_key="e", minute=60, home_goals=0, away_goals=0, home_corners=4, away_corners=2,
                        home_shots_on_target_10=9, away_shots_on_target_10=0, attack_momentum=0.9)
    st.momentum_mult_home, st.momentum_mult_away = 1.1, 0.95
    assert m._momentum(st, 0.5) == (1.1, 0.95)  # SOT ratio and momentum index NOT added on top
    st.momentum_mult_home = st.momentum_mult_away = None
    assert m._momentum(st, 0.5)[0] > 1.1  # legacy graph path only without the multiplier


def test_delta_momentum_sign_shrinkage_and_bounds():
    t = DeltaTracker()
    t.update("e", 30, {"sot": 1, "shots": 4, "corners": 2}, {"sot": 1, "shots": 4, "corners": 2})
    r = t.update("e", 40, {"sot": 1, "shots": 5, "corners": 2}, {"sot": 3, "shots": 9, "corners": 5})
    assert -1.0 <= r.momentum < -0.3  # away pressure
    t2 = DeltaTracker()
    t2.update("e", 30, H0, H0)
    r2 = t2.update("e", 40, {"sot": 1, "shots": 1, "corners": 0}, H0)  # one shot: shrunk, mild
    assert 0 < r2.momentum < 0.5


def test_delta_halftime_reset_and_missing():
    t = DeltaTracker()
    t.update("e", 40, {"sot": 2, "shots": 5, "corners": 3}, {"sot": 1, "shots": 2, "corners": 1})
    t.update("e", 45, {"sot": 3, "shots": 6, "corners": 3}, {"sot": 1, "shots": 2, "corners": 1}, status="halftime")
    assert len(t.history("e")) == 1  # HT snapshot ignored (minute frozen)
    # counters went down (data correction / wrong match) -> history reset
    t.update("e", 48, {"sot": 0, "shots": 1, "corners": 0}, H0)
    assert t.resets["e"] == 1 and len(t.history("e")) == 1
    # source switch resets too
    t.update("e", 50, {"sot": 0, "shots": 1, "corners": 0}, H0, source="fotmob")
    t.update("e", 51, {"sot": 0, "shots": 1, "corners": 0}, H0, source="espn")
    assert t.resets["e"] == 2
    # missing stats are dropped; all missing -> None
    t3 = DeltaTracker()
    r = t3.update("x", 20, {"sot": None, "shots": None, "corners": 4}, {"sot": None, "shots": None, "corners": 0})
    assert r.basis == "match_avg" and set(r.components) == {"corners"}
    assert r.momentum is None and r.mult == {}  # activity needs corners, SOT and shots
    r = DeltaTracker().update("y", 20, {"sot": None, "shots": None, "corners": None}, {"sot": None, "shots": None, "corners": None})
    assert r.momentum is None
    assert DeltaTracker().update("z", 0.0, H0, H0).momentum is None


def test_delta_trims_old_history():
    t = DeltaTracker()
    for m in range(10, 61):
        t.update("e", float(m), {"sot": m // 10, "shots": m // 4, "corners": m // 9}, H0)
    h = t.history("e")
    assert h[0].minute <= 50 and h[1].minute > 50 and h[-1].minute == 60


# ---------------------------------------------------------------------------
# Tier 2 API-Football budget
# ---------------------------------------------------------------------------
DAY1 = datetime(2026, 10, 25, 12, tzinfo=timezone.utc).timestamp()
DAY2 = datetime(2026, 10, 26, 0, 5, tzinfo=timezone.utc).timestamp()


def test_api_football_budget_limits_and_rollover(tmp_path):
    b = ApiFootballBudget(tmp_path / "b.sqlite3", daily_limit=100, use_limit=90)
    for _ in range(89):
        b.record("/fixtures", 200, None, DAY1)
    assert b.can_spend(1, DAY1) and b.remaining(DAY1) == 1
    b.record("/fixtures", 200, None, DAY1)
    assert not b.can_spend(1, DAY1)
    assert b.can_spend(1, DAY2) and b.used_today(DAY2) == 0  # new UTC day
    # persisted across instances
    assert ApiFootballBudget(tmp_path / "b.sqlite3").used_today(DAY1) == 90
    with pytest.raises(ValueError):
        ApiFootballBudget(daily_limit=100, use_limit=101)


def test_api_football_budget_syncs_with_header():
    b = ApiFootballBudget()
    b.record("/fixtures", 200, 15, DAY1)  # header says 15 left of 100 -> 85 used
    assert b.used_today(DAY1) == 85 and b.remaining(DAY1) == 5


def _flagged_state(**kw) -> LiveMatchState:
    base = dict(event_key="k", minute=70.0, home="Manchester United", away="AFC Bournemouth", league="English Premier League",
                home_goals=0, away_goals=1, home_corners=9, away_corners=4, stats_available=True, status="2nd half", updated_at=DAY1)
    base.update(kw)
    return LiveMatchState(**base)


LIVE = {"errors": [], "response": [{"fixture": {"id": 1001, "status": {"short": "2H", "elapsed": 70, "extra": None}},
                                    "league": {"name": "Premier League"},
                                    "teams": {"home": {"name": "Manchester United"}, "away": {"name": "Bournemouth"}},
                                    "goals": {"home": 0, "away": 1}}]}
STATS = {"errors": [], "response": [
    {"team": {"name": "Manchester United"}, "statistics": [{"type": "Corner Kicks", "value": 5}, {"type": "Shots insidebox", "value": 9},
                                                         {"type": "Red Cards", "value": None}, {"type": "expected_goals", "value": "1.42"},
                                                         {"type": "Ball Possession", "value": "64%"}]},
    {"team": {"name": "Bournemouth"}, "statistics": [{"type": "Corner Kicks", "value": 2}, {"type": "Shots insidebox", "value": 3},
                                                   {"type": "Red Cards", "value": 1}, {"type": "expected_goals", "value": "0.61"},
                                                   {"type": "Ball Possession", "value": "36%"}]}]}


def test_api_football_interest_flags():
    r = PreMatchRatings(0.8, 2.8, 1.5, 10.0)
    assert set(interest_flags(_flagged_state(), r)) == {"fav_level_or_trailing", "high_corner_pace"}
    assert interest_flags(_flagged_state(minute=40.0), r) == []
    assert interest_flags(_flagged_state(minute=88.0), r) == []
    calm = _flagged_state(home_goals=2, away_goals=0, home_corners=3, away_corners=2)
    assert interest_flags(calm, r) == []
    assert interest_flags(_flagged_state(home_goals=2, away_goals=0, home_corners=3, away_corners=2, away_red_cards=1), r) == ["red_card"]


def test_api_football_enrich_within_budget_and_off_without_key():
    calls = []

    async def http_get(url, headers):
        calls.append((url, headers))
        assert headers["x-apisports-key"] == "K"
        return 200, {"x-ratelimit-requests-remaining": str(100 - len(calls))}, (LIVE if "live=all" in url else STATS)

    r = PreMatchRatings(0.8, 2.8, 1.5, 10.0)
    api = ApiFootballFree("K", cfg=ApiFootballConfig(use_limit=3), http_get=http_get)
    st = run(api.enrich(_flagged_state(), r, now=DAY1))
    assert [c[0].split("?")[1] for c in calls] == ["live=all", "fixture=1001"]
    assert (st.home_xg, st.away_xg) == (1.42, 0.61) and (st.home_shots_inside_box, st.away_shots_inside_box) == (9, 3)
    assert st.away_red_cards == 1 and "api_football" in st.enrichments
    run(api.enrich(_flagged_state(), r, now=DAY1 + 60))  # cached: no new calls
    assert len(calls) == 2
    run(api.enrich(_flagged_state(minute=80.0), r, now=DAY1 + 400))  # one more stats + live -> budget 3 hit
    assert len(calls) == 3 and api.budget.used_today(DAY1) >= 3
    run(api.enrich(_flagged_state(minute=84.0), r, now=DAY1 + 800))
    assert len(calls) == 3  # budget exhausted -> no request
    # not flagged -> no request at all
    api2 = ApiFootballFree("K", http_get=http_get)
    run(api2.enrich(_flagged_state(minute=30.0), r, now=DAY1))
    assert len(calls) == 3
    # no key -> disabled
    off = ApiFootballFree(None, http_get=http_get)
    assert not off.enabled and run(off.enrich(_flagged_state(), r, now=DAY1)).enrichments == ()
    assert len(calls) == 3


def test_api_football_plan_error_disables_for_day(monkeypatch):
    # ``enabled`` compares against the *wall-clock* UTC day (utc_day() with no ts), while the
    # refusal is stamped with the injected ``now``. Freeze the module's day clock so the test
    # does not depend on the calendar date it runs on (it used to fail after 2026-10-25).
    from quantbet import api_football_free as apif

    real_utc_day = apif.utc_day
    clock = {"t": DAY1}
    monkeypatch.setattr(apif, "utc_day", lambda ts=None: real_utc_day(clock["t"] if ts is None else ts))

    async def http_get(url, headers):
        return 200, {}, {"errors": {"plan": "Free plans do not have access to this season"}, "response": []}

    api = ApiFootballFree("K", http_get=http_get)
    assert run(api.live_fixtures(now=DAY1)) == [] and not api.enabled and "plan" in api.disabled_reason
    assert api.disabled_until_day == "2026-10-25"
    clock["t"] = DAY1 + 86400  # next UTC day: the adapter re-enables itself
    assert api.enabled


def test_api_football_parsers():
    assert parse_live_fixtures(LIVE)[0]["away"] == "Bournemouth"
    swapped = {"response": list(reversed(STATS["response"]))}
    assert parse_fixture_statistics(swapped, "Manchester United", "Bournemouth")["Corner Kicks"] == (5, 2)


# ---------------------------------------------------------------------------
# Tier 3 FotMob page (__NEXT_DATA__ from a real page, trimmed)
# ---------------------------------------------------------------------------
def test_fotmob_next_data_parser():
    html = (FX / "fotmob_match_page.html").read_text()
    nd = extract_next_data(html)
    assert "content" in nd["props"]["pageProps"]
    st = parse_fotmob_page(html, "k", now=1.0)
    assert (st.home, st.away, st.home_xg, st.away_xg) == ("Inter", "Fiorentina", 0.52, 0.09)
    assert (st.home_corners, st.home_shots_on_target, st.home_shots_inside_box, st.away_shots_inside_box) == (3, 3, 5, 0)
    assert st.minute == pytest.approx(43.5) and st.period == 1
    assert st.attack_momentum is not None and st.momentum_points > 0
    assert parse_fotmob_page("<html><body>no data</body></html>") is None


def test_fotmob_suggest_and_enricher():
    hit = parse_suggest(load("fotmob_suggest.json"), "Internazionale", "Fiorentina", datetime(2026, 10, 25, 11, 30, tzinfo=timezone.utc))
    assert hit.id == "5749714"
    assert parse_suggest(load("fotmob_suggest.json"), "Arsenal", "Chelsea") is None
    t = Transport({"searchapi/suggest": js(load("fotmob_suggest.json")),
                   "/match/5749714": (200, "text/html", (FX / "fotmob_match_page.html").read_text(), {})})
    ev = [e for e in parse_espn_scoreboard(load("espn_scoreboard_ita1_live.json")) if e.live][0]
    st = build_state_from_espn(ev, None, "k", 0.0)
    enr = FotmobPageEnricher(fetcher(t))
    run(enr.enrich(st, now=1000.0))
    assert (st.home_xg, st.away_xg, st.home_shots_inside_box) == (0.52, 0.09, 5)
    assert st.momentum_source == "graph" and "fotmob_page" in st.enrichments
    n = len(t.calls)
    run(enr.enrich(build_state_from_espn(ev, None, "k", 0.0), now=1100.0))  # within 300 s -> reuse
    assert len(t.calls) == n


def test_fotmob_page_blocked_leaves_state_untouched():
    t = Transport({"searchapi/suggest": js(load("fotmob_suggest.json")),
                   "/match/": (403, "text/html", "<html><title>Access denied</title></html>", {})})
    ev = [e for e in parse_espn_scoreboard(load("espn_scoreboard_ita1_live.json")) if e.live][0]
    st = build_state_from_espn(ev, None, "k", 0.0)
    f = fetcher(t)
    run(FotmobPageEnricher(f).enrich(st, now=1.0))
    assert st.home_xg is None and st.enrichments == () and f.is_blocked("www.fotmob.com")


# ---------------------------------------------------------------------------
# Poller with ESPN as primary
# ---------------------------------------------------------------------------
def _espn_routes():
    return {
        "/ita.1/scoreboard": js(load("espn_scoreboard_ita1_live.json")),
        "/ita.1/summary?event=401875036": js(load("espn_summary_live.json")),
    }


def test_poller_espn_primary_with_synthetic_momentum():
    t = Transport(_espn_routes())
    got: list[LiveMatchState] = []
    p = FreeStatsPoller(fetcher(t), got.append, cfg=FreePollerConfig(espn_leagues=("ita.1",), espn_use_all=False,
                                                                     espn_summary_once=False))
    states = run(p.poll_once(now=1000.0))
    assert len(states) == 1 and got[0] is states[0] and p.last_source == "espn"
    st = states[0]
    assert st.source == "espn" and st.momentum_source == "synthetic" and st.attack_momentum > 0.3
    assert st.momentum_mult_home > 1.0 > st.momentum_mult_away and "home_sot" in st.velocity
    assert p.prior_for(st.event_key)[1] == "espn_draftkings"
    assert not any("sofascore" in u or "fotmob" in u for u, _ in t.calls)
    assert all("quantbet-test" in h["User-Agent"] for _, h in t.calls)


def test_poller_espn_idle_league_is_polled_rarely():
    sb = load("espn_scoreboard_ucl_ft.json")  # all FT -> idle interval
    t = Transport({"/uefa.champions/scoreboard": js(sb)})
    p = FreeStatsPoller(fetcher(t), lambda s: None, cfg=FreePollerConfig(espn_leagues=("uefa.champions",),
                                                                          espn_use_all=False))
    assert run(p.poll_once()) == []
    run(p.poll_once())
    assert len(t.calls) == 1  # next poll scheduled espn_idle_interval_s later


def test_poller_espn_blocked_fails_over_when_enabled():
    t = Transport({"/scoreboard": (429, "application/json", "{}", {"retry-after": "120"})})
    p = FreeStatsPoller(fetcher(t), lambda s: None, cfg=FreePollerConfig(espn_leagues=("ita.1",)))
    assert run(p.poll_espn_once()) is None  # "all" endpoint refused
    assert p.f.is_blocked("site.api.espn.com") and len(t.calls) == 1
    assert p.f.is_blocked("site.api.espn.com")


def _all_payload():
    live = load("espn_scoreboard_ita1_live.json")
    other = load("espn_scoreboard_all.json")["events"]
    evs = [dict(e, uid=f"s:600~l:730~e:{e['id']}") for e in live["events"]]
    evs += [e for e in other if "Internazionale" not in e["name"]]  # EPL pre-match + Scottish (not a target)
    return {"leagues": [{}], "events": evs}


def _all_routes():
    return {
        "/all/scoreboard": js(_all_payload()),
        "/ita.1/summary?event=401875036": js(load("espn_summary_live.json")),
        "/eng.1/summary?event=401879254": js(load("espn_summary_ft.json")),
    }


def test_split_all_scoreboard_by_uid_league():
    from quantbet.espn import league_slug_of, split_all_scoreboard

    real = load("espn_scoreboard_all.json")  # real soccer/all payload (trimmed), 25 Oct 2026
    assert {league_slug_of(e) for e in real["events"]} == {"eng.1", "ita.1", None}
    parts = split_all_scoreboard(real, ("eng.1", "ita.1", "esp.1"))
    assert set(parts) == {"eng.1", "ita.1"} and parts["ita.1"]["leagues"][0]["slug"] == "ita.1"


def test_poller_all_endpoint_single_request_and_summary_once(tmp_path):
    t = Transport(_all_routes())
    got: list[LiveMatchState] = []
    cfg = FreePollerConfig(espn_leagues=("eng.1", "ita.1"), summary_db_path=str(tmp_path / "s.sqlite3"))
    p = FreeStatsPoller(fetcher(t), got.append, cfg=cfg)
    states = run(p.poll_once(now=1000.0))
    assert len(states) == 1 and states[0].league == "Italian Serie A" and states[0].source == "espn"
    urls = [u for u, _ in t.calls]
    assert sum("/all/scoreboard" in u for u in urls) == 1
    assert sum("/summary" in u for u in urls) == 1 and "/ita.1/summary" in urls[1]  # live match first, 1 per cycle
    assert p.prior_for(states[0].event_key)[1] == "espn_draftkings"
    p.f.cache.get = lambda *a, **k: None  # type: ignore[method-assign]  # force refetch of the scoreboard
    p.f._last_by_url.clear()
    run(p.poll_once(now=1060.0))
    urls = [u for u, _ in t.calls]
    assert sum("summary?event=401875036" in u for u in urls) == 1  # never re-polled in play
    assert sum("/summary" in u for u in urls) == 2  # one more (next pre-match fixture) on the next cycle
    # a restart reads the SQLite cache instead of refetching
    p2 = FreeStatsPoller(fetcher(Transport(_all_routes())), lambda s: None, cfg=cfg)
    run(p2.poll_once(now=1100.0))
    assert not any("summary?event=401875036" in u for u, _ in p2.f._transport.calls)  # type: ignore[attr-defined]
    assert p2.prior_for(states[0].event_key) is not None
    # Scottish match is not a target league; EPL match recorded as scheduled
    assert all("Celtic" not in v["home"] + v["away"] for v in p.scheduled.values())


def test_poller_records_finals_for_grading():
    sb = load("espn_scoreboard_ucl_ft.json")
    evs = [dict(e, uid=f"s:600~l:775~e:{e['id']}") for e in sb["events"]]
    t = Transport({"/all/scoreboard": js({"leagues": [{}], "events": evs})})
    p = FreeStatsPoller(fetcher(t), lambda s: None, cfg=FreePollerConfig(espn_leagues=("uefa.champions",),
                                                                          use_espn_summary=False))
    run(p.poll_once())
    assert p.finals and all(v["home_corners"] is not None for v in p.finals.values())


def test_fotmob_xg_confirm_rule():
    from quantbet.fotmob_page import xg_confirm

    # 60', xG 1.8 total vs pre-match lambda 2.7 -> pace 2.7 -> confirms an Over
    assert xg_confirm("over", 60, 1.0, 0.8, None, None, 2.7) == "confirmed"
    # low xG and few shots in the box -> contradicts an Over
    assert xg_confirm("over", 60, 0.4, 0.3, 4, 2, 2.7) == "contradicted"
    # high box shots rescue a low-xG Over (any indicator)
    assert xg_confirm("over", 60, 0.4, 0.3, 9, 5, 2.7) == "confirmed"
    assert xg_confirm("under", 60, 0.4, 0.3, 4, 2, 2.7) == "confirmed"
    assert xg_confirm("over", 60, None, None, None, None, 2.7) == "unavailable"


def test_paper_grading_asian_lines(tmp_path):
    from quantbet.paper import pnl, result_label, settle_total

    assert result_label(settle_total(8.5, "over", 9)) == "win"
    assert result_label(settle_total(9.0, "over", 9)) == "push"
    assert result_label(settle_total(8.75, "over", 9)) == "half-win"
    assert result_label(settle_total(9.25, "over", 9)) == "half-loss"
    assert result_label(settle_total(2.25, "under", 1)) == "win"
    assert pnl(settle_total(8.75, "over", 9), 2.0, 20) == pytest.approx(10.0)
    assert pnl(settle_total(9.25, "over", 9), 2.0, 20) == pytest.approx(-10.0)


# ---------------------------------------------------------------------------
# End-to-end: ESPN fixture -> LiveMatchState -> Engine B -> alert -> Telegram (mocked HTTP)
# ---------------------------------------------------------------------------
async def _e2e(monkeypatch):
    from aiohttp import web

    received: list[dict] = []

    async def send_message(request):
        received.append(await request.json())
        return web.json_response({"ok": True, "result": {"message_id": len(received)}})

    app = web.Application()
    app.router.add_post("/bot{token}/sendMessage", send_message)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    monkeypatch.setattr(notifier_mod, "TELEGRAM_API", f"http://127.0.0.1:{port}/bot{{token}}/sendMessage")
    try:
        holder = {}
        resolver = RatingsResolver(None, None, prior_lookup=lambda k: holder["p"].prior_for(k))
        risk = RiskManager(RiskConfig(bankroll=2000.0, currency="USD"))
        free = FreeSignalEngine(resolver, PricingConfig(), sizer=lambda f, o: risk.stake_from_kelly(f))
        signals = []

        async with TelegramNotifier("TESTTOKEN", "42") as tg:
            async def on_state(st):
                for sig in risk.size_all(free.on_match_state(st, now=st.updated_at)):
                    signals.append(sig)
                    assert await tg.send_signal(sig)

            p = FreeStatsPoller(fetcher(Transport(_all_routes())), on_state,
                                cfg=FreePollerConfig(espn_leagues=("eng.1", "ita.1")))
            holder["p"] = p
            await p.poll_once()
        return signals, received
    finally:
        await runner.cleanup()


def test_end_to_end_espn_to_telegram(monkeypatch):
    signals, received = asyncio.run(_e2e(monkeypatch))
    assert signals, "expected at least one model-only alert"
    s0 = signals[0]
    assert s0.urgency is Urgency.MIN_ODDS and s0.extra["ratings_source"] == "espn_draftkings"
    assert s0.extra["source"] == "espn" and "momentum_home" in s0.extra["triggers"]
    assert s0.extra["momentum_mult"][0] > 1.0 and s0.extra["ev_at_reference"] is not None
    assert len(received) == len(signals) and received[0]["chat_id"] == "42"
    text = received[0]["text"]
    assert "MINIMUM BET365 ODDS TO BET" in text and "If Bet365 offers" in text
    assert f"{s0.extra['min_odds']:.2f}" in text
    first_tier = s0.extra["tiers"][0]
    assert f"{first_tier[0]:.2f}" in text and f"${first_tier[1]:,.0f}" in text
    assert text == format_signal(s0)
