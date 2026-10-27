"""Post-gap delta reset: no velocity / 10-min activity across a polling gap > 180 s."""
from __future__ import annotations

import asyncio
import logging

import pytest

from quantbet.delta_tracker import NEUTRAL_MULT, POST_GAP_RESET_LOG, DeltaConfig, DeltaTracker
from quantbet.free_stats_poller import FreeStatsPoller
from quantbet.heartbeat import Gap
from quantbet.models import LiveMatchState

T0 = 1_800_000_000.0


def s(sot, shots, corners):
    return {"sot": sot, "shots": shots, "corners": corners}


def feed_normal(t, key="e", m0=50, n=6, wall0=T0, dt_wall=60.0):
    """One snapshot per match minute / 60 s wall: home pressing hard."""
    r = None
    for i in range(n):
        r = t.update(key, m0 + i, s(i, 2 * i, i), s(0, 0, 0), wall=wall0 + i * dt_wall)
    return r


def test_no_gap_keeps_normal_window():
    t = DeltaTracker()
    r = feed_normal(t)
    assert r.basis == "window" and not r.post_gap and r.mult["home"] > 1.0
    assert t.gap_resets == {}


def test_per_match_gap_discards_history_and_returns_neutral(caplog):
    t = DeltaTracker()
    feed_normal(t)  # minutes 50..55, wall T0..T0+300
    assert len(t.history("e")) == 6
    with caplog.at_level(logging.INFO, logger="quantbet.delta_tracker"):
        # 181 s later, 4 match minutes on, counters jumped (events during the gap)
        r = t.update("e", 59, s(9, 14, 8), s(1, 1, 0), wall=T0 + 300 + 181)
    assert POST_GAP_RESET_LOG == "delta_reset: post_gap"
    assert any(rec.getMessage() == "delta_reset: post_gap" for rec in caplog.records)
    assert r.reset_reason == "post_gap" and r.post_gap and r.basis == "post_gap"
    assert r.gap_s == pytest.approx(181.0)
    assert r.mult == NEUTRAL_MULT and r.momentum is None and r.velocity == {} and r.activity_10m == {}
    assert len(t.history("e")) == 1 and t.history("e")[0].minute == 59  # fresh baseline only
    assert t.gap_resets["e"] == 1


def test_exactly_threshold_is_not_a_gap():
    t = DeltaTracker()
    feed_normal(t)
    r = t.update("e", 58, s(6, 12, 6), s(0, 0, 0), wall=T0 + 300 + 180)
    assert not r.post_gap and r.basis == "window" and t.gap_resets == {}


def test_neutral_until_post_gap_window_then_only_post_gap_deltas():
    t = DeltaTracker(DeltaConfig(prior_min=0.0))
    feed_normal(t)
    w = T0 + 300 + 600  # 10-minute gap
    base = (s(20, 40, 15), s(0, 0, 0))
    t.update("e", 70, *base, wall=w)
    r = t.update("e", 72, s(20, 41, 15), s(0, 0, 0), wall=w + 120)
    assert r.post_gap and r.mult == NEUTRAL_MULT and r.reset_reason == ""  # 2 min < 5 min
    r = t.update("e", 74.9, s(20, 41, 15), s(0, 0, 0), wall=w + 240)
    assert r.post_gap and r.mult == NEUTRAL_MULT
    r = t.update("e", 75, s(21, 42, 16), s(0, 0, 0), wall=w + 300)
    assert not r.post_gap and r.basis == "window" and r.span_min == pytest.approx(5.0)
    # rates use the post-gap baseline (70'), never the pre-gap counters: 1 SOT in 5 min
    assert r.velocity["home_sot"] == pytest.approx(1 / 5)
    assert r.velocity["home_corners"] == pytest.approx(1 / 5)


def test_halftime_break_is_not_a_gap_when_polled():
    t = DeltaTracker()
    t.update("e", 44, s(1, 3, 2), s(1, 2, 1), wall=T0)
    t.update("e", 45, s(1, 3, 2), s(1, 2, 1), wall=T0 + 60)
    for i in range(1, 17):  # 16 minutes of half-time polled every 60 s
        t.update("e", 45, s(1, 3, 2), s(1, 2, 1), status="halftime", wall=T0 + 60 + 60 * i)
    r = t.update("e", 46, s(1, 3, 2), s(1, 2, 1), wall=T0 + 60 + 60 * 17)
    assert not r.post_gap and t.gap_resets == {}


