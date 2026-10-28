"""Tests for the $0/month stack: polite HTTP, free stats parsers, pricing, quota, Football-Data."""

from __future__ import annotations

import asyncio
import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from quantbet.aggregator_odds import SofaScoreOddsSource, fractional_to_decimal, parse_sofascore_odds
from quantbet.engine import (
    AlertRateLimiter,
    FreeSignalEngine,
    LiveFootballModel,
    PreMatchRatings,
    PricingConfig,
    RatingsResolver,
    SharpPrices,
    assess_confidence,
    build_stake_tiers,
    candidate_lines,
    price_line,
    price_live_lines,
    select_alert_lines,
)
from quantbet.football_data import (
    calibrate_league,
    csv_url,
    fit_negbin_dispersion,
    fit_team_ratings,
    load_csv,
    load_ratings,
    moment_dispersion,
    save_ratings,
    season_code,
    to_closing_rows,
)
from quantbet.free_stats_poller import (
    FreePollerConfig,
    FreeSourceEndpoints,
    FreeStatsPoller,
    build_state_from_sofascore,
    parse_fotmob_match_details,
    parse_fotmob_matches,
    parse_fotmob_minute,
    parse_momentum_points,
    parse_sofascore_graph,
    parse_sofascore_incidents,
    parse_sofascore_live_events,
    parse_sofascore_statistics,
    sofascore_clock,
)
from quantbet.models import LiveMatchState, MarketType, Selection, Signal, Urgency
from quantbet.notifier import format_signal, format_tier_line, money
from quantbet.odds_quota import OddsApiQuotaManager, QuotaConfig, days_left_in_month
from quantbet.polite_http import (
    ForbiddenHostError,
    PoliteFetcher,
    ResponseCache,
    assert_allowed_url,
    looks_like_challenge,
)
from quantbet.risk import RiskManager

FIX = Path(__file__).parent / "fixtures"
T0 = 1761400000.0  # currentPeriodStartTimestamp in the fixtures


