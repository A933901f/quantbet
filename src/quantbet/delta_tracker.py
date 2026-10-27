"""Rolling delta tracker -> 10-minute velocity and a synthetic momentum index.

ESPN (Tier 1) gives only cumulative counters (corners, shots, shots on target)
- no xG and no momentum graph. This module keeps timestamped snapshots per
match and turns them into:

* **velocity**: per-minute rates over the last ``window_min`` (10) *match*
  minutes, computed from the last two ~``interval_min`` (5) minute intervals
  (``rate = (c_now - c_-10) / (m_now - m_-10)``; the two half-window rates are
  exposed too, for acceleration).
* **synthetic momentum** in [-1, +1] (positive = home pressure)::

      x = sum_k w_k * (r_home_k - r_away_k) / E_k  /  sum_k w_k
      momentum = tanh(x)

  with weights SOT 3, shots 1, corners 1.5 and ``E_k`` the *pre-match
  expected* total per-minute rate (corners from the fixture's ratings, shots /
  SOT from league averages). Rates are shrunk toward an even split with
  ``prior_min`` minutes of expected events so 1 lucky shot does not swing it.

Edge cases: half-time (minute frozen -> snapshot ignored), counter decreases or
source switch (history reset), a missing stat (dropped from the index; all
missing -> ``None``), and too-short history (falls back to whole-match average
rates, flagged ``basis="match_avg"``). The index is *uncalibrated* heuristics -
tune the weights/expectations on your own logged data.

Post-gap delta reset (data-pipeline rule, not an Engine-B parameter)
--------------------------------------------------------------------
Velocity / 10-minute activity must never be computed across a polling gap (laptop sleep, VM freeze,
source back-off, process restart): the counter deltas would be real but their timing unknown.

* **Detection.** Per match: the wall-clock time since that match's previous ``update`` (half-time
  updates included, so the interval break itself is not a gap) exceeds ``gap_reset_s`` (180 s).
  Globally: the heartbeat monitor reports a suspend gap -> :meth:`DeltaTracker.note_global_gap`
  marks every tracked match; its next update is treated as post-gap.
* **Action.** All prior snapshots of that match are discarded and the new state is stored as a fresh
  baseline. ``delta_reset: post_gap`` is logged (event, gap seconds, minute).
* **Neutral window.** Until the post-gap history spans ``post_gap_min_span_min`` (5) match minutes
  the reading has ``basis="post_gap"``, no velocity/activity, ``momentum=None`` and neutral activity
  multipliers ``{"home": 1.0, "away": 1.0, "total": 1.0}`` (explicitly set, so Engine B does not fall
  back to other live-pressure inputs either). After that the normal window maths resumes using only
  post-gap snapshots (the 10-minute window is scaled from the shorter post-gap span until it fills).
  The whole-match-average fallback (``basis="match_avg"``) is *not* used post-gap because it would
  smear pre-gap activity into the current rate.

None of this touches ``LiveModelParams`` / ``PricingConfig`` (the frozen, hashed rule set): it only
decides which snapshots feed the multipliers.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

STATS: tuple[str, ...] = ("sot", "shots", "corners")
POST_GAP_RESET_LOG = "delta_reset: post_gap"
NEUTRAL_MULT: dict[str, float] = {"home": 1.0, "away": 1.0, "total": 1.0}


@dataclass(frozen=True, slots=True)
class DeltaConfig:
    window_min: float = 10.0
    interval_min: float = 5.0
    min_span_min: float = 3.0  # below this use whole-match averages
    weights: tuple[tuple[str, float], ...] = (("sot", 3.0), ("shots", 1.0), ("corners", 1.5))
    expected_shots_90: float = 25.0  # both teams, top-5-league average (approx.)
    expected_sot_90: float = 8.5
    expected_corners_90: float = 10.0  # overridden per fixture by ratings.corner_total
    prior_min: float = 4.0  # shrinkage: minutes of "expected, even" pseudo-events
    max_snapshots: int = 200
    # Activity multiplier (the ONLY path by which live pressure reaches Engine B):
    # activity_10m = 1.5*dCorners + 1.2*dSOT + 0.5*dShotsOff (per 10 min),
    # shots_off = max(0, totalShots - shotsOnTarget);
    # mult = clip(1 + 0.08*(activity_10m/baseline - 1), 0.85, 1.25).
    activity_weights: tuple[tuple[str, float], ...] = (("corners", 1.5), ("sot", 1.2), ("shots_off", 0.5))
    activity_baseline_total: float = 3.5  # combined points per 10 min in a normal match
    activity_baseline_team: float = 1.75
    activity_slope: float = 0.08
    mult_min: float = 0.85
    mult_max: float = 1.25
    # Post-gap delta reset (see module docstring). Pipeline rule, not part of the frozen param hash.
    gap_reset_s: float = 180.0  # wall-clock seconds between two updates of the same match
    post_gap_min_span_min: float = 5.0  # match minutes of post-gap history before velocity resumes


@dataclass(slots=True)
class Snapshot:
    minute: float
    wall: float
    home: dict[str, float | None]
    away: dict[str, float | None]


@dataclass(slots=True)
class MomentumReading:
    """Output of :meth:`DeltaTracker.update`."""

    momentum: float | None
    velocity: dict[str, float] = field(default_factory=dict)  # e.g. {"home_sot": 0.2, ...} per minute
    window_counts: dict[str, float] = field(default_factory=dict)  # events in the window (scaled to 10 min)
    span_min: float = 0.0
    basis: str = "none"  # "window" | "match_avg" | "none"
    n_snapshots: int = 0
    components: dict[str, float] = field(default_factory=dict)
    activity_10m: dict[str, float] = field(default_factory=dict)  # {"home", "away", "total"}
    mult: dict[str, float] = field(default_factory=dict)  # {"home", "away", "total"} momentum multipliers
    post_gap: bool = False  # inside the neutral post-gap window (basis == "post_gap")
    reset_reason: str = ""  # "post_gap" when this very update discarded history after a gap
    gap_s: float | None = None  # size of the gap that triggered the reset


class DeltaTracker:
    """Per-match snapshot history -> velocity + synthetic momentum."""

    def __init__(self, cfg: DeltaConfig | None = None) -> None:
        self.cfg = cfg or DeltaConfig()
        self._hist: dict[str, deque[Snapshot]] = {}
        self._source: dict[str, str] = {}
        self.resets: dict[str, int] = {}
        self.gap_resets: dict[str, int] = {}
        self._last_wall: dict[str, float] = {}  # wall time of the last update (half-time included)
        self._pending_gap: dict[str, float] = {}  # event_key -> global gap seconds (heartbeat)
        self._post_gap: dict[str, float | None] = {}  # event_key -> baseline minute (None = not yet stored)

    def reset(self, event_key: str) -> None:
        self._hist.pop(event_key, None)
        self.resets[event_key] = self.resets.get(event_key, 0) + 1

    def note_global_gap(self, gap_s: float) -> list[str]:
        """Heartbeat saw a polling gap: the next update of every tracked match is a fresh baseline."""
        keys = sorted(set(self._hist) | set(self._last_wall))
        for k in keys:
            self._pending_gap[k] = max(gap_s, self._pending_gap.get(k, 0.0))
        return keys

    def in_post_gap(self, event_key: str) -> bool:
        return event_key in self._post_gap

    def _post_gap_reset(self, event_key: str, gap_s: float, minute: float, why: str) -> None:
        had = len(self._hist.get(event_key, ()))
        self.reset(event_key)
        self.gap_resets[event_key] = self.gap_resets.get(event_key, 0) + 1
        self._post_gap[event_key] = None
        log.info(POST_GAP_RESET_LOG, extra={"event": event_key, "gap_s": round(gap_s, 1), "minute": minute,
                                            "detected_by": why, "discarded_snapshots": had})

    def history(self, event_key: str) -> list[Snapshot]:
        return list(self._hist.get(event_key, ()))

    # -- core -----------------------------------------------------------------
    def _expected_per_min(self, stat: str, corner_total: float | None) -> float:
        c = self.cfg
        per90 = {"sot": c.expected_sot_90, "shots": c.expected_shots_90, "corners": corner_total or c.expected_corners_90}[stat]
        return max(per90, 1e-6) / 90.0

    @staticmethod
    def _anchor(hist: list[Snapshot], target_minute: float) -> Snapshot | None:
        """Latest snapshot at or before ``target_minute`` (else the oldest one)."""
        best = None
        for s in hist:
            if s.minute <= target_minute + 1e-9:
                best = s
        return best

    def update(
        self,
        event_key: str,
        minute: float,
        home: dict[str, float | None],
        away: dict[str, float | None],
        *,
        status: str = "",
        source: str = "",
        wall: float = 0.0,
        corner_total: float | None = None,
    ) -> MomentumReading:
        """Add a snapshot (``home``/``away`` keys: sot, shots, corners; None = missing) and compute."""
        c = self.cfg
        # -- post-gap detection (before anything else touches the history) -----------------------
        gap_s, why = None, ""
        lw = self._last_wall.get(event_key)
        if wall > 0 and lw is not None and lw > 0 and wall - lw > c.gap_reset_s:
            gap_s, why = wall - lw, "match"
        pend = self._pending_gap.pop(event_key, None)
        if pend is not None and (gap_s is None or pend > gap_s):
            gap_s, why = pend, "heartbeat"
        if wall > 0:
            self._last_wall[event_key] = wall
        reset_now = False
        if gap_s is not None and (self._hist.get(event_key) or event_key in self._post_gap):
            self._post_gap_reset(event_key, gap_s, minute, why)
            reset_now = True
        dq = self._hist.setdefault(event_key, deque(maxlen=c.max_snapshots))
        if source and self._source.get(event_key) not in (None, source):
            self.reset(event_key)
            dq = self._hist.setdefault(event_key, deque(maxlen=c.max_snapshots))
        if source:
            self._source[event_key] = source
        halftime = status.lower() in ("halftime", "ht", "pause")
        if dq and not halftime:
            last = dq[-1]
            decreased = minute < last.minute - 0.01 or any(
                (home.get(k) is not None and last.home.get(k) is not None and home[k] < last.home[k])  # type: ignore[operator]
                or (away.get(k) is not None and last.away.get(k) is not None and away[k] < last.away[k])  # type: ignore[operator]
                for k in STATS
            )
            if decreased:
                self.reset(event_key)
                dq = self._hist.setdefault(event_key, deque(maxlen=c.max_snapshots))
        snap = Snapshot(minute, wall, dict(home), dict(away))
        if not halftime:
            if dq and abs(dq[-1].minute - minute) < 1e-6:
                dq[-1] = snap  # same match minute: keep the freshest counters
            else:
                dq.append(snap)
            # drop history older than window + one interval (keep one anchor before the window)
            while len(dq) > 2 and minute - dq[1].minute >= c.window_min:
                dq.popleft()
        hist = list(dq)
        if event_key in self._post_gap:
            if self._post_gap[event_key] is None and hist:
                self._post_gap[event_key] = hist[0].minute  # fresh baseline stored (diagnostics)
            # history holds only post-gap snapshots (it was cleared at the reset)
            post_span = (hist[-1].minute - hist[0].minute) if hist else 0.0
            if post_span < c.post_gap_min_span_min:
                return MomentumReading(None, span_min=round(post_span, 2), basis="post_gap", n_snapshots=len(hist),
                                       mult=dict(NEUTRAL_MULT), post_gap=True,
                                       reset_reason="post_gap" if reset_now else "", gap_s=gap_s if reset_now else None)
            del self._post_gap[event_key]  # enough post-gap history: normal window maths resumes
        if not hist:
            return MomentumReading(None)
        cur = hist[-1]
        a10 = self._anchor(hist, cur.minute - c.window_min) or hist[0]
        a5 = self._anchor(hist, cur.minute - c.interval_min) or hist[0]
        span = cur.minute - a10.minute
        basis = "window"
        if span < c.min_span_min:
            basis = "match_avg" if cur.minute >= 1.0 else "none"
        out = MomentumReading(None, span_min=round(span, 2), basis=basis, n_snapshots=len(hist))
        if basis == "none":
            return out
        num = den = 0.0
        for stat, w in c.weights:
            hv, av = cur.home.get(stat), cur.away.get(stat)
            if hv is None or av is None:
                continue
            if basis == "window":
                h0, a0 = a10.home.get(stat), a10.away.get(stat)
                if h0 is None or a0 is None:
                    continue
                dh, da, dt = hv - h0, av - a0, span
                # half-window rates (acceleration diagnostics)
                h5, a5v = a5.home.get(stat), a5.away.get(stat)
                dt2 = cur.minute - a5.minute
                if h5 is not None and a5v is not None and dt2 > 0:
                    out.velocity[f"home_{stat}_last5"] = round((hv - h5) / dt2, 4)
                    out.velocity[f"away_{stat}_last5"] = round((av - a5v) / dt2, 4)
            else:
                dh, da, dt = hv, av, cur.minute
            rh, ra = dh / dt, da / dt
            out.velocity[f"home_{stat}"] = round(rh, 4)
            out.velocity[f"away_{stat}"] = round(ra, 4)
            out.window_counts[f"home_{stat}"] = round(rh * c.window_min, 3)
            out.window_counts[f"away_{stat}"] = round(ra * c.window_min, 3)
            e = self._expected_per_min(stat, corner_total)
            pseudo = e * c.prior_min / 2.0  # even split of expected events
            srh = (dh + pseudo) / (dt + c.prior_min)
            sra = (da + pseudo) / (dt + c.prior_min)
            comp = (srh - sra) / e
            out.components[stat] = round(comp, 4)
            num += w * comp
            den += w
        self._activity(out)
        return out

    def _activity(self, out: MomentumReading) -> None:
        """Weighted 10-minute activity and bounded multipliers (needs corners, SOT and shots)."""
        c = self.cfg
        wc = out.window_counts
        if not all(f"{side}_{k}" in wc for side in ("home", "away") for k in ("corners", "sot", "shots")):
            return
        act: dict[str, float] = {}
        for side in ("home", "away"):
            comp = {"corners": wc[f"{side}_corners"], "sot": wc[f"{side}_sot"],
                    "shots_off": max(0.0, wc[f"{side}_shots"] - wc[f"{side}_sot"])}
            act[side] = sum(w * comp[k] for k, w in c.activity_weights)
        act["total"] = act["home"] + act["away"]
        out.activity_10m = {k: round(v, 4) for k, v in act.items()}

        def mult(a: float, base: float) -> float:
            return round(min(c.mult_max, max(c.mult_min, 1.0 + c.activity_slope * (a / base - 1.0))), 4)

        out.mult = {"home": mult(act["home"], c.activity_baseline_team),
                    "away": mult(act["away"], c.activity_baseline_team),
                    "total": mult(act["total"], c.activity_baseline_total)}
        # Signed dominance index in [-1, 1] - used only as a *trigger*, never for pricing.
        out.momentum = round((act["home"] - act["away"]) / max(act["total"], c.activity_baseline_total), 4)


__all__ = ["DeltaConfig", "DeltaTracker", "MomentumReading", "NEUTRAL_MULT", "POST_GAP_RESET_LOG", "Snapshot", "STATS"]
