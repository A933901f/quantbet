"""Replay today's captured live states under Old (legacy) and New (v3) alert rules and grade both.

    python scripts/replay_rules.py --date 2026-10-25

Inputs (all local, no network):
* dryrun/run_attempt2.log + dryrun/run.log  - JSON "live state" lines (minute, score, corners, SOT,
  activity multipliers, synthetic momentum, status, ratings source) and "fotmob confirmation" lines.
* data/dryrun_http.sqlite3  - latest cached ESPN soccer/all scoreboard (names, kick-off, red-card
  details with clock, final score + corners).
* data/espn_summary.sqlite3 - once-per-match ESPN summaries (DraftKings pickcenter prior).
* dryrun/paper_trades.sqlite3 - the original live paper alerts (to check the Old-Rules reproduction).

Grading: min-odds fill, first-tier (quarter-Kelly at min odds) stake, full-match totals,
quarter lines split in half stakes (same as scripts/paper_dry_run.py).
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quantbet.config import Settings  # noqa: E402
from quantbet.engine import (  # noqa: E402
    FreeSignalEngine, LiveModelParams, PricingConfig, RatingsResolver, SignalEngine, chasing_corner_penalty_active,
)
from quantbet.espn import parse_espn_scoreboard, parse_espn_summary, split_all_scoreboard  # noqa: E402
from quantbet.football_data import load_ratings  # noqa: E402
from quantbet.fotmob_page import fotmob_dominance, xg_confirm  # noqa: E402
from quantbet.main import build_engine_config, build_risk, load_live_params  # noqa: E402
from quantbet.models import LiveMatchState, MarketType, Selection  # noqa: E402
from quantbet.paper import pnl, result_label, settle_total  # noqa: E402
from quantbet.poller import EventMatcher  # noqa: E402

LEGACY_FLAGS = ("shrinkage_enabled", "game_state_dampener", "fav_rule", "chasing_corner_penalty")


def _ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def load_log(path: Path, segment: str) -> tuple[list[dict], list[dict]]:
    states, fotmob = [], []
    if not path.exists():
        return states, fotmob
    for line in path.open():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        d["_t"] = _ts(d["ts"])
        d["_seg"] = segment
        if d.get("msg") == "live state":
            states.append(d)
        elif d.get("msg") == "fotmob confirmation":
            fotmob.append(d)
    return states, fotmob


@dataclass
class Meta:
    id: str
    home: str
    away: str
    league: str
    start: Any
    reds: list[tuple[float, str]] = field(default_factory=list)  # (minute, "home"/"away")
    final: dict | None = None


def load_meta(http_db: Path, leagues: list[str]) -> dict[str, Meta]:
    c = sqlite3.connect(str(http_db))
    row = c.execute("SELECT body FROM http_cache WHERE url LIKE '%soccer/all/scoreboard%' ORDER BY fetched_at DESC").fetchone()
    payload = json.loads(row[0])
    raw_by_id = {str(e.get("id")): e for e in payload.get("events") or []}
    matcher = EventMatcher()
    out: dict[str, Meta] = {}
    for lg, part in split_all_scoreboard(payload, leagues).items():
        for ev in parse_espn_scoreboard(part, lg):
            key = matcher.match("espn", ev.id, ev.home, ev.away, ev.league_name, ev.start)
            m = Meta(ev.id, ev.home, ev.away, ev.league_name, ev.start)
            comp = ((raw_by_id.get(ev.id) or {}).get("competitions") or [{}])[0]
            for d in comp.get("details") or []:
                txt = str((d.get("type") or {}).get("text") or "").lower()
                if d.get("redCard") or txt in ("red card", "yellow-red card", "second yellow card"):
                    tid = str((d.get("team") or {}).get("id") or "")
                    mins = float((d.get("clock") or {}).get("value") or 0) / 60.0
                    m.reds.append((mins, "home" if tid == ev.home_id else "away"))
            if ev.state == "post":
                m.final = {"home_goals": ev.home_score, "away_goals": ev.away_score,
                           "home_corners": ev.home_stats.get("wonCorners"), "away_corners": ev.away_stats.get("wonCorners")}
            out[key] = m
    return out


def load_priors(summary_db: Path) -> dict[str, Any]:
    c = sqlite3.connect(str(summary_db))
    out = {}
    for ev_id, body in c.execute("SELECT event_id, body FROM espn_summary_once"):
        try:
            summ = parse_espn_summary(json.loads(body))
        except Exception:  # noqa: BLE001
            continue
        if summ.prior is not None:
            out[str(ev_id)] = (summ.prior, f"espn_{summ.prior_provider.lower() or 'pickcenter'}")
    return out


def build_state(d: dict, meta: Meta | None) -> LiveMatchState:
    hg, ag = (int(x) for x in d["score"].split("-"))
    status = d.get("status") or ""
    minute = float(d["minute"])
    period = 2 if status.startswith("2nd") or (minute > 45 and status != "halftime" and not status.startswith("1st")) else 1
    hr = sum(1 for t, s in (meta.reds if meta else []) if t <= minute + 1e-9 and s == "home")
    ar = sum(1 for t, s in (meta.reds if meta else []) if t <= minute + 1e-9 and s == "away")
    mult = d.get("mult") or [None, None, None]
    sot = d.get("sot") or [None, None]
    corners = d.get("corners") or [0, 0]
    return LiveMatchState(
        event_key=d["event"], minute=minute, home_goals=hg, away_goals=ag,
        home_corners=int(corners[0] or 0), away_corners=int(corners[1] or 0),
        home_red_cards=hr, away_red_cards=ar, period=period, updated_at=d["_t"], source="espn",
        source_event_id=meta.id if meta else "", home=meta.home if meta else d["event"].split(":")[1],
        away=meta.away if meta else d["event"].split(":")[2], league=meta.league if meta else "",
        commence_time=meta.start if meta else None, status=status,
        home_shots_on_target=sot[0], away_shots_on_target=sot[1],
        attack_momentum=d.get("momentum"), momentum_source=d.get("momentum_src") or "",
        stats_available=d.get("corners") is not None,
        momentum_mult_home=mult[0], momentum_mult_away=mult[1], momentum_mult_total=mult[2],
        activity_10m=d.get("activity") or {}, fetched_at=d["_t"],
    )


class Replay:
    def __init__(self, name: str, legacy: bool, s: Settings, meta: dict[str, Meta], priors: dict[str, Any],
                 fotmob: dict[str, list[dict]]) -> None:
        self.name, self.legacy, self.s, self.meta, self.priors, self.fotmob = name, legacy, s, meta, priors, fotmob
        self.cur_src: dict[str, str] = {}
        self.alerts: list[dict] = []
        self.traces: dict[tuple[str, float], dict] = {}
        self.fotmob_drops: dict[tuple[str, float, str, float], str] = {}
        self.reset()

    def reset(self) -> None:
        s = self.s
        kw = dict(target_edge=s.target_edge, abort_edge=s.abort_edge, kelly_multiplier=s.kelly_multiplier,
                  min_minute=s.min_alert_minute, max_minute=s.max_alert_minute, realert_cooldown_s=s.realert_cooldown_s,
                  bankroll=s.bankroll, max_match_stake_frac=s.max_match_stake_frac, family_cooldown_min=s.family_cooldown_min)
        pricing = PricingConfig.legacy(**kw) if self.legacy else PricingConfig(**kw)
        lp = load_live_params(ROOT / s.engine_b_params_path) or LiveModelParams()
        if self.legacy:
            import dataclasses
            lp = LiveModelParams.legacy(**{f.name: getattr(lp, f.name) for f in dataclasses.fields(LiveModelParams)
                                           if f.name not in LEGACY_FLAGS})
        self.params = lp

        def prior_lookup(key: str):  # type: ignore[no-untyped-def]
            if not str(self.cur_src.get(key, "")).startswith("espn_"):
                return None
            m = self.meta.get(key)
            return self.priors.get(m.id) if m else None

        resolver = RatingsResolver(None, load_ratings(ROOT / s.football_data_ratings_path), devig_method=s.devig_method,
                                   prior_lookup=prior_lookup)
        risk = build_risk(s)
        self.risk = risk
        self.free = FreeSignalEngine(resolver, pricing, SignalEngine(build_engine_config(s), lp),
                                     sizer=lambda f, o: risk.stake_from_kelly(f))
        self.free.auto_commit = False

    def fotmob_at(self, key: str, t: float) -> dict | None:
        best = None
        for e in self.fotmob.get(key, []):
            if e["_t"] <= t + 5 and t - e["_t"] <= 300 and e.get("xg") and e["xg"][0] is not None:
                best = e
        return best

    def confirm(self, sigs: list, st: LiveMatchState, t: float, pre_gate: bool) -> list:
        s = self.s
        lo, hi = s.fotmob_gate_minutes
        out = []
        for sig in sigs:
            x = sig.extra
            evr = x.get("ev_at_reference")
            if not pre_gate and evr is not None and evr < 0 and s.drop_negative_reference_ev:
                self.fotmob_drops[(sig.event_key, t, sig.market.value, sig.line)] = "neg_ref_ev(main)"
                continue
            fm = self.fotmob_at(sig.event_key, t)
            hx, ax = (fm["xg"] if fm else (None, None))
            hs_, as_ = (fm["sib"] if fm else (None, None))
            if x.get("fotmob_required") == "dominance":
                dom = fotmob_dominance(x.get("dominant_side", "home"), hx, ax, hs_, as_) if fm else "unavailable"
                x["fotmob"] = f"dominance_{dom}"
                if dom != "confirmed":
                    self.fotmob_drops[(sig.event_key, t, sig.market.value, sig.line)] = "fotmob_corner_75"
                    continue
                out.append(sig)
                continue
            if pre_gate:
                x["fotmob"] = "not_checked (pre-gate v1 run)"
                out.append(sig)
                continue
            if not (lo <= (sig.minute or 0) <= hi) or evr is None or evr < s.fotmob_ev_gate:
                x["fotmob"] = "not_checked"
                out.append(sig)
                continue
            v = xg_confirm(sig.selection.value, sig.minute or 0, hx, ax, hs_, as_, x.get("lambda_pre_total")) if fm else "unavailable"
            x["fotmob"] = v
            if v == "contradicted" and s.fotmob_drop_contradicted:
                self.fotmob_drops[(sig.event_key, t, sig.market.value, sig.line)] = "fotmob_contradicted"
                continue
            out.append(sig)
        return out

    def run(self, states: list[dict], restart_between_segments: bool) -> None:
        seg = None
        for d in states:
            if restart_between_segments and seg is not None and d["_seg"] != seg:
                self.reset()  # the live run restarted at 12:58:30 (fresh limiter)
            seg = d["_seg"]
            self.cur_src[d["event"]] = d.get("ratings_src") or ""
            st = build_state(d, self.meta.get(d["event"]))
            pre_gate = self.legacy and d["_seg"] == "attempt2"
            sigs = self.free.on_match_state(st, now=d["_t"])
            self.traces[(d["event"], d["_t"])] = dict(self.free.last_trace.get(d["event"], {}))
            sigs = self.confirm(sigs, st, d["_t"], pre_gate)
            for sg in sigs:
                self.free.commit(sg)
            for sg in self.risk.size_all(sigs):
                x = sg.extra
                tiers = x.get("tiers") or []
                self.alerts.append({
                    "t": d["_t"], "event": sg.event_key, "home": sg.home, "away": sg.away, "minute": sg.minute,
                    "score": sg.score, "corners": f"{x.get('home_corners')}-{x.get('away_corners')}",
                    "market": sg.market.value, "line": sg.line, "selection": sg.selection.value,
                    "fair": sg.fair_odds, "min_odds": x.get("min_odds"), "stake": float(tiers[0][1]) if tiers else 0.0,
                    "settlement": x.get("settlement"), "triggers": x.get("triggers"), "fotmob": x.get("fotmob"),
                    "ev_ref": x.get("ev_at_reference"), "growth": x.get("log_growth"), "notes": x.get("notes"),
                    "ratings_src": x.get("ratings_source"),
                })

    def grade(self) -> None:
        for a in self.alerts:
            fin = (self.meta.get(a["event"]) or Meta("", "", "", "", None)).final
            a["result"] = None
            if not fin:
                continue
            if a["market"] == "TOTAL_CORNERS":
                if fin.get("home_corners") is None:
                    continue
                total = float(fin["home_corners"]) + float(fin["away_corners"])
            else:
                total = float(fin["home_goals"]) + float(fin["away_goals"])
            parts = settle_total(a["line"], a["selection"], total)
            a["result"] = result_label(parts)
            a["pnl_u"] = pnl(parts, a["min_odds"], 1.0)
            a["pnl_m"] = pnl(parts, a["min_odds"], a["stake"])
            a["ft"] = f"{fin['home_goals']}-{fin['away_goals']} / {fin.get('home_corners'):g}-{fin.get('away_corners'):g}"


def metrics(alerts: list[dict]) -> dict[str, Any]:
    g = [a for a in alerts if a.get("result")]
    rec = Counter(a["result"] for a in g)
    staked = sum(a["stake"] for a in g)
    pu = sum(a["pnl_u"] for a in g)
    pm = sum(a["pnl_m"] for a in g)
    br = ll = 0.0
    nb = 0
    for a in g:
        if a["result"] == "push" or not a["settlement"]:
            continue
        w, hw, p, hl, l_ = a["settlement"]
        prob = min(max((w + hw) / max(1e-9, 1 - p), 1e-6), 1 - 1e-6)
        y = 1.0 if a["result"] in ("win", "half-win") else 0.0
        br += (prob - y) ** 2
        ll += -(y * math.log(prob) + (1 - y) * math.log(1 - prob))
        nb += 1
    per_match = defaultdict(float)
    for a in alerts:
        per_match[a["event"]] += a["stake"]
    mx = max(per_match.items(), key=lambda kv: kv[1]) if per_match else ("–", 0.0)
    return {
        "alerts": len(alerts), "graded": len(g), "open": len(alerts) - len(g),
        "matches": len({a["event"] for a in alerts}), "staked_all": sum(a["stake"] for a in alerts), "staked": staked,
        "flat_pnl": pu, "flat_roi": (pu / len(g)) if g else None, "qk_pnl": pm, "qk_roi": (pm / staked) if staked else None,
        "record": ", ".join(f"{k} {rec[k]}" for k in ("win", "half-win", "push", "half-loss", "loss") if rec.get(k)) or "–",
        "brier": br / nb if nb else None, "logloss": ll / nb if nb else None, "n_brier": nb,
        "max_match": mx,
    }


def original_rows(db: Path) -> list[dict]:
    c = sqlite3.connect(str(db))
    cur = c.execute("SELECT event_key, minute, market, line, selection, min_odds, stake, result, pnl_units, pnl_money"
                    " FROM paper_trades ORDER BY id")
    cols = [x[0] for x in cur.description]
    return [dict(zip(cols, r)) for r in cur]


RULE_OF = {
    "not_best": "R1 single-best-line",
    "cooldown": "R1 family cooldown (15')",
    "family_cap": "R1 family cap (1 per family)",
    "match_signal_cap": "R1 match cap (2 signals)",
    "stake_cap": "R1 match stake cap (5%)",
    "fotmob_corner_75": "R4 FotMob dominance for corner Over >75'",
    "fotmob_contradicted": "FotMob xG/SIB contradicted (existing rule)",
}


def attribute(old: Replay, new: Replay) -> tuple[Counter, dict[int, str]]:
    cnt: Counter = Counter()
    per: dict[int, str] = {}
    new_keys = {(a["event"], a["t"], a["market"], a["line"]) for a in new.alerts}
    for i, a in enumerate(old.alerts):
        k = (a["event"], a["t"], a["market"], a["line"])
        if k in new_keys:
            per[i] = "kept"
            cnt["kept"] += 1
            continue
        tr = new.traces.get((a["event"], a["t"]), {})
        mk = MarketType(a["market"])
        reason = tr.get((mk, a["line"], Selection(a["selection"])))
        drop = new.fotmob_drops.get(k)
        if reason is None:
            why = "model repricing (line no longer in priced range)"
        elif reason.startswith("gate:"):
            if "outside windows" in reason:
                why = "R2 minute gate (30-43 / 55-85)"
            elif "no trigger" in reason:
                why = "R3 fav_chasing redefinition (no trigger left)"
            else:
                why = "gate: " + reason[5:].strip()
        elif reason in RULE_OF:
            why = RULE_OF[reason]
        elif reason == "emitted" and drop:
            why = RULE_OF.get(drop, drop)
        elif reason in ("fair_range", "no_tiers", "neg_ref_ev", "direction"):
            why = f"model repricing ({reason})"
        else:
            why = reason
        if why.startswith("model repricing"):
            hg, ag = (int(x) for x in a["score"].split("-"))
            if abs(hg - ag) >= 2:
                why = "R3 dampener + R2 shrinkage repricing"
            else:
                why = "R2 shrinkage (+R4 penalty) repricing"
        per[i] = why
        cnt[why] += 1
    return cnt, per


def fmt(v: Any, pct: bool = False, nd: int = 2) -> str:
    if v is None:
        return "–"
    return f"{100 * v:+.1f}%" if pct else f"{v:+.{nd}f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-10-25")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    dd = ROOT / "dryrun"
    st_a, fm_a = load_log(dd / "run_attempt2.log", "attempt2")
    st_b, fm_b = load_log(dd / "run.log", "run")
    states = sorted(st_a + st_b, key=lambda d: d["_t"])
    fotmob: dict[str, list[dict]] = defaultdict(list)
    for e in fm_a + fm_b:
        fotmob[e["event"]].append(e)
    meta = load_meta(ROOT / "data/dryrun_http.sqlite3", list(s.espn_leagues))
    priors = load_priors(ROOT / "data/espn_summary.sqlite3")

    old = Replay("old", True, s, meta, priors, fotmob)
    old.run(states, restart_between_segments=True)
    old.grade()
    new = Replay("new", False, s, meta, priors, fotmob)
    new.run(states, restart_between_segments=False)
    new.grade()
    mo, mn = metrics(old.alerts), metrics(new.alerts)

    # reproduction check vs. the live paper table
    orig = original_rows(dd / "paper_trades.sqlite3")
    ok_ = Counter((r["event_key"], int(round(r["minute"])), r["market"], float(r["line"])) for r in orig)
    rk = Counter((x["event"], int(round(x["minute"])), x["market"], float(x["line"])) for x in old.alerts)
    only_orig = sorted((ok_ - rk).elements())
    only_rep = sorted((rk - ok_).elements())
    attr, per = attribute(old, new)

    last_t = max(d["_t"] for d in states)
    gap = [(states[i - 1]["_t"], states[i]["_t"]) for i in range(1, len(states)) if states[i]["_t"] - states[i - 1]["_t"] > 600]
    L = [f"# Rules replay v3 – {a.date} (Old Rules vs New Rules)", "",
         f"Generated {datetime.utcnow():%Y-%m-%d %H:%M} UTC from {len(states)} logged live states "
         f"({len({d['event'] for d in states})} matches, last state {datetime.utcfromtimestamp(last_t):%H:%M} UTC). "
         "No network calls; nothing sent.", "",
         "## Caveats (read first)", "",
         "* **In-sample**: the v3 rules were designed after seeing these exact results. Any improvement below is "
         "expected by construction and is *not* evidence of edge.",
         "* **Tiny sample**: a few dozen correlated bets on ~20 matches; Brier/log-loss/ROI are noise-dominated.",
         "* **Missing window**: the box was paused ~14:35-16:45 UTC" + (
             " (log gap " + ", ".join(f"{datetime.utcfromtimestamp(x):%H:%M}-{datetime.utcfromtimestamp(y):%H:%M}" for x, y in gap) + " UTC)"
             if gap else "") + ", so the second halves of the 14:00 UTC matches (and part of the 15:15 games) have no "
         "states: neither rule set could alert there. Those matches are graded only on alerts from before the gap.",
         "* **FotMob in replay** = only what was logged live (one Inter snapshot at 78', Celta unavailable). Every other "
         "check counts as *unavailable*: the corner-Over-after-75' rule then suppresses; the generic xG gate lets the "
         "alert through tagged unavailable (as live).",
         "* States are rebuilt from the log line (multipliers rounded to 4 dp; red cards from the final ESPN details "
         "with their clock; ratings prior used only where the live line had an espn_* ratings source).",
         "* Same min-odds fill assumption: each bet filled exactly at its minimum odds with the first-tier quarter-Kelly stake.", "",
         "## Side by side", "",
         "| metric | Old Rules (replay) | New Rules v3 (replay) |", "|---|---|---|"]
    rows = [
        ("Total alerts", mo["alerts"], mn["alerts"]),
        ("Graded / open", f"{mo['graded']} / {mo['open']}", f"{mn['graded']} / {mn['open']}"),
        ("Matches with alerts", mo["matches"], mn["matches"]),
        ("Total staked (graded)", f"{mo['staked']:.0f}", f"{mn['staked']:.0f}"),
        ("Flat 1u P&L / ROI", f"{fmt(mo['flat_pnl'])} u / {fmt(mo['flat_roi'], True)}", f"{fmt(mn['flat_pnl'])} u / {fmt(mn['flat_roi'], True)}"),
        ("Quarter-Kelly P&L / ROI", f"{fmt(mo['qk_pnl'])} / {fmt(mo['qk_roi'], True)}", f"{fmt(mn['qk_pnl'])} / {fmt(mn['qk_roi'], True)}"),
        ("Record", mo["record"], mn["record"]),
        ("Brier (n)", f"{mo['brier']:.4f} ({mo['n_brier']})" if mo["brier"] is not None else "–",
         f"{mn['brier']:.4f} ({mn['n_brier']})" if mn["brier"] is not None else "–"),
        ("Log-loss", f"{mo['logloss']:.4f}" if mo["logloss"] is not None else "–", f"{mn['logloss']:.4f}" if mn["logloss"] is not None else "–"),
        ("Max stake on one match", f"{mo['max_match'][1]:.0f} ({mo['max_match'][0]})", f"{mn['max_match'][1]:.0f} ({mn['max_match'][0]})"),
    ]
    L += [f"| {r[0]} | {r[1]} | {r[2]} |" for r in rows]
    L += ["", "## Old-Rules reproduction check", "",
          f"Live paper table: {len(orig)} alerts ({sum(1 for r in orig if r['result'])} graded). "
          f"Old-Rules replay: {len(old.alerts)} alerts.",
          f"* In live table but not reproduced: {len(only_orig)}" + ("" if not only_orig else " – " + "; ".join(f"{e.split(':',1)[1]} {m}' {mk.split('_')[1].lower()} {ln:g}" for e, m, mk, ln in only_orig)),
          f"* Reproduced but not in live table: {len(only_rep)}" + ("" if not only_rep else " – " + "; ".join(f"{e.split(':',1)[1]} {m}' {mk.split('_')[1].lower()} {ln:g}" for e, m, mk, ln in only_rep)), ""]
    L += ["## Old alerts removed by the new rules (first blocking rule in the v3 pipeline)", "",
          "| rule | old alerts |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in attr.most_common()]
    L += ["", "Attribution = the reason the v3 pipeline rejected that exact line at that exact state (trace from "
          "FreeSignalEngine.last_trace). 'model repricing' = the line was still a candidate under the old model but "
          "the v3 model (shrinkage / dampener / chasing-corner penalty) moved it out of range or to negative "
          "reference EV (incl. the 2-goal-lead dampener and fav_chasing changes; not split further).", ""]
    L += ["## New-rules alerts", "",
          "| match | min | score | corners | line | fair | min odds | stake | log-growth | FT | result | P&L u | P&L | FotMob | triggers |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for x in new.alerts:
        mk = "Corners" if x["market"] == "TOTAL_CORNERS" else "Goals"
        L.append(f"| {x['home']} v {x['away']} | {int(x['minute'])} | {x['score']} | {x['corners']} | {x['selection'].title()} {x['line']:g} {mk} |"
                 f" {x['fair']:.2f} | {x['min_odds']:.2f} | {x['stake']:.0f} | {x['growth'] if x['growth'] is not None else '–'} | {x.get('ft', '')} |"
                 f" {x.get('result') or 'open'} | {'' if x.get('result') is None else f'{x['pnl_u']:+.2f}'} |"
                 f" {'' if x.get('result') is None else f'{x['pnl_m']:+.2f}'} | {x['fotmob']} | {', '.join(x['triggers'] or [])} |")
    L += ["", "## Old-rules replay alerts and their fate under v3", "",
          "| match | min | score | line | min odds | stake | result | P&L | v3 fate |", "|---|---|---|---|---|---|---|---|---|"]
    for i, x in enumerate(old.alerts):
        mk = "Corners" if x["market"] == "TOTAL_CORNERS" else "Goals"
        L.append(f"| {x['home']} v {x['away']} | {int(x['minute'])} | {x['score']} | {x['selection'].title()} {x['line']:g} {mk} |"
                 f" {x['min_odds']:.2f} | {x['stake']:.0f} | {x.get('result') or 'open'} |"
                 f" {'' if x.get('result') is None else f'{x['pnl_m']:+.2f}'} | {per.get(i)} |")
    out = Path(a.out) if a.out else dd / f"report_{a.date}_v3.md"
    out.write_text("\n".join(L) + "\n")
    print(json.dumps({"old": {k: v for k, v in mo.items()}, "new": {k: v for k, v in mn.items()},
                      "attr": attr, "only_orig": len(only_orig), "only_rep": len(only_rep)}, default=str, indent=1))


if __name__ == "__main__":
    main()