def fx(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def run(coro):
    return asyncio.run(coro)


class FakeTransport:
    """Routes URLs to (status, content-type, body, headers); records calls."""

    def __init__(self, routes: dict[str, tuple[int, str, str, dict[str, str]]], default=(404, "text/html", "nf", {})):
        self.routes = routes
        self.default = default
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.active = 0
        self.max_active = 0

    async def __call__(self, url: str, headers: dict[str, str]):
        self.calls.append((url, headers))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        for k, v in self.routes.items():
            if k in url:
                return v
        return self.default


def js(obj, status=200, headers=None):
    return (status, "application/json", json.dumps(obj), headers or {})


def fetcher(transport, **kw) -> PoliteFetcher:
    base = dict(min_interval_s=0.0, global_min_gap_s=0.0, jitter_s=0.0, backoff_base_s=60.0)
    base.update(kw)
    return PoliteFetcher("quantbet-test/1.0 (test@example.com)", ResponseCache(), transport=transport, **base)


# ---------------------------------------------------------------------------
# polite HTTP
# ---------------------------------------------------------------------------
def test_bet365_hosts_are_refused():
    for url in ("https://www.bet365.com/x", "https://mobile.bet365.es/", "https://BET365.it"):
        with pytest.raises(ForbiddenHostError):
            assert_allowed_url(url)
    assert assert_allowed_url("https://api.sofascore.com/api/v1/x") == "api.sofascore.com"


def test_fetcher_refuses_bet365_without_network():
    t = FakeTransport({})
    with pytest.raises(ForbiddenHostError):
        run(fetcher(t).get_json("https://www.bet365.com/api"))
    assert t.calls == []


def test_honest_user_agent_and_cache():
    t = FakeTransport({"/a": js({"x": 1})})
    f = fetcher(t)

    async def go():
        r1 = await f.get_json("https://example.org/a", cache_ttl_s=60)
        r2 = await f.get_json("https://example.org/a", cache_ttl_s=60)
        return r1, r2

    r1, r2 = run(go())
    assert r1.ok and not r1.from_cache and r2.from_cache and r2.data == {"x": 1}
    assert len(t.calls) == 1
    ua = t.calls[0][1]["User-Agent"]
    assert ua.startswith("quantbet-test") and "Mozilla" not in ua


def test_403_triggers_exponential_backoff():
    t = FakeTransport({"/a": js({"error": {"code": 403}}, 403)})
    f = fetcher(t, backoff_base_s=10)

    async def go():
        r = await f.get_json("https://example.org/a", cache_ttl_s=0)
        b1 = f.host_state("example.org").blocked_until - time.time()
        r2 = await f.get_json("https://example.org/b", cache_ttl_s=0)  # same host, still blocked
        f.host_state("example.org").blocked_until = 0
        await f.get_json("https://example.org/a", cache_ttl_s=0)
        b2 = f.host_state("example.org").blocked_until - time.time()
        return r, r2, b1, b2

    r, r2, b1, b2 = run(go())
    assert r.blocked and not r.ok and r.status == 403
    assert r2.blocked and r2.error == "host in backoff"
    assert len(t.calls) == 2  # blocked request never hit the network
    assert 9 < b1 <= 10.1 and 19 < b2 <= 20.1


def test_429_retry_after_honoured():
    t = FakeTransport({"/a": js({}, 429, {"retry-after": "300"})})
    f = fetcher(t, backoff_base_s=10)
    run(f.get_json("https://example.org/a", cache_ttl_s=0))
    assert f.host_state("example.org").blocked_until - time.time() > 290


def test_challenge_page_detected_as_block():
    html = "<html><title>Just a moment...</title><script src='/cdn-cgi/challenge-platform/x'></script></html>"
    t = FakeTransport({"/a": (200, "text/html", html, {})})
    f = fetcher(t)
    r = run(f.get_json("https://example.org/a", cache_ttl_s=0))
    assert r.blocked and f.is_blocked("example.org")
    assert looks_like_challenge(200, "text/html", html)
    assert not looks_like_challenge(200, "application/json", '{"captcha": 1}')


def test_global_concurrency_limit():
    t = FakeTransport({"example.org": js({"ok": 1})})
    f = fetcher(t, global_concurrency=2)

    async def go():
        await asyncio.gather(*(f.get_json(f"https://example.org/{i}", cache_ttl_s=0) for i in range(8)))

    run(go())
    assert len(t.calls) == 8 and t.max_active <= 2


def test_per_url_min_interval_enforced():
    t = FakeTransport({"example.org": js({"ok": 1})})
    f = fetcher(t, min_interval_s=0.3)

    async def go():
        t0 = time.perf_counter()
        await f.get_json("https://example.org/a", cache_ttl_s=0)
        await f.get_json("https://example.org/a", cache_ttl_s=0)
        return time.perf_counter() - t0

    assert run(go()) >= 0.29


def test_curl_cffi_backend_never_impersonates():
    import inspect

    import quantbet.polite_http as ph

    src = inspect.getsource(ph.PoliteFetcher._default_transport)
    assert "impersonate=" not in src


# ---------------------------------------------------------------------------
# SofaScore parsers
# ---------------------------------------------------------------------------
def test_parse_sofascore_live_events():
    evs = parse_sofascore_live_events(fx("sofascore_live.json"))
    assert [e.id for e in evs] == [12437791, 12437792, 12437793]  # malformed skipped
    a = evs[0]
    assert (a.home, a.away, a.home_score, a.away_score) == ("Arsenal", "Chelsea", 0, 1)
    assert a.league == "England - Premier League" and a.status == "2nd half"


def test_sofascore_clock_including_added_time():
    evs = parse_sofascore_live_events(fx("sofascore_live.json"))
    m, p, add = sofascore_clock(evs[0], T0 + 20 * 60)
    assert (round(m, 6), p, add) == (65.0, 2, None)
    m, p, add = sofascore_clock(evs[1], T0 + 47.5 * 60)  # first-half stoppage
    assert p == 1 and math.isclose(m, 47.5) and math.isclose(add, 2.5)
    m, p, add = sofascore_clock(evs[0], T0 + 48 * 60)  # 93'
    assert math.isclose(add, 3.0)
    assert sofascore_clock(evs[2], T0) == (45.0, 2, None)  # half-time


def test_parse_sofascore_statistics():
    s = parse_sofascore_statistics(fx("sofascore_statistics.json"))
    assert s["corners"] == (7, 2) and s["shots_on_target"] == (5, 2)
    assert s["shots_inside_box"] == (11, 3) and s["xg"] == (1.48, 0.61)
    assert s["possession"] == (62, 38) and s["red_cards"] == (0, 1)
    assert parse_sofascore_statistics(fx("sofascore_statistics.json"), "1ST")["corners"] == (3, 1)
    assert parse_sofascore_statistics({}) == {}


def test_momentum_rolling_window_and_normalisation():
    g = fx("sofascore_graph.json")
    m, n = parse_sofascore_graph(g, 61.0)
    # points in (51, 61]: 55,60,35,50,70,45,80 -> mean 395/7 / 100
    assert n == 7 and math.isclose(m, 395 / 7 / 100)
    assert parse_momentum_points([{"minute": 60, "value": 400}], 61)[0] == 1.0
    assert parse_momentum_points([{"minute": 10, "value": 50}], 61) == (None, 0)


def test_parse_incidents_red_cards_and_injury_time():
    inc = parse_sofascore_incidents(fx("sofascore_incidents.json"))
    assert (inc.home_reds, inc.away_reds) == (0, 1)
    assert inc.injury_time_1 == 2 and inc.injury_time_2 is None
    assert (inc.home_goals, inc.away_goals) == (0, 1)


def test_build_live_match_state_from_sofascore():
    ev = parse_sofascore_live_events(fx("sofascore_live.json"))[0]
    now = T0 + 16 * 60  # 61'
    st = build_state_from_sofascore(
        ev, parse_sofascore_statistics(fx("sofascore_statistics.json")),
        parse_sofascore_incidents(fx("sofascore_incidents.json")),
        parse_sofascore_graph(fx("sofascore_graph.json"), 61.0), "k1", now,
    )
    assert isinstance(st, LiveMatchState)
    assert st.total_corners == 9 and st.total_goals == 1 and st.score == "0-1"
    assert st.away_red_cards == 1 and st.home_shots_on_target == 5 and st.home_shots_inside_box == 11
    assert st.home_xg == 1.48 and st.attack_momentum > 0.5 and st.stats_available
    assert st.source == "sofascore" and st.source_event_id == "12437791" and st.period == 2
    # Engine B accepts it
    pred = LiveFootballModel().predict(st, PreMatchRatings(0.3, 2.7, 1.0, 10.0))
    assert pred is not None


# ---------------------------------------------------------------------------
# FotMob parsers
# ---------------------------------------------------------------------------
def test_parse_fotmob_matches():
    ms = parse_fotmob_matches(fx("fotmob_matches.json"))
    assert len(ms) == 3
    live = [m for m in ms if m.started and not m.finished]
    assert [m.id for m in live] == [4813501]


@pytest.mark.parametrize(
    "lt,expected",
    [
        ({"short": "67’", "long": "66:30"}, (66.5, 2, None)),
        ({"short": "45+2’"}, (47.0, 1, 2.0)),
        ({"short": "90+4'"}, (94.0, 2, 4.0)),
        ({"short": "HT"}, (45.0, 2, None)),
        ({"short": "12'"}, (12.0, 1, None)),
    ],
)
def test_parse_fotmob_minute(lt, expected):
    assert parse_fotmob_minute({"liveTime": lt}) == expected


def test_parse_fotmob_match_details():
    st = parse_fotmob_match_details(fx("fotmob_match_details.json"), "k1", now=T0)
    assert (st.home, st.away, st.home_goals, st.away_goals) == ("Arsenal", "Chelsea", 0, 1)
    assert (st.home_corners, st.away_corners, st.away_red_cards) == (7, 2, 1)
    assert st.home_shots_on_target == 5 and st.home_shots_inside_box == 11 and st.home_xg == 1.48
    assert math.isclose(st.minute, 66.5) and st.period == 2
    assert math.isclose(st.attack_momentum, 0.5) and st.momentum_points == 3
    assert st.source == "fotmob" and st.stats_available


# ---------------------------------------------------------------------------
# Poller with failover (fake transport, no network)
# ---------------------------------------------------------------------------
def _sofa_routes(status_live=200):
    return {
        "events/live": js(fx("sofascore_live.json"), status_live) if status_live == 200 else js({"error": {"code": status_live}}, status_live),
        "/statistics": js(fx("sofascore_statistics.json")),
        "/graph": js(fx("sofascore_graph.json")),
        "/incidents": js(fx("sofascore_incidents.json")),
        "matches?date": js(fx("fotmob_matches.json")),
        "matchDetails": js(fx("fotmob_match_details.json")),
    }


def test_poller_sofascore_happy_path():
    t = FakeTransport(_sofa_routes())
    got: list[LiveMatchState] = []
    p = FreeStatsPoller(fetcher(t), got.append, cfg=FreePollerConfig(league_filter=("premier",), use_espn=False, use_sofascore=True, use_fotmob=True))
    states = run(p.poll_once(now=T0 + 20 * 60))
    assert len(states) == 1 and got[0].home == "Arsenal" and got[0].source == "sofascore"
    assert not any("fotmob" in u for u, _ in t.calls)
    assert all("bet365" not in u for u, _ in t.calls)


def test_poller_fails_over_to_fotmob_on_403():
    t = FakeTransport(_sofa_routes(status_live=403))
    got: list[LiveMatchState] = []
    f = fetcher(t)
    p = FreeStatsPoller(f, got.append, cfg=FreePollerConfig(use_espn=False, use_sofascore=True, use_fotmob=True))
    states = run(p.poll_once(now=T0))
    assert len(states) == 1 and states[0].source == "fotmob" and got[0].home == "Arsenal"
    assert f.is_blocked("api.sofascore.com")
    # next cycle: SofaScore is skipped without a request while in backoff
    n = len([u for u, _ in t.calls if "sofascore" in u])
    run(p.poll_once(now=T0))
    assert len([u for u, _ in t.calls if "sofascore" in u]) == n


def test_endpoints_configurable():
    ep = FreeSourceEndpoints(sofascore_base="https://mirror.example/api/v1/")
    assert ep.sofa("statistics", 5) == "https://mirror.example/api/v1/event/5/statistics"
    assert ep.fotmob("match_details", id=7).endswith("matchDetails?matchId=7")


# ---------------------------------------------------------------------------
# Aggregator odds (Layer 2)
# ---------------------------------------------------------------------------
def test_fractional_to_decimal():
    assert fractional_to_decimal("4/5") == 1.8
    assert fractional_to_decimal("evs") == 2.0
    assert fractional_to_decimal("13/10") == 2.3
    assert fractional_to_decimal("x") is None


def test_parse_sofascore_odds():
    snaps = parse_sofascore_odds(fx("sofascore_odds.json"), "k1", "Arsenal", "Chelsea", "EPL", None, "provider_x")
    by = {(s.market, s.selection, s.line): s.price for s in snaps}
    assert by[(MarketType.H2H, Selection.HOME, None)] == 2.3
    assert by[(MarketType.TOTAL_GOALS, Selection.OVER, 2.5)] == 1.8
    assert by[(MarketType.ASIAN_HANDICAP, Selection.HOME, -0.25)] == 1.9
    assert not any(s.market is MarketType.TOTAL_CORNERS for s in snaps)  # suspended
    assert all(s.bookmaker == "provider_x" and s.is_live for s in snaps)


def test_sofascore_odds_source_disabled_by_default():
    t = FakeTransport({"/odds/": js(fx("sofascore_odds.json"))})
    src = SofaScoreOddsSource(fetcher(t))
    assert run(src.fetch_event_odds("1", "k", "A", "B", "L", None, True)) == [] and t.calls == []
    src.enabled = True
    assert len(run(src.fetch_event_odds("1", "k", "A", "B", "L", None, True))) > 0


# ---------------------------------------------------------------------------
# Pricing: min odds, abort floor, quarter lines, tiers
# ---------------------------------------------------------------------------
def _state(**kw) -> LiveMatchState:
    base = dict(event_key="k1", minute=65.0, home_goals=0, away_goals=1, home_corners=7, away_corners=2,
                period=2, updated_at=time.time(), home="Arsenal", away="Chelsea", league="England - Premier League",
                stats_available=True, status="2nd half", attack_momentum=0.45)
    base.update(kw)
    return LiveMatchState(**base)


RATINGS = PreMatchRatings(0.6, 2.8, 1.5, 10.5)


def test_min_odds_and_abort_floor_edges_exact():
    pred = LiveFootballModel().predict(_state(), RATINGS)
    for line in (1.5, 1.75, 2.0, 2.25):
        lp = price_line(pred, MarketType.TOTAL_GOALS, Selection.OVER, line)
        assert lp is not None
        sp = lp.settlement
        assert math.isclose(sp.expected_value(lp.fair_odds), 0.0, abs_tol=1e-9)
        assert lp.ev_at(lp.min_odds) >= 0.025 - 1e-12
        assert lp.ev_at(lp.min_odds - 0.01) < 0.025  # 2-dp ceiling is the minimum
        assert lp.ev_at(lp.abort_floor) >= 0.015 - 1e-12 and lp.ev_at(lp.abort_floor - 0.01) < 0.015
        assert lp.abort_floor <= lp.min_odds
        exact = 1 + (sp.loss_weight + 0.025) / sp.win_weight
        assert lp.min_odds == math.ceil(exact * 100 - 1e-9) / 100


def test_quarter_line_settlement_is_split():
    pred = LiveFootballModel().predict(_state(), RATINGS)
    q = price_line(pred, MarketType.TOTAL_GOALS, Selection.OVER, 1.75).settlement
    assert q.half_win > 0 and q.push == 0  # 1.75 with 1 goal: exactly 2 -> half win
    w = price_line(pred, MarketType.TOTAL_GOALS, Selection.OVER, 2.0).settlement
    assert w.push > 0 and w.half_win == 0
    assert math.isclose(q.total, 1.0, abs_tol=1e-6) and math.isclose(w.total, 1.0, abs_tol=1e-6)


def test_candidate_lines_include_quarters():
    assert candidate_lines(9, 1.0, 0.25) == [9.25, 9.5, 9.75, 10.0]


def test_price_live_lines_covers_goals_and_corners():
    prices = price_live_lines(_state(), RATINGS)
    mk = {lp.market for lp in prices}
    assert mk == {MarketType.TOTAL_GOALS, MarketType.TOTAL_CORNERS}
    lines = {lp.line for lp in prices if lp.market is MarketType.TOTAL_CORNERS}
    assert any(abs(x % 1 - 0.25) < 1e-9 for x in lines) and any(abs(x % 1 - 0.75) < 1e-9 for x in lines)
    assert all(1.30 <= lp.fair_odds <= 6.0 for lp in prices)


def test_stake_tiers_deduped_no_zeros_and_increasing():
    pred = LiveFootballModel().predict(_state(), RATINGS)
    lp = price_line(pred, MarketType.TOTAL_CORNERS, Selection.OVER, 11.0)
    rm = RiskManager()
    tiers = build_stake_tiers(lp.settlement, lp.min_odds, PricingConfig(max_tiers=10), lambda f, o: rm.stake_from_kelly(f))
    stakes = [t.stake for t in tiers]
    assert stakes and all(s > 0 for s in stakes)
    assert len(set(stakes)) == len(stakes) and stakes == sorted(stakes)
    assert all(s % 5 == 0 for s in stakes)
    assert tiers[0].odds == lp.min_odds
    # a sizer that rounds everything to zero gives no tiers
    assert build_stake_tiers(lp.settlement, lp.min_odds, PricingConfig(), lambda f, o: 0.0) == []
    # constant sizer collapses to one tier
    assert len(build_stake_tiers(lp.settlement, lp.min_odds, PricingConfig(), lambda f, o: 20.0)) == 1


# ---------------------------------------------------------------------------
# Confidence gate & rate limiter
# ---------------------------------------------------------------------------
def test_confidence_gates():
    now = time.time()
    ok = assess_confidence(_state(updated_at=now), "pinnacle", ratings=RATINGS, now=now)
    assert ok.ok and "momentum_home" in ok.triggers and ok.direction[MarketType.TOTAL_GOALS] is Selection.OVER
    assert not assess_confidence(_state(minute=5, updated_at=now), "pinnacle", now=now).ok
    assert not assess_confidence(_state(minute=88, updated_at=now), "pinnacle", now=now).ok
    assert not assess_confidence(_state(updated_at=now - 600), "pinnacle", now=now).ok
    assert not assess_confidence(_state(stats_available=False, updated_at=now), "pinnacle", now=now).ok
    r = assess_confidence(_state(updated_at=now), "league_default", now=now)
    assert not r.ok and "only league-default ratings" in r.reasons
    r = assess_confidence(_state(attack_momentum=0.05, updated_at=now, away_goals=0), "pinnacle",
                          ratings=PreMatchRatings(0.0, 2.6, 0.0, 10.0), now=now)
    assert not r.ok and "no trigger" in r.reasons
    r = assess_confidence(_state(attack_momentum=None, away_red_cards=1, updated_at=now), "football_data", now=now)
    assert r.ok and r.direction[MarketType.TOTAL_GOALS] is None


def test_rate_limiter():
    rl = AlertRateLimiter(PricingConfig(realert_cooldown_s=600, realert_fair_move=0.08, max_alerts_per_event_window=3))
    a = (MarketType.TOTAL_CORNERS, 11.0, Selection.OVER)
    assert rl.allow("e", a[0], a[1], a[2], 2.0, now=0)
    assert not rl.allow("e", a[0], a[1], a[2], 2.05, now=60)  # small move in cooldown
    assert rl.allow("e", a[0], a[1], a[2], 1.80, now=120)  # fair moved > 8 %
    assert rl.allow("e", MarketType.TOTAL_GOALS, 2.5, Selection.OVER, 2.0, now=130)  # 3rd alert in window
    assert not rl.allow("e", MarketType.TOTAL_GOALS, 3.5, Selection.OVER, 2.0, now=140)  # event budget used
    assert rl.allow("other", a[0], a[1], a[2], 2.0, now=140)
    assert rl.allow("e", a[0], a[1], a[2], 1.80, now=900)  # cooldown and window over


def test_select_alert_lines_respects_direction_and_count():
    prices = price_live_lines(_state(), RATINGS)
    cfg = PricingConfig(lines_per_market=2)
    sel = select_alert_lines(prices, cfg, {MarketType.TOTAL_GOALS: Selection.OVER, MarketType.TOTAL_CORNERS: Selection.OVER})
    assert all(lp.selection is Selection.OVER for lp in sel)
    assert sum(lp.market is MarketType.TOTAL_CORNERS for lp in sel) <= 2


# ---------------------------------------------------------------------------
# Ratings resolver & free engine
# ---------------------------------------------------------------------------
class _Table:
    def ratings(self, home, away, league=None):
        return PreMatchRatings(0.4, 2.9, 2.0, 11.0) if home == "Arsenal" else None


def test_resolver_priority():
    sharp = SharpPrices(1.80, 3.8, 4.6, 2.5, 1.75, 2.15, time.time())
    r, src = RatingsResolver(lambda h, a, k: sharp, _Table()).resolve("e1", "Arsenal", "Chelsea", "Premier League")
    assert src == "pinnacle" and r.goal_supremacy > 0 and r.corner_total == 11.0
    r, src = RatingsResolver(lambda h, a, k: None, _Table()).resolve("e2", "Arsenal", "Chelsea")
    assert src == "football_data" and r == PreMatchRatings(0.4, 2.9, 2.0, 11.0)
    r, src = RatingsResolver(None, _Table()).resolve("e3", "Unknown", "Other", "Premier League")
    assert src == "league_default"


def test_free_engine_emits_min_odds_alerts_and_notifier_format():
    rm = RiskManager()
    fe = FreeSignalEngine(RatingsResolver(None, _Table()), sizer=lambda f, o: rm.stake_from_kelly(f))
    now = time.time()
    sigs = [s for s in fe.on_match_state(_state(updated_at=now), now) if s.urgency is Urgency.MIN_ODDS]
    assert sigs, "expected model-only alerts with momentum trigger"
    s = rm.size_all(sigs)[0]
    assert s.extra["model_only"] and s.extra["tiers"] and s.extra["ratings_source"] == "football_data"
    assert s.bookmaker_odds == s.extra["min_odds"] and s.min_acceptable_odds == s.extra["abort_floor"]
    assert math.isclose(s.ev, s.extra["min_odds"] * s.p_true - 1, abs_tol=1e-9)
    txt = format_signal(s)
    assert "Fair Odds" in txt and "MINIMUM BET365 ODDS TO BET" in txt and "Abort floor" in txt
    assert "If Bet365 offers" in txt and "-&gt; Bet €" in txt
    # rate limiting: the same state again does not re-alert
    assert not [x for x in fe.on_match_state(_state(updated_at=now), now + 5) if x.urgency is Urgency.MIN_ODDS]


def test_free_engine_no_alert_on_default_ratings():
    fe = FreeSignalEngine(RatingsResolver())
    now = time.time()
    assert not [s for s in fe.on_match_state(_state(updated_at=now), now) if s.urgency is Urgency.MIN_ODDS]


def test_tier_line_format():
    assert format_tier_line([(2.05, 15), (2.15, 25)], "USD") == "If Bet365 offers 2.05 -> Bet $15 | If 2.15 -> Bet $25"
    assert money(12.5, "SEK") == "12.50 SEK"


def test_high_limit_risk_scales_tiers():
    s = Signal(urgency=Urgency.MIN_ODDS, engine="B", event_key="e", home="A", away="B", league="Obscure League",
               market=MarketType.TOTAL_CORNERS, line=9.5, selection=Selection.OVER, bookmaker="bet365 (check app)",
               bookmaker_odds=2.0, p_true=0.53, fair_odds=1.9, min_acceptable_odds=1.95, ev=0.06, kelly_fraction=0.01,
               extra={"tiers": [(2.0, 20.0), (2.1, 40.0)]})
    from quantbet.risk import RiskConfig

    out = RiskManager(RiskConfig(high_limit_risk_stake_mult=0.5)).size(s)
    assert out.high_limit_risk and out.extra["tiers"] == [(2.0, 10.0), (2.1, 20.0)]
    assert all(st < orig for (_, st), (_, orig) in zip(out.extra["tiers"], [(2.0, 20.0), (2.1, 40.0)]))


# ---------------------------------------------------------------------------
# The-Odds-API quota manager
# ---------------------------------------------------------------------------
def _odds_payload(ko: datetime):
    return [{
        "id": "ev1", "sport_key": "soccer_epl", "commence_time": ko.isoformat().replace("+00:00", "Z"),
        "home_team": "Arsenal", "away_team": "Chelsea",
        "bookmakers": [{"key": "pinnacle", "last_update": ko.isoformat(), "markets": [
            {"key": "h2h", "outcomes": [{"name": "Arsenal", "price": 1.8}, {"name": "Chelsea", "price": 4.6}, {"name": "Draw", "price": 3.8}]},
            {"key": "totals", "outcomes": [{"name": "Over", "price": 1.75, "point": 2.5}, {"name": "Under", "price": 2.15, "point": 2.5}]}]}],
    }]


def test_quota_cost_and_pacing():
    cfg = QuotaConfig()
    assert cfg.cost_per_call == 2  # 2 markets x 1 region (6 books <= 10)
    assert QuotaConfig(bookmakers=tuple(f"b{i}" for i in range(11))).cost_per_call == 4
    now = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
    assert 6.4 < days_left_in_month(now) < 6.6
    q = OddsApiQuotaManager("k", cfg=cfg)
    assert q.remaining(now) == 500 and math.isclose(q.daily_allowance(now), 480 / days_left_in_month(now))


def test_quota_manager_priority_budget_and_headers():
    now = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
    ko = now + timedelta(hours=2)
    calls: list[str] = []

    async def http_get(url):
        calls.append(url)
        if "/events" in url:
            return 200, {}, [{"id": "ev1", "commence_time": ko.isoformat().replace("+00:00", "Z"), "home_team": "Arsenal", "away_team": "Chelsea"}]
        return 200, {"x-requests-remaining": "478", "x-requests-used": "22", "x-requests-last": "2"}, _odds_payload(ko)

    q = OddsApiQuotaManager("KEY", cfg=QuotaConfig(sports=("soccer_epl",)), http_get=http_get)

    async def go():
        a = await q.fetch_odds("soccer_epl", now=now)
        b = await q.fetch_odds("soccer_epl", now=now + timedelta(minutes=30))  # min_refresh not elapsed
        return a, b

    a, b = run(go())
    assert a is not None and b is None
    assert sum("/odds" in c for c in calls) == 1
    assert "bookmakers=pinnacle" in calls[-1] or any("bookmakers=pinnacle" in c for c in calls)
    assert q.used_this_month(now) == 22 and q.remaining(now) == 478
    sp = q.sharp_prices("Arsenal FC", "Chelsea")
    assert sp and (sp.home, sp.draw, sp.away, sp.total_line) == (1.8, 3.8, 4.6, 2.5)
    snaps = q.snapshots("soccer_epl")
    assert {s.bookmaker for s in snaps} == {"pinnacle"} and len(snaps) == 5


def test_quota_manager_skips_when_no_fixture_soon_or_budget_spent():
    now = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
    calls: list[str] = []

    async def http_get(url):
        calls.append(url)
        ko = now + timedelta(days=2)
        return 200, {}, [{"id": "x", "commence_time": ko.isoformat(), "home_team": "A", "away_team": "B"}]

    q = OddsApiQuotaManager("KEY", http_get=http_get)
    assert run(q.fetch_odds("soccer_epl", now=now)) is None
    assert not any("/odds" in c for c in calls)
    q2 = OddsApiQuotaManager("KEY", cfg=QuotaConfig(monthly_budget=21, reserve=20))
    assert not q2.can_spend(now)
    assert run(OddsApiQuotaManager(None).fetch_odds("soccer_epl", force=True)) is None


# ---------------------------------------------------------------------------
# Football-Data.co.uk
# ---------------------------------------------------------------------------
def test_football_data_urls():
    assert season_code(2025) == "2526" and season_code(1999) == "9900"
    assert csv_url("E0", 2025) == "https://www.football-data.co.uk/mmz4281/2526/E0.csv"


def test_load_csv_and_closing_rows():
    df = load_csv(FIX / "football_data_sample.csv")
    assert len(df) == 8 and df["League"].iloc[0] == "premier league"
    assert df["Date"].iloc[0].year == 2025 and df["HC"].sum() > 0
    rows = to_closing_rows(df)
    assert rows[0].h2h == (1.78, 3.95, 4.9) and rows[0].ou25 == (1.70, 2.26)
    assert rows[0].ah_line == -0.75 and rows[0].ah == (1.95, 1.97)


def test_team_ratings_and_calibration(tmp_path):
    df = load_csv(FIX / "football_data_sample.csv")
    t = fit_team_ratings(df, shrink_k=2.0, half_life_days=None)
    lv, ch = t.ratings("Liverpool", "Chelsea"), t.ratings("Chelsea", "Liverpool")
    assert lv.goal_supremacy > ch.goal_supremacy and lv.corner_supremacy > ch.corner_supremacy
    assert t.ratings("Real Madrid", "Chelsea") is None
    cal = calibrate_league(df, "premier league", t)
    assert cal.n_matches == 8 and cal.corner_dispersion_mle > 0 and cal.home_goals > cal.away_goals
    p = tmp_path / "r.json"
    save_ratings(t, p)
    assert load_ratings(p).ratings("Liverpool", "Chelsea") == lv


def test_negbin_dispersion_fit_recovers_parameter():
    import numpy as np

    rng = np.random.default_rng(7)
    r, mu = 8.0, 10.0
    k = rng.negative_binomial(r, r / (r + mu), size=4000)
    est = fit_negbin_dispersion(k)
    assert 6.0 < est < 11.0
    assert 5.0 < moment_dispersion(k) < 12.0
    assert fit_negbin_dispersion(rng.poisson(10, 3000)) > 50  # ~Poisson


def test_main_free_mode_wiring_without_network(tmp_path, monkeypatch):
    """run_free builds the whole free pipeline and shuts down cleanly when stop is set."""
    from quantbet import main as qmain
    from quantbet.config import Settings

    s = Settings(_env_file=None, db_path=tmp_path / "a.sqlite3", http_cache_path=tmp_path / "h.sqlite3",
                 odds_cache_path=tmp_path / "o.sqlite3", football_data_ratings_path=tmp_path / "none.json",
                 engine_b_params_path=tmp_path / "none2.json", dry_run=True, alert_rules="v3",
                 espn_summary_db_path=tmp_path / "s.sqlite3")  # never touch (or migrate) the repo's data/ DB
    assert s.mode == "free" and not s.enable_api_football and not s.enable_odds_api_io and not s.enable_sofascore_odds

    async def go():
        stop = asyncio.Event()
        stop.set()
        await qmain.run_free(s, stop)

    run(go())


def test_load_live_params_roundtrip(tmp_path):
    import dataclasses

    from quantbet.engine import LiveModelParams
    from quantbet.main import load_live_params

    p = tmp_path / "p.json"
    d = dataclasses.asdict(LiveModelParams())
    d["corner_dispersion"] = 12.5 if "corner_dispersion" in d else d.get("corner_dispersion")
    d["unknown_key"] = 1
    p.write_text(json.dumps(d, default=list))
    lp = load_live_params(p)
    assert isinstance(lp, LiveModelParams)
    assert load_live_params(tmp_path / "missing.json") is None


def test_settlement_note():
    from quantbet.notifier import settlement_note

    assert settlement_note([0.4, 0, 0.2, 0, 0.4]) == "whole line: stake refunded 20%"
    assert settlement_note([0.4, 0.2, 0, 0, 0.4]).startswith("quarter line: half-win")
    assert settlement_note([0.5, 0, 0, 0, 0.5]) == "half line"


def test_page_title_for_logs():
    from quantbet.polite_http import page_title

    assert page_title("<html><head><title>Just a moment...</title></head></html>") == "Just a moment..."
    assert page_title("{}") == ""
