"""Heartbeat + suspend/resume gap detection for the live runner.

* A heartbeat row ``(ts, ticks, live_matches)`` is written to SQLite every ``heartbeat_every_s`` (60 s).
* ``tick()`` is called often (every ~15 s by the runner). If the wall-clock **or** the monotonic gap since
  the previous tick exceeds ``gap_threshold_s`` (180 s) a ``gaps`` row is written: start, end, duration,
  the live matches at the start of the gap and their last known minute. The caller then forces an
  immediate scoreboard refresh (still through ``PoliteFetcher`` pacing/backoff).
* ``start()`` compares "now" with the last heartbeat of a previous process (restart gap).

Wall and monotonic clocks are both checked: on Linux ``time.monotonic`` does not advance during a system
suspend, wall-clock does; a frozen VM/container (cgroup freeze) usually advances both. Either way a gap
shows up in at least one of them. Clocks are injectable for tests.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS heartbeat (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts REAL, ticks INTEGER,
    live_matches TEXT);
CREATE TABLE IF NOT EXISTS gaps (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, kind TEXT, start_ts REAL,
    end_ts REAL, duration_s REAL, wall_gap_s REAL, mono_gap_s REAL, affected TEXT);
"""


@dataclass
class Gap:
    kind: str  # "suspend" (inside one process) | "restart" (since the previous process' last heartbeat)
    start: float
    end: float
    duration_s: float
    wall_gap_s: float
    mono_gap_s: float | None
    affected: list[dict[str, Any]] = field(default_factory=list)  # [{"event", "match", "minute"}]


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts))


class HeartbeatMonitor:
    def __init__(self, db_path: str | Path, run_id: str = "run", gap_threshold_s: float = 180.0,
                 heartbeat_every_s: float = 60.0, wall: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic) -> None:
        self.db = sqlite3.connect(str(db_path))
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.run_id, self.threshold, self.every = run_id, gap_threshold_s, heartbeat_every_s
        self.wall, self.mono = wall, mono
        self.ticks = 0
        self._last_wall: float | None = None
        self._last_mono: float | None = None
        self._last_hb: float | None = None
        self._last_live: dict[str, dict[str, Any]] = {}
        self.gaps: list[Gap] = []

    # -- helpers ----------------------------------------------------------------
    def _record_gap(self, g: Gap) -> Gap:
        self.db.execute("INSERT INTO gaps (run_id, kind, start_ts, end_ts, duration_s, wall_gap_s, mono_gap_s, affected)"
                        " VALUES (?,?,?,?,?,?,?,?)", (self.run_id, g.kind, g.start, g.end, g.duration_s, g.wall_gap_s,
                                                      g.mono_gap_s, json.dumps(g.affected)))
        self.db.commit()
        self.gaps.append(g)
        log.warning("gap detected", extra={"kind": g.kind, "start": _iso(g.start), "end": _iso(g.end),
                                           "duration_s": round(g.duration_s), "affected": g.affected})
        return g

    def _heartbeat(self, now: float, live: dict[str, dict[str, Any]]) -> None:
        self.db.execute("INSERT INTO heartbeat (run_id, ts, ticks, live_matches) VALUES (?,?,?,?)",
                        (self.run_id, now, self.ticks, json.dumps(sorted(live))))
        self.db.commit()
        self._last_hb = now

    # -- API --------------------------------------------------------------------
    def start(self) -> Gap | None:
        """Restart gap: time since the last heartbeat of any earlier process (if > threshold)."""
        row = self.db.execute("SELECT ts, live_matches FROM heartbeat ORDER BY ts DESC LIMIT 1").fetchone()
        now = self.wall()
        g = None
        if row and now - row[0] > self.threshold:
            prev = [{"event": k, "match": k, "minute": None} for k in json.loads(row[1] or "[]")]
            g = self._record_gap(Gap("restart", row[0], now, now - row[0], now - row[0], None, prev))
        self._last_wall, self._last_mono = now, self.mono()
        return g

    def tick(self, live: dict[str, dict[str, Any]] | None = None) -> Gap | None:
        """``live`` = {event_key: {"match": "A v B", "minute": 63.0}} for matches currently in play."""
        live = live or {}
        now, mono = self.wall(), self.mono()
        self.ticks += 1
        g = None
        if self._last_wall is not None:
            wg = now - self._last_wall
            mg = mono - self._last_mono if self._last_mono is not None else 0.0
            if wg > self.threshold or mg > self.threshold:
                aff = [{"event": k, **v} for k, v in sorted(self._last_live.items())]
                g = self._record_gap(Gap("suspend", self._last_wall, now, max(wg, mg), wg, mg, aff))
        self._last_wall, self._last_mono = now, mono
        self._last_live = dict(live)
        if self._last_hb is None or now - self._last_hb >= self.every or g is not None:
            self._heartbeat(now, live)
        return g

    def all_gaps(self, since: float | None = None) -> list[tuple]:
        q = "SELECT kind, start_ts, end_ts, duration_s, affected FROM gaps"
        args: tuple = ()
        if since is not None:
            q += " WHERE end_ts >= ?"
            args = (since,)
        return self.db.execute(q + " ORDER BY start_ts", args).fetchall()

    def report_lines(self, since: float | None = None, fixtures: dict[str, dict[str, Any]] | None = None,
                     match_len_s: float = 115 * 60) -> list[str]:
        """``fixtures`` = {event_key: {"match", "kickoff_ts", "minute"?}}: matches whose play window
        [kickoff, kickoff + match_len_s] overlaps a gap are added to "affected" even if the tick before
        the gap had not yet seen them live (e.g. the gap started right at/before kick-off)."""
        rows = self.all_gaps(since)
        if not rows:
            return ["No polling gaps > %d s detected." % int(self.threshold)]
        out = ["| kind | start (UTC) | end (UTC) | duration | live matches affected (last known minute) |",
               "|---|---|---|---|---|"]
        for kind, s, e, d, aff in rows:
            a = json.loads(aff or "[]")
            seen = {x.get("event") for x in a}
            for k, f in (fixtures or {}).items():
                ko = f.get("kickoff_ts")
                if k in seen or ko is None or not (ko < e and ko + match_len_s > s):
                    continue
                a.append({"event": k, "match": f.get("match", k),
                          "minute": f.get("minute") if s >= ko else None})
            txt = "; ".join(f"{x.get('match')} ({'?' if x.get('minute') is None else int(x['minute'])}')" for x in a) or "none"
            out.append(f"| {kind} | {_iso(s)} | {_iso(e)} | {d / 60:.1f} min | {txt} |")
        return out


