"""Replay Old (legacy) vs v3 vs v4 vs v4.1 on today's logged live states, afternoon (in-sample) and the
evening log (continuations of afternoon matches + the evening target matches) separately and combined.
Writes dryrun/report_<date>_v4_1.md. No network calls; nothing is sent.

    python scripts/replay_v41.py --date 2026-10-25

One continuous engine per rule set (afternoon then evening states, chronologically); legacy keeps the
12:58 restart of the afternoon run. Alerts are assigned to a section by the log they came from.
Grading uses the latest cached ESPN scoreboard (data/dryrun_http.sqlite3); unfinished matches stay open.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import replay_v4 as r4
from replay_rules import ROOT, build_state, load_log, load_meta, load_priors, original_rows
from replay_v4 import ReplayV, _p, f2, metrics_v4

from quantbet.config import Settings
from quantbet.frozen import read_frozen
from quantbet.models import MarketType, Selection

VERSIONS = ("legacy", "v3", "v4", "v4.1")
NAMES = {"legacy": "Old", "v3": "v3", "v4": "v4", "v4.1": "v4.1"}
LABEL = dict(r4.LABEL, **{
    "not_best": "single line per family (fair closest to 1.95)",
    "fair_target_range": "fair-odds window 1.75-2.20 (no line in range)",
    "blowout": "blowout block (|diff| >= 3: no Overs)",
})


def lift_label(reason: str) -> str:
    if "Overs suppressed" in reason:
        return "v4.1 lift: net multiplier <= 1.05 (all Overs of family suppressed)"
    if "net multiplier < " in reason:
        return "v4.1 lift: net multiplier 1.05-1.06"
    if "Over line lift" in reason:
        return "v4.1 lift: Over line lift < 6% vs prior"
    if "Under needs" in reason:
        return "v4.1 lift: Under needs lambda_model < lambda_prior"
    if "Under line lift" in reason:
        return "v4.1 lift: Under line lift < 6% vs prior"
    if "Unders disabled" in reason:
        return "v4.2: Unders disabled"
    return reason


class ReplayV41(ReplayV):
    def run(self, states: list[dict], restart_between_segments: bool) -> None:  # noqa: ARG002
        seg = None
        for d in states:
            if self.legacy and seg == "attempt2" and d["_seg"] != seg:
                self.reset()  # the afternoon live run restarted at 12:58:30 (fresh limiter)
            seg = d["_seg"]
            self.cur_src[d["event"]] = d.get("ratings_src") or ""
            st = build_state(d, self.meta.get(d["event"]))
            pre_gate = self.legacy and d["_seg"] == "attempt2"
            sigs = self.free.on_match_state(st, now=d["_t"])
            self.traces[(d["event"], d["_t"])] = dict(self.free.last_trace.get(d["event"], {}))
            sigs = self.confirm(sigs, st, d["_t"], pre_gate)
            for sg in sigs:
                self.free.commit(sg)
            ratings = self.free.engine.ratings.get(d["event"])
            for sg in self.risk.size_all(sigs):
                x = sg.extra
                tiers = x.get("tiers") or []
                sp = self.prior_model.predict(st, ratings).settlement(sg.market, sg.selection, sg.line)
                self.alerts.append({
                    "t": d["_t"], "section": "evening" if d["_seg"] == "evening" else "afternoon",
                    "event": sg.event_key, "home": sg.home, "away": sg.away, "minute": sg.minute,
                    "score": sg.score, "corners": f"{x.get('home_corners')}-{x.get('away_corners')}",
                    "market": sg.market.value, "line": sg.line, "selection": sg.selection.value,
                    "fair": sg.fair_odds, "min_odds": x.get("min_odds"), "stake": float(tiers[0][1]) if tiers else 0.0,
                    "settlement": x.get("settlement"), "triggers": x.get("triggers"), "fotmob": x.get("fotmob"),
                    "ev_ref": x.get("ev_at_reference"), "growth": x.get("log_growth"), "notes": x.get("notes"),
                    "ratings_src": x.get("ratings_source"), "lift": x.get("lift"),
                    "prior_settlement": [sp.win, sp.half_win, sp.push, sp.half_loss, sp.loss],
                })


def attribute(src: ReplayV, dst: ReplayV) -> Counter:
    cnt: Counter = Counter()
    keys = {(a["event"], a["t"], a["market"], a["line"], a["selection"]) for a in dst.alerts}
    for a in src.alerts:
        k = (a["event"], a["t"], a["market"], a["line"], a["selection"])
        if k in keys:
            why = "kept (same line)"
        else:
            tr = dst.traces.get((a["event"], a["t"]), {})
            reason = tr.get((MarketType(a["market"]), a["line"], Selection(a["selection"])))
            drop = dst.fotmob_drops.get(k[:4])
            if reason is None:
                why = "repricing: line no longer priced"
            elif reason.startswith("gate:"):
                why = ("minute gate (30-43 / 55-85)" if "outside windows" in reason else
                       "no trigger" if "no trigger" in reason else "gate: " + reason[5:].strip())
            elif reason.startswith("lift:"):
                why = lift_label(reason)
            elif reason == "emitted" and drop:
                why = LABEL.get(drop, drop)
            elif reason == "emitted":
                why = "emitted but dropped downstream"
            else:
                why = LABEL.get(reason, reason)
        cnt[why] += 1
    return cnt


def metrics41(alerts: list[dict]) -> dict[str, Any]:
    m = metrics_v4(alerts)
    m["overs"] = sum(a["selection"] == "over" for a in alerts)
    m["unders"] = sum(a["selection"] == "under" for a in alerts)
    if not alerts:
        m["max_match"] = ("–", 0.0)
    return m


ROWS = [
    ("Alerts", lambda m: str(m["alerts"])),
    ("Overs / Unders", lambda m: f"{m['overs']} / {m['unders']}"),
    ("Graded / open", lambda m: f"{m['graded']} / {m['open']}"),
    ("Matches with alerts", lambda m: str(m["matches"])),
    ("Staked (graded)", lambda m: f"{m['staked']:.0f}"),
    ("Flat 1u P&L / ROI", lambda m: f"{f2(m['flat_pnl'], 2, True)} u / {pct(m['flat_roi'])}"),
    ("Quarter-Kelly P&L / ROI", lambda m: f"{f2(m['qk_pnl'], 2, True)} / {pct(m['qk_roi'])}"),
    ("Record", lambda m: m["record"]),
    ("Brier model (n)", lambda m: f"{f2(m['brier'])} ({m['n_brier']})"),
    ("Brier prior-only", lambda m: f2(m["brier_prior"])),
    ("BSS = 1 − Brier/Brier_prior", lambda m: f2(m["bss"], 3, True)),
    ("Log-loss model", lambda m: f2(m["logloss"])),
    ("Log-loss prior-only", lambda m: f2(m["ll_prior"])),
    ("LL skill = 1 − LL/LL_prior", lambda m: f2(m["ll_skill"], 3, True)),
    ("Max stake on one match", lambda m: f"{m['max_match'][1]:.0f}" + (f" ({m['max_match'][0].split(':', 1)[-1]})"
                                                                        if m["max_match"][1] else "")),
    ("Avg fair odds of chosen lines (all / graded)", lambda m: f"{f2(m['avg_fair'], 2)} / {f2(m['avg_fair_graded'], 2)}"),
]


def pct(x: float | None) -> str:
    return "–" if x is None else f"{100 * x:+.1f}%"


def table(R: dict[str, ReplayV], section: str | None) -> list[str]:
    M = {v: metrics41([a for a in R[v].alerts if section is None or a["section"] == section]) for v in R}
    L = ["| metric | " + " | ".join(NAMES[v] for v in R) + " |", "|---" * (len(R) + 1) + "|"]
    L += [f"| {lab} | " + " | ".join(fn(M[v]) for v in R) + " |" for lab, fn in ROWS]
    return L


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-10-25")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    dd = ROOT / "dryrun"
    st_a, fm_a = load_log(dd / "run_attempt2.log", "attempt2")
    st_b, fm_b = load_log(dd / "run.log", "run")
    st_e, fm_e = load_log(dd / f"run_{a.date}_evening.log", "evening")
    aft = sorted(st_a + st_b, key=lambda d: d["_t"])
    eve = sorted(st_e, key=lambda d: d["_t"])
    states = aft + eve
    fotmob: dict[str, list[dict]] = defaultdict(list)
    for e in fm_a + fm_b + fm_e:
        fotmob[e["event"]].append(e)
    meta = load_meta(ROOT / "data/dryrun_http.sqlite3", list(s.espn_leagues))
    priors = load_priors(ROOT / "data/espn_summary.sqlite3")
    generated = datetime.now(timezone.utc)

    R: dict[str, ReplayV41] = {}
    for v in VERSIONS:
        r = ReplayV41(v, s, meta, priors, fotmob)
        r.run(states, True)
        r.grade()
        R[v] = r
    fr = read_frozen(ROOT / s.engine_b_params_path) or {}
    h41, h4 = R["v4.1"].hash, R["v4"].hash
    sup = [x for x in fr.get("superseded") or [] if x.get("rules_version") == "v4"]
    hhmm = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M")  # noqa: E731
    ev_by: dict[str, list[dict]] = defaultdict(list)
    for d in eve:
        ev_by[d["event"]].append(d)
    aft_events = {d["event"] for d in aft}
    targets = [k for k in ev_by if k not in aft_events]
    conts = [k for k in ev_by if k in aft_events]
    gaps = [(aft[i - 1]["_t"], aft[i]["_t"]) for i in range(1, len(aft)) if aft[i]["_t"] - aft[i - 1]["_t"] > 600]
    eve_orig = original_rows(dd / f"paper_trades_{a.date}_evening.sqlite3") if (dd / f"paper_trades_{a.date}_evening.sqlite3").exists() else []
    aft_orig = original_rows(dd / "paper_trades.sqlite3")

    def desc(k: str) -> str:
        m = meta.get(k)
        lst = ev_by[k][-1]
        fin = "final " + f"{m.final['home_goals']}-{m.final['away_goals']}" if (m and m.final) else "NOT finished"
        return (f"{m.home if m else k} v {m.away if m else ''}: {len(ev_by[k])} states, {hhmm(ev_by[k][0]['_t'])}–"
                f"{hhmm(lst['_t'])} UTC (minute {lst['minute']:.0f}, {lst['score']} at the cut), {fin} at generation")

    L = [f"# Rules replay v4.1 – {a.date} (Old vs v3 vs v4 vs v4.1)", "",
         f"Generated {generated:%Y-%m-%d %H:%M} UTC. No network calls; nothing sent.", "",
         "## Data and time cut", "",
         f"* **Afternoon (in-sample): {len(aft)} states**, {len(aft_events)} matches, {hhmm(aft[0]['_t'])}–{hhmm(aft[-1]['_t'])} "
         f"UTC (run_attempt2.log + run.log). Old-rules reproduction: live paper table {len(aft_orig)} alerts vs legacy "
         f"replay {sum(x['section'] == 'afternoon' for x in R['legacy'].alerts)}.",
         f"* **Evening log: {len(eve)} states** up to the cut **{hhmm(eve[-1]['_t']) if eve else '–'} UTC** "
         f"(dryrun/run_{a.date}_evening.log, live v3 dry run pid 90891 still running):"]
    L += [f"  * continuation of an afternoon match – {desc(k)}" for k in conts]
    L += [f"  * evening target – {desc(k)}" for k in targets]
    if not targets:
        L.append("  * evening targets (Udinese v AC Milan 19:45, Barcelona v Real Madrid 20:00 UTC): **no in-play states "
                 "yet at the cut** (not kicked off) – the evening target sample is empty in this report.")
    L += ["* **Sample labels.** v4.1 was designed at ~19:05–19:20 UTC. Afternoon = in-sample for v3/v4/v4.1. Evening-log "
          "states of the continuation matches were logged 18:35–19:1x, i.e. partly before the v4.1 design, and the v4 "
          "report (18:55) showed their earlier alerts, so they are **quasi-out-of-sample** at best. Lecce v Juventus had "
          "finished (0-2) before v4.1 was written, so its result was known; Real Sociedad v Levante was still in play. "
          "The evening target matches had not kicked off: anything they produce on a re-run is genuinely out-of-sample "
          "for v4.1 (rules and hash frozen before kick-off).",
          f"* One continuous engine per rule set (afternoon → evening, so caps/cooldowns carry over for the continuation "
          f"matches); legacy keeps the 12:58 restart. Evening live paper table (v3, targets only): {len(eve_orig)} alerts.",
          "", "## Hashes", "",
          f"* **v4.1 params sha256 (current freeze): `{h41}`** – config/engine_b_params.json, frozen_at "
          f"{fr.get('frozen_at', '–')}, file {'matches' if fr.get('sha256') == h41 else 'DOES NOT match'} the replayed set.",
          f"* v4 sha256 **superseded**: `{h4}`" + (f" (frozen_at {sup[0]['frozen_at']}; kept under `superseded`, still runnable "
                                                    "with `--rules v4` because its hash matches exactly)" if sup else ""),
          f"* v3 set `{R['v3'].hash[:16]}…`, legacy set `{R['legacy'].hash[:16]}…` (not frozen).",
          "", "## Findings", "",
          f"* v4.1 fires {len(R['v4.1'].alerts)} alerts ({sum(x['selection'] == 'over' for x in R['v4.1'].alerts)} Overs, "
          f"{sum(x['selection'] == 'under' for x in R['v4.1'].alerts)} Unders) vs {len(R['v4'].alerts)} for v4 (all Overs). "
          "The lift gate removes most v4 Overs (net multiplier <= 1.05), but the Under branch replaces them almost one for one.",
          "* Overs that pass the lift gate did well; the Unders lost (see 'v4.1 by direction'). Unders come from damped "
          "states: goals net ≈ 0.75 in every 2-goal-lead state (the v4 comfortable-lead dampener, goals x0.75 and both teams' corners x0.80 at |diff| >= 2) and corner net "
          "0.70–0.97 driven mostly by Gamma-Poisson shrinkage toward a slow early corner pace. The prior beat those "
          "dampening adjustments in this sample, so v4.1's BSS is below v4's.",
          f"* v3/v4 numbers differ slightly from report_{a.date}_v4.md because Lecce v Juventus has since finished and is now graded.",
          "", "## Caveats (read first)", "",
          "* **In-sample.** All of v3, v4 and v4.1 were designed after seeing the afternoon results; improvements there are "
          "expected by construction.",
          "* **Tiny sample.** A few bets on ~15 matches; ROI, Brier, BSS and LL skill are noise-dominated.",
          "* **Min-odds fill.** Every bet assumed filled at its minimum odds with the first-tier quarter-Kelly stake; no "
          "bookmaker odds were seen (reverse-pricing mode everywhere, so v4.1's lift gate applies to every line).",
          "* **Gaps.** No afternoon states in " + ", ".join(f"{hhmm(x)}–{hhmm(y)}" for x, y in gaps) + " UTC (box paused).",
          "* **FotMob** = only what was logged live; no shots-inside-box in the states, so the chasing-corner SIB exception "
          "never fires (1.03 cap always applies).",
          "* **Prior-only benchmark** = same engine and state (score, corners, clock, stoppage), time-decayed pre-match "
          "lambdas, no multipliers, no shrinkage; each rule set is scored on its own chosen lines. For v4.1 the lift gate "
          "*selects* lines where model and prior disagree by ≥ 6 %, so BSS/LL skill there measure exactly that bet: whether "
          "the in-play adjustment beat the prior.",
          "* **net_multiplier** = λ_model_remaining / λ_prior_remaining per family (goals: home+away; corners: total μ "
          "incl. Gamma-Poisson shrinkage of the observed corner pace and the chasing cap). Unders allowed in blowouts "
          "(|diff| ≥ 3 blocks Overs only).",
          ]
    for sec, title in (("afternoon", "Afternoon (in-sample, 539 states)" if len(aft) == 539 else f"Afternoon ({len(aft)} states)"),
                       ("evening", "Evening log (quasi-out-of-sample, see above)"), (None, "Combined")):
        L += ["", f"## {title}", ""] + table(R, sec)
    L += ["", "## Alerts removed by v4.1 (first blocking rule in the v4.1 pipeline, all sections)", ""]
    for v in ("legacy", "v3", "v4"):
        L += [f"**{NAMES[v]} → v4.1**", "", "| rule | alerts |", "|---|---|"]
        L += [f"| {k} | {n} |" for k, n in attribute(R[v], R["v4.1"]).most_common()]
        L.append("")
    L += ["## v4.1 alerts", "",
          "| section | match | min | score | corners | line | prior fair | fair | lift % | net mult | λ model / prior | min odds "
          "| stake | FT | result | P&L u | P&L | FotMob |",
          "|---" * 18 + "|"]
    for x in R["v4.1"].alerts:
        mk = "Corners" if x["market"] == "TOTAL_CORNERS" else "Goals"
        lf = x.get("lift") or {}
        res = x.get("result")
        L.append(f"| {x['section']} | {x['home']} v {x['away']} | {int(x['minute'])} | {x['score']} | {x['corners']} | "
                 f"{x['selection'].title()} {x['line']:g} {mk} | {f2(lf.get('prior_fair'), 2)} | {x['fair']:.2f} | "
                 f"{pct(lf.get('lift'))} | {f2(lf.get('net_multiplier'), 3)} | {f2(lf.get('lambda_model'), 2)} / "
                 f"{f2(lf.get('lambda_prior'), 2)} | {x['min_odds']:.2f} | {x['stake']:.0f} | {x.get('ft', '')} | {res or 'open'} | "
                 f"{'' if res is None else f'{x['pnl_u']:+.2f}'} | {'' if res is None else f'{x['pnl_m']:+.2f}'} | {x['fotmob']} |")
    L += ["", "## v4.1 by direction (combined)", "", "| selection | alerts | graded | record | staked | QK P&L / ROI | flat ROI | BSS | LL skill |",
          "|---|---|---|---|---|---|---|---|---|"]
    for sel in ("over", "under"):
        for fam, mk in (("all", None), ("goals", "TOTAL_GOALS"), ("corners", "TOTAL_CORNERS")):
            m = metrics41([x for x in R["v4.1"].alerts if x["selection"] == sel and (mk is None or x["market"] == mk)])
            L.append(f"| {sel.title()} {'' if fam == 'all' else fam} | {m['alerts']} | {m['graded']} | {m['record']} | {m['staked']:.0f} | "
                     f"{f2(m['qk_pnl'], 2, True)} / {pct(m['qk_roi'])} | {pct(m['flat_roi'])} | {f2(m['bss'], 3, True)} | {f2(m['ll_skill'], 3, True)} |")
    L += ["", "## v4 alerts and their v4.1 fate", "",
          "Prior column here = 1/p_prior with p = (W+HW)/(1−P) on the v4 line (the Brier definition), so it differs from the "
          "fair-odds definition used by the lift gate on quarter lines.", "",
          "| section | match | min | score | line | v4 fair | prior 1/p | result | v4.1 |",
          "|---|---|---|---|---|---|---|---|---|"]
    keys41 = {(a["event"], a["t"], a["market"], a["line"], a["selection"]) for a in R["v4.1"].alerts}
    for x in R["v4"].alerts:
        mk = "Corners" if x["market"] == "TOTAL_CORNERS" else "Goals"
        k = (x["event"], x["t"], x["market"], x["line"], x["selection"])
        tr = R["v4.1"].traces.get((x["event"], x["t"]), {}).get((MarketType(x["market"]), x["line"], Selection(x["selection"])))
        fate = "kept" if k in keys41 else lift_label(tr) if tr else "not priced"
        res = x.get("result")
        L.append(f"| {x['section']} | {x['home']} v {x['away']} | {int(x['minute'])} | {x['score']} | {x['selection'].title()} "
                 f"{x['line']:g} {mk} | {x['fair']:.2f} | {1 / _p(x['prior_settlement']):.2f} | {res or 'open'} | {LABEL.get(fate, fate)} |")
    out = Path(a.out) if a.out else dd / f"report_{a.date}_v4_1.md"
    out.write_text("\n".join(L) + "\n")
    summ = {v: {sec or "all": metrics41([x for x in R[v].alerts if sec is None or x["section"] == sec])
                for sec in ("afternoon", "evening", None)} for v in R}
    print(json.dumps({"aft": len(aft), "eve": len(eve), "targets": targets, "conts": conts, "h41": h41, "h4": h4,
                      "summ": summ}, default=str, indent=1))


if __name__ == "__main__":
    main()