def test_gap_during_halftime_baselines_on_first_live_snapshot():
    t = DeltaTracker()
    t.update("e", 40, s(1, 3, 2), s(1, 2, 1), wall=T0)
    r = t.update("e", 45, s(1, 3, 2), s(1, 2, 1), status="halftime", wall=T0 + 400)
    assert r.post_gap and r.mult == NEUTRAL_MULT and t.history("e") == []
    r = t.update("e", 47, s(2, 4, 3), s(1, 2, 1), wall=T0 + 460)
    assert r.post_gap and [x.minute for x in t.history("e")] == [47]


def test_global_heartbeat_gap_marks_every_match():
    t = DeltaTracker()
    feed_normal(t, "a")
    feed_normal(t, "b")
    assert t.note_global_gap(240.0) == ["a", "b"]
    # wall=0 (no per-match clock) -> only the heartbeat mark can trigger the reset
    r = t.update("a", 57, s(6, 12, 6), s(0, 0, 0))
    assert r.reset_reason == "post_gap" and r.gap_s == 240.0 and r.mult == NEUTRAL_MULT
    assert t.gap_resets == {"a": 1}  # "b" resets on its own next update
    r = t.update("b", 57, s(6, 12, 6), s(0, 0, 0))
    assert r.reset_reason == "post_gap" and t.gap_resets == {"a": 1, "b": 1}


def test_unseen_match_is_not_reset():
    t = DeltaTracker()
    r = t.update("new", 30, s(1, 2, 1), s(0, 1, 0), wall=T0)
    assert not r.post_gap and r.basis == "match_avg"


def _state(minute, hs, as_):
    return LiveMatchState(event_key="e", minute=minute, home_goals=0, away_goals=0, home_corners=hs[2],
                          away_corners=as_[2], home_shots_on_target=hs[0], away_shots_on_target=as_[0],
                          home_shots=hs[1], away_shots=as_[1], stats_available=True, source="espn")


def test_poller_sets_neutral_multipliers_and_resets_rolling_counts():
    p = FreeStatsPoller(None, lambda st: None)  # type: ignore[arg-type]
    for i in range(6):
        st = p.apply_synthetic_momentum(_state(50 + i, (i, 2 * i, i), (0, 0, 0)), now=T0 + 60 * i)
    assert st.momentum_mult_home > 1.0
    p.tracker.rolling("e", "sot", 55, 5)
    st = p.apply_synthetic_momentum(_state(62, (9, 15, 9), (0, 0, 0)), now=T0 + 300 + 400)
    assert (st.momentum_mult_home, st.momentum_mult_away, st.momentum_mult_total) == (1.0, 1.0, 1.0)
    assert st.activity_10m == {} and st.velocity == {} and st.momentum_source != "synthetic"
    assert not any(k[0] == "e" for k in p.tracker._hist)  # StatsTracker rolling 10-min reset too


def test_engine_neutral_multiplier_is_exactly_one():
    from quantbet.engine import LiveFootballModel

    st = _state(62, (9, 15, 9), (0, 0, 0))
    st.home_shots_on_target_10, st.attack_momentum = 9, 0.9  # would push >1 on the legacy path
    st.momentum_mult_home = st.momentum_mult_away = st.momentum_mult_total = 1.0
    assert LiveFootballModel()._momentum(st, 0.5) == (1.0, 1.0)


def test_heartbeat_loop_arms_post_gap_reset():
    from quantbet.main import heartbeat_loop

    p = FreeStatsPoller(None, lambda st: None)  # type: ignore[arg-type]
    feed_normal(p.delta, "e")
    stop = asyncio.Event()

    class Mon:
        def tick(self, live):
            stop.set()
            return Gap("suspend", T0, T0 + 900, 900.0, 900.0, 900.0, [])

    asyncio.run(heartbeat_loop(Mon(), p, stop, every_s=0.01))
    assert p.wake.is_set() and p.delta._pending_gap == {"e": 900.0}


def test_delta_config_is_not_part_of_frozen_hash():
    import dataclasses

    from quantbet.engine import LiveModelParams, PricingConfig

    names = {f.name for f in dataclasses.fields(LiveModelParams)} | {f.name for f in dataclasses.fields(PricingConfig)}
    assert not names & {"gap_reset_s", "post_gap_min_span_min"}