def prevent_sleep_windows(enable: bool = True) -> bool:
    """Optional Windows helper: ask the OS not to sleep while the process runs
    (SetThreadExecutionState ES_CONTINUOUS|ES_SYSTEM_REQUIRED). No-op elsewhere. Returns True if applied."""
    import sys

    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes

        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enable else 0)
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return False


def start_keepalive(why: str = "quantbet live poller", platform: str | None = None,
                    runner: Callable[..., Any] | None = None) -> tuple[str, Any]:
    """In-process keep-alive for the CLI (used when not launched via scripts/run_live.sh).

    macOS: ``caffeinate -i -w <pid>``; Linux: ``systemd-inhibit --what=idle:sleep`` holding a
    ``tail --pid=<pid>`` child (only if a probe succeeds); Windows: SetThreadExecutionState.
    Returns ``(description, handle)``; the handle is a Popen (or None) that ends with this process.
    Skipped when ``QB_KEEPALIVE`` is already set by the wrapper.
    """
    import os
    import shutil
    import subprocess
    import sys

    if os.environ.get("QB_KEEPALIVE"):
        return f"wrapper: {os.environ['QB_KEEPALIVE']}", None
    plat = platform or sys.platform
    run = runner or subprocess.Popen
    pid = str(os.getpid())
    try:
        if plat == "darwin" and shutil.which("caffeinate"):
            return "caffeinate -i", run(["caffeinate", "-i", "-w", pid])
        if plat.startswith("linux") and shutil.which("systemd-inhibit"):
            probe = subprocess.run(["systemd-inhibit", "--what=idle:sleep", f"--why={why} (probe)", "true"],
                                   capture_output=True, timeout=10)
            if probe.returncode == 0:
                return "systemd-inhibit", run(["systemd-inhibit", "--what=idle:sleep", f"--why={why}", "--mode=block",
                                               "tail", f"--pid={pid}", "-f", "/dev/null"])
            return "none (systemd-inhibit unavailable)", None
        if plat.startswith("win"):
            return ("SetThreadExecutionState" if prevent_sleep_windows(True) else "none"), None
    except Exception as exc:  # noqa: BLE001
        log.warning("keep-alive failed", extra={"error": str(exc)})
    return "none", None


__all__ = ["HeartbeatMonitor", "Gap", "prevent_sleep_windows", "start_keepalive"]
