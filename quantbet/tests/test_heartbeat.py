"""Heartbeat / gap detection with simulated clocks."""

from __future__ import annotations

import asyncio
import json

from quantbet.heartbeat import HeartbeatMonitor, prevent_sleep_windows


class Clock:
    def __init__(self, wall=1_000_000.0, mono=100.0):
        self.w, self.m = wall, mono

    def adv(self, s, mono=True):
        self.w += s
        if mono:
            self.m += s


def _mon(tmp_path, c, run="r1"):
    return HeartbeatMonitor(tmp_path / "hb.sqlite3", run, wall=lambda: c.w, mono=lambda: c.m)


LIVE = {"e1": {"match": "A v B", "minute": 63.0}}


def test_no_gap_and_heartbeat_every_60s(tmp_path):
    c = Clock()
    m = _mon(tmp_path, c)
    assert m.start() is None
    for _ in range(12):  # 12 x 15 s = 3 min
        assert m.tick(LIVE) is None
        c.adv(15)
    n = m.db.execute("SELECT COUNT(*) FROM heartbeat").fetchone()[0]
    assert n == 3  # t=0, 60, 120 (165 < 180)
    row = m.db.execute("SELECT ticks, live_matches FROM heartbeat ORDER BY id DESC").fetchone()
    assert row[0] == 9 and json.loads(row[1]) == ["e1"]


def test_wall_clock_gap_logged_with_affected_matches(tmp_path):
    c = Clock()
    m = _mon(tmp_path, c)
    m.start()
    m.tick(LIVE)
    c.adv(15)
    m.tick(LIVE)
    c.adv(7800)  # 2h10 suspend/pause
    g = m.tick({})
    assert g and g.kind == "suspend" and g.duration_s == 7800 and g.affected == [{"event": "e1", "match": "A v B", "minute": 63.0}]
    rows = m.all_gaps()
    assert len(rows) == 1 and rows[0][3] == 7800
    assert "A v B (63')" in "\n".join(m.report_lines())


def test_monotonic_only_gap_and_threshold(tmp_path):
    c = Clock()
    m = _mon(tmp_path, c)
    m.start()
    m.tick(LIVE)
    c.adv(179)
    assert m.tick(LIVE) is None  # under threshold
    c.m += 400  # monotonic jumps (wall clock reset/NTP step backwards would otherwise hide it)
    g = m.tick(LIVE)
    assert g and g.mono_gap_s == 400 and g.wall_gap_s == 0


def test_restart_gap_from_previous_heartbeat(tmp_path):
    c = Clock()
    m = _mon(tmp_path, c, "afternoon")
    m.start()
    m.tick(LIVE)
    c.adv(3600)
    m2 = _mon(tmp_path, c, "evening")
    g = m2.start()
    assert g and g.kind == "restart" and g.duration_s == 3600 and g.affected[0]["event"] == "e1"
    m2.tick(LIVE)  # writes a fresh heartbeat
    c.adv(30)
    assert _mon(tmp_path, c, "evening2").start() is None  # quick restart (30 s) is not a gap


def test_no_gaps_report_line(tmp_path):
    m = _mon(tmp_path, Clock())
    assert m.report_lines() == ["No polling gaps > 180 s detected."]
    assert prevent_sleep_windows() is False  # not Windows


def test_heartbeat_loop_forces_refresh_on_gap(tmp_path):
    from quantbet.main import heartbeat_loop

    c = Clock()
    m = _mon(tmp_path, c)
    m.start()

    class P:
        refreshed = 0

        def live_snapshot(self):
            return LIVE

        def request_refresh(self):
            P.refreshed += 1

    async def go():
        stop = asyncio.Event()
        t = asyncio.create_task(heartbeat_loop(m, P(), stop, every_s=0.01))
        await asyncio.sleep(0.05)
        c.adv(600)
        await asyncio.sleep(0.05)
        stop.set()
        await t

    asyncio.run(go())
    assert P.refreshed == 1 and len(m.all_gaps()) == 1


def test_poller_wake_skips_timer(tmp_path):
    from quantbet.free_stats_poller import FreePollerConfig, FreeStatsPoller

    class F:
        async def get_json(self, *a, **k):
            return None

    p = FreeStatsPoller(F(), lambda st: None, cfg=FreePollerConfig(use_espn=False, use_sofascore=False, use_fotmob=False,
                                                                  stats_interval_s=3600))

    async def go():
        stop = asyncio.Event()
        t = asyncio.create_task(p.run(stop))
        await asyncio.sleep(0.05)
        assert p.n_polls == 1
        p.request_refresh()
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(t, 2)

    asyncio.run(go())
    assert p.n_polls == 2
