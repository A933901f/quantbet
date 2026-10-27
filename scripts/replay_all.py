#!/usr/bin/env python
"""One-command replay/grading of several rule sets on today's logged live states.

    python scripts/replay_all.py                       # afternoon + evening + combined, Old/v4/v4.1/v4.2
    python scripts/replay_all.py --session evening     # 22:30 grading: evening log only (targets = out-of-sample)
    python scripts/replay_all.py --versions v4 v4.1 v4.2 --session evening --out dryrun/x.md

One continuous engine per rule set over afternoon -> evening states (so caps carry over for continuation matches);
legacy keeps the 12:58 restart. Alerts are graded from the latest cached ESPN scoreboard
(data/dryrun_http.sqlite3, written by the running dry run); unfinished matches stay open. No network calls.
Out-of-sample status per evening match is derived from each version's frozen_at (config/engine_b_params.json,
incl. ``superseded``) vs the match's first logged in-play state.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from replay_rules import ROOT, load_log, load_meta, load_priors, original_rows
from replay_v41 import LABEL, ReplayV41, attribute, f2, lift_label, metrics41, pct, ROWS
from replay_v4 import _p

from quantbet.config import Settings
from quantbet.frozen import read_frozen
from quantbet.models import MarketType, Selection

NAMES = {"legacy": "Old", "v3": "v3", "v4": "v4", "v4.1": "v4.1", "v4.2": "v4.2"}


def ts(s: str | None) -> float | None:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() if s else None


def hhmm(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M")


def table(R, versions, pred) -> list[str]:
    M = {v: metrics41([a for a in R[v].alerts if pred(a)]) for v in versions}
    L = ["| metric | " + " | ".join(NAMES[v] for v in versions) + " |", "|---" * (len(versions) + 1) + "|"]
    return L + [f"| {lab} | " + " | ".join(fn(M[v]) for v in versions) + " |" for lab, fn in ROWS]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-10-25")
    ap.add_argument("--versions", nargs="+", default=["legacy", "v4", "v4.1", "v4.2"])
    ap.add_argument("--focus", default=None, help="version whose alert list / removals are shown (default: last)")
    ap.add_argument("--session", choices=("all", "afternoon", "evening"), default="all")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    focus = a.focus or a.versions[-1]
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    dd = ROOT / "dryrun"
    st_a, fm_a = load_log(dd / "run_attempt2.log", "attempt2")
    st_b, fm_b = load_log(dd / "run.log", "run")
    st_e, fm_e = load_log(dd / f"run_{a.date}_evening.log", "evening")
    aft = sorted(st_a + st_b, key=lambda d: d["_t"])
    eve = sorted(st_e, key=lambda d: d["_t"])
    fotmob: dict[str, list[dict]] = defaultdict(list)
    for e in fm_a + fm_b + fm_e:
        fotmob[e["event"]].append(e)
    meta = load_meta(ROOT / "data/dryrun_http.sqlite3", list(s.espn_leagues))
    priors = load_priors(ROOT / "data/espn_summary.sqlite3")
    aft_events = {d["event"] for d in aft}
    R: dict[str, ReplayV41] = {}
    for v in a.versions:
        r = ReplayV41(v, s, meta, priors, fotmob)
        r.run(aft + eve, True)
        r.grade()
        for x in r.alerts:
            x["target"] = x["section"] == "evening" and x["event"] not in aft_events
        R[v] = r
    fr = read_frozen(ROOT / s.engine_b_params_path) or {}
    frozen = {x["rules_version"]: x for x in (fr.get("superseded") or [])}
    frozen[fr.get("rules_version")] = {"sha256": fr.get("sha256"), "frozen_at": fr.get("frozen_at")}
    ev_by: dict[str, list[dict]] = defaultdict(list)
    for d in eve:
        ev_by[d["event"]].append(d)

    L = [f"# Rules replay {a.date} – {' vs '.join(NAMES[v] for v in a.versions)} (session: {a.session})", "",
         f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC from logged states only (no network, nothing sent). "
         f"Afternoon {len(aft)} states (in-sample); evening log {len(eve)} states up to "
         f"{hhmm(eve[-1]['_t']) if eve else '–'} UTC.", "", "## Frozen parameter sets", "",
         "| version | sha256 | frozen_at | replayed hash matches |", "|---|---|---|---|"]
    for v in a.versions:
        f = frozen.get(v)
        L.append(f"| {NAMES[v]} | `{R[v].hash}` | {f['frozen_at'] if f else 'not frozen'} | "
                 f"{'yes' if f and f['sha256'] == R[v].hash else ('–' if not f else 'NO')} |")
    import json as _json
    cal = (_json.loads((ROOT / "config/calibration_report.json").read_text()).get("v4_2")
           if (ROOT / "config/calibration_report.json").exists() else None)
    if cal and "v4.2" in a.versions:
        fd, sb = cal["football_data"], cal["statsbomb"]
        L += ["", "## v4.2 calibration (config/calibration_report.json → v4_2)", "",
              f"* Football-Data CSVs ({len(fd['files'])} files, {fd['matches']} matches): corner columns {fd['corner_columns']} "
              f"only – **no half-time corners** (has_half_time_corners={fd['has_half_time_corners']}).",
              f"* Corner H2/H1 per-minute rate ratio **{sb['h2_h1_per_minute_ratio']}** (90% CI {sb['h2_h1_ci90']}), "
              f"H2 share of corners {sb['h2_share_of_corners']:.1%}; {sb['source']}, {sb['matches']} matches "
              f"({sb['h1_corners']} H1 / {sb['h2_corners']} H2 corners, minutes incl. stoppage).",
              f"* Goals 2-goal-lead dampener fit (HT |diff| >= 2, n={fd['n_lead_matches']}): observed/expected 2nd-half goals "
              f"{fd['ht_diff_ge2']['obs_over_exp_h2']} vs {fd['all_obs_over_exp_h2']} for all matches → **{fd['goal_lead_dampener']}** "
              f"(90% CI {fd['goal_lead_dampener_ci90']}); expected = {fd['lambda_pre_source']} × 2nd-half share "
              f"{fd['second_half_goal_share']}. No dampening found → v4.2 uses **1.00** (capped: shields only).",
              f"* Corner lead dampener (both teams, StatsBomb, {sb['n_lead_matches']} matches with a 2-goal lead in H2, "
              f"{sb['lead_minutes']:.0f} min): {sb['corner_lead_dampener_raw']} (90% CI {sb['corner_lead_dampener_ci90']}) → 0.952; "
              "indicative (baseline = each match's own corners, biased toward 1).",
              "* v4.2 corner shrinkage: observed pace vs half-adjusted *expected* corners so far (profile-weighted minutes), "
              "Gamma-Poisson weight eff/(eff+180); before 55' clamped at >= prior (may only raise). Prior benchmark uses the "
              "same half profile (normalised: mean weight 1 over 90', total prior unchanged). Unders disabled."]
    L += ["", "## Caveats", "",
          "* Afternoon is in-sample for v4/v4.1/v4.2 (all designed after seeing it); v4.2's afternoon result is a fit, not evidence.",
          "* Tiny samples (v4.2: a handful of bets); ROI/BSS are noise-dominated. Min-odds fill assumed; no bookmaker odds seen.",
          "* Afternoon gaps (box paused) 13:45–14:19, 14:23–14:35, 14:35–16:46, 16:54–17:10 UTC.",
          "* Lead dampeners are fitted at half time and applied at any minute with |diff| >= 2."]
    L += ["", "## Evening matches: sample status per version", "",
          "Out-of-sample = first logged in-play state after that version's frozen_at (continuation matches of the "
          "afternoon were in play before any freeze, so they are at best quasi-out-of-sample).", "",
          "| match | kind | states | first–last state UTC | at cut | final | " + " | ".join(NAMES[v] for v in a.versions if v != "legacy") + " |",
          "|---" * (6 + len([v for v in a.versions if v != "legacy"])) + "|"]
    for k, sts in ev_by.items():
        m = meta.get(k)
        kind = "continuation" if k in aft_events else "evening target"
        fin = f"{m.final['home_goals']}-{m.final['away_goals']} (corners {m.final.get('home_corners')}-{m.final.get('away_corners')})" if m and m.final else "not final"
        first = min(d["_t"] for d in (sts + [d for d in aft if d["event"] == k]))
        cells = []
        for v in a.versions:
            if v == "legacy":
                continue
            fa = ts((frozen.get(v) or {}).get("frozen_at"))
            cells.append("in-sample (no freeze)" if fa is None else "out-of-sample" if first > fa else "quasi / in-sample")
        L.append(f"| {m.home if m else k} v {m.away if m else ''} | {kind} | {len(sts)} | {hhmm(sts[0]['_t'])}–{hhmm(sts[-1]['_t'])} | "
                 f"{sts[-1]['minute']:.0f}' {sts[-1]['score']} | {fin} | " + " | ".join(cells) + " |")
    for tgt in ("20261025:udinese:milan", "20261025:barcelona:real_madrid"):
        if a.date == "2026-10-25" and tgt not in ev_by:
            m = meta.get(tgt)
            fin = (f"{m.final['home_goals']}-{m.final['away_goals']} (corners {m.final.get('home_corners')}-{m.final.get('away_corners')})"
                   if m and m.final else "not final")
            name = f"{m.home} v {m.away}" if m else tgt.split(':', 1)[1].replace(':', ' v ')
            L.append(f"| {name} | evening target | 0 | – | no logged in-play state | {fin} | "
                     + " | ".join("– (no states)" for v in a.versions if v != "legacy") + " |")
    secs = []
    if a.session in ("all", "afternoon"):
        secs.append(("Afternoon (in-sample)", lambda x: x["section"] == "afternoon"))
    if a.session in ("all", "evening"):
        secs += [("Evening log – all", lambda x: x["section"] == "evening"),
                 ("Evening targets only (Udinese v Milan, Barcelona v Real Madrid; out-of-sample for v4/v4.1/v4.2)",
                  lambda x: x.get("target")),
                 ("Evening continuation matches (quasi-out-of-sample)", lambda x: x["section"] == "evening" and not x.get("target"))]
    if a.session == "all":
        secs.append(("Combined", lambda x: True))
    in_sess = (lambda x: True) if a.session == "all" else (lambda x: x["section"] == a.session)
    for title, pred in secs:
        L += ["", f"## {title}", ""] + table(R, a.versions, pred)
    L += ["", f"## {NAMES[focus]} by direction ({a.session})", "",
          "| selection | alerts | graded | record | staked | QK P&L / ROI | flat ROI | BSS | LL skill |", "|---" * 9 + "|"]
    for v in a.versions:
        for sel in ("over", "under"):
            m = metrics41([x for x in R[v].alerts if x["selection"] == sel and in_sess(x)])
            if m["alerts"]:
                L.append(f"| {NAMES[v]} {sel} | {m['alerts']} | {m['graded']} | {m['record']} | {m['staked']:.0f} | "
                         f"{f2(m['qk_pnl'], 2, True)} / {pct(m['qk_roi'])} | {pct(m['flat_roi'])} | {f2(m['bss'], 3, True)} | {f2(m['ll_skill'], 3, True)} |")
    L += ["", f"## Alerts removed by {NAMES[focus]} (first blocking rule; all sessions)", ""]
    for v in a.versions:
        if v == focus:
            continue
        L += [f"**{NAMES[v]} → {NAMES[focus]}**", "", "| rule | alerts |", "|---|---|"]
        L += [f"| {k.replace('v4.1 lift', 'lift')} | {n} |" for k, n in attribute(R[v], R[focus]).most_common()]
        L.append("")
    L += [f"## {NAMES[focus]} alerts ({a.session})", "",
          "| section | match | min | score | corners | line | prior fair | fair | lift % | net mult | λ model / prior | min odds "
          "| stake | FT | result | P&L u | P&L |", "|---" * 17 + "|"]
    for x in R[focus].alerts:
        if not in_sess(x):
            continue
        mk = "Corners" if x["market"] == "TOTAL_CORNERS" else "Goals"
        lf = x.get("lift") or {}
        res = x.get("result")
        sec = x["section"] + (" (target)" if x.get("target") else "")
        L.append(f"| {sec} | {x['home']} v {x['away']} | {int(x['minute'])} | {x['score']} | {x['corners']} | "
                 f"{x['selection'].title()} {x['line']:g} {mk} | {f2(lf.get('prior_fair'), 2)} | {x['fair']:.2f} | "
                 f"{pct(lf.get('lift'))} | {f2(lf.get('net_multiplier'), 3)} | {f2(lf.get('lambda_model'), 2)} / "
                 f"{f2(lf.get('lambda_prior'), 2)} | {x['min_odds']:.2f} | {x['stake']:.0f} | {x.get('ft', '')} | {res or 'open'} | "
                 f"{'' if res is None else format(x['pnl_u'], '+.2f')} | {'' if res is None else format(x['pnl_m'], '+.2f')} |")
    for v in a.versions:
        if v == focus:
            continue
        rows = [x for x in R[v].alerts if in_sess(x)]
        if not rows:
            continue
        keys = {(y["event"], y["t"], y["market"], y["line"], y["selection"]) for y in R[focus].alerts}
        L += ["", f"## {NAMES[v]} alerts and their {NAMES[focus]} fate ({a.session})", "",
              "| section | match | min | score | line | fair | prior 1/p | result | " + NAMES[focus] + " |", "|---" * 9 + "|"]
        for x in rows:
            mk = "Corners" if x["market"] == "TOTAL_CORNERS" else "Goals"
            k = (x["event"], x["t"], x["market"], x["line"], x["selection"])
            tr = R[focus].traces.get((x["event"], x["t"]), {}).get((MarketType(x["market"]), x["line"], Selection(x["selection"])))
            fate = "kept" if k in keys else (lift_label(tr).replace("v4.1 lift", "lift") if tr else "not priced")
            L.append(f"| {x['section']} | {x['home']} v {x['away']} | {int(x['minute'])} | {x['score']} | {x['selection'].title()} "
                     f"{x['line']:g} {mk} | {x['fair']:.2f} | {1 / _p(x['prior_settlement']):.2f} | {x.get('result') or 'open'} | "
                     f"{LABEL.get(fate, fate)} |")
    L += ["", "Notes: min-odds fill with first-tier quarter-Kelly stake; prior-only benchmark = same state, time-decayed "
          "pre-match lambdas, no multipliers/shrinkage (v4.2: with the calibrated corner half profile); "
          f"live paper table rows: afternoon {len(original_rows(dd / 'paper_trades.sqlite3'))}"
          + (f", evening {len(original_rows(dd / f'paper_trades_{a.date}_evening.sqlite3'))}"
             if (dd / f"paper_trades_{a.date}_evening.sqlite3").exists() else "") + " (run with v3 rules)."]
    out = Path(a.out) if a.out else dd / (f"report_{a.date}_evening_grading.md" if a.session == "evening"
                                          else f"report_{a.date}_{'_'.join(v.replace('.', '_') for v in a.versions[-1:])}.md")
    out.write_text("\n".join(L) + "\n")
    print("wrote", out)
    for v in a.versions:
        for title, pred in secs:
            m = metrics41([x for x in R[v].alerts if pred(x)])
            print(f"{v:7s} {title[:22]:22s} n={m['alerts']:3d} O/U={m['overs']}/{m['unders']} graded={m['graded']} "
                  f"qk={m['qk_pnl']:+.2f} roi={pct(m['qk_roi'])} bss={f2(m['bss'], 3, True)} ll={f2(m['ll_skill'], 3, True)}")


if __name__ == "__main__":
    main()
