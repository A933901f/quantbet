"""Replay today's logged (afternoon) live states under Old (legacy), v3 and v4 rules; grade, score and
compare against a pre-match-prior-only benchmark (BSS, log-loss skill). Writes dryrun/report_<date>_v4.md.

    python scripts/replay_v4.py --date 2026-10-25

Inputs are the same as scripts/replay_rules.py (dryrun/run_attempt2.log + dryrun/run.log). The evening
run (dryrun/run_<date>_evening.log) is deliberately NOT replayed: it is the out-of-sample test.
No network calls; nothing is sent.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import replay_rules as rr  # scripts/ is on sys.path when run as a script
from replay_rules import ROOT, Meta, Replay, build_state, load_log, load_meta, load_priors, metrics, original_rows

from quantbet.config import Settings
from quantbet.engine import LiveFootballModel, SignalEngine, FreeSignalEngine, RatingsResolver, rules_preset
from quantbet.football_data import load_ratings
from quantbet.frozen import param_set, params_hash, read_frozen
from quantbet.main import build_engine_config, build_risk, load_live_params
from quantbet.models import MarketType, Selection


class ReplayV(Replay):
    def __init__(self, version: str, s: Settings, meta: dict[str, Meta], priors: dict[str, Any],
                 fotmob: dict[str, list[dict]]) -> None:
        self.version = version
        super().__init__(version, version == "legacy", s, meta, priors, fotmob)

    def reset(self) -> None:
        s = self.s
        kw = dict(target_edge=s.target_edge, abort_edge=s.abort_edge, kelly_multiplier=s.kelly_multiplier,
                  min_minute=s.min_alert_minute, max_minute=s.max_alert_minute, realert_cooldown_s=s.realert_cooldown_s,
                  bankroll=s.bankroll, max_match_stake_frac=s.max_match_stake_frac, family_cooldown_min=s.family_cooldown_min)
        pricing, lp = rules_preset(self.version, load_live_params(ROOT / s.engine_b_params_path), **kw)
        self.pricing, self.params = pricing, lp
        self.hash = params_hash(param_set(self.version, pricing, lp))
        self.prior_model = LiveFootballModel(dataclasses.replace(lp, prior_only=True))

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

    def run(self, states: list[dict], restart_between_segments: bool) -> None:
        seg = None
        for d in states:
            if restart_between_segments and seg is not None and d["_seg"] != seg:
                self.reset()
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
                    "t": d["_t"], "event": sg.event_key, "home": sg.home, "away": sg.away, "minute": sg.minute,
                    "score": sg.score, "corners": f"{x.get('home_corners')}-{x.get('away_corners')}",
                    "market": sg.market.value, "line": sg.line, "selection": sg.selection.value,
                    "fair": sg.fair_odds, "min_odds": x.get("min_odds"), "stake": float(tiers[0][1]) if tiers else 0.0,
                    "settlement": x.get("settlement"), "triggers": x.get("triggers"), "fotmob": x.get("fotmob"),
                    "ev_ref": x.get("ev_at_reference"), "growth": x.get("log_growth"), "notes": x.get("notes"),
                    "ratings_src": x.get("ratings_source"),
                    "prior_settlement": [sp.win, sp.half_win, sp.push, sp.half_loss, sp.loss],
                })


def _p(settle: list[float]) -> float:
    w, hw, p, _hl, _l = settle
    return min(max((w + hw) / max(1e-9, 1 - p), 1e-6), 1 - 1e-6)


def metrics_v4(alerts: list[dict]) -> dict[str, Any]:
    m = metrics(alerts)
    bm = bp = lm = lpr = 0.0
    n = 0
    for a in alerts:
        if not a.get("result") or a["result"] == "push" or not a["settlement"]:
            continue
        y = 1.0 if a["result"] in ("win", "half-win") else 0.0
        pm, pp = _p(a["settlement"]), _p(a["prior_settlement"])
        bm += (pm - y) ** 2
        bp += (pp - y) ** 2
        lm += -(y * math.log(pm) + (1 - y) * math.log(1 - pm))
        lpr += -(y * math.log(pp) + (1 - y) * math.log(1 - pp))
        n += 1
    m["brier_prior"] = bp / n if n else None
    m["ll_prior"] = lpr / n if n else None
    m["bss"] = (1 - bm / bp) if n and bp > 0 else None
    m["ll_skill"] = (1 - lm / lpr) if n and lpr > 0 else None
    m["avg_fair"] = sum(a["fair"] for a in alerts) / len(alerts) if alerts else None
    m["avg_fair_graded"] = (sum(a["fair"] for a in alerts if a.get("result")) / m["graded"]) if m["graded"] else None
    return m


LABEL = {
    "not_best": "single line per family (v4: fair closest to 1.95)",
    "fair_target_range": "v4 fair-odds window 1.75-2.20 (no line in range)",
    "blowout": "v4 blowout block (|diff| >= 3: no Overs)",
    "cooldown": "family cooldown (15')",
    "family_cap": "hard cap 1 goals + 1 corners per match",
    "match_signal_cap": "match cap (2 signals)",
    "stake_cap": "match stake cap (5%)",
    "fotmob_corner_75": "FotMob dominance for corner Over >75'",
    "fotmob_contradicted": "FotMob xG/SIB contradicted",
    "neg_ref_ev": "negative EV vs reference model",
    "direction": "direction gate",
    "fair_range": "repricing: fair outside alert range",
    "no_tiers": "repricing: no positive stake tier",
    "rate_limited": "rate limiter",
}


def attribute_v(src: Replay, dst: Replay) -> tuple[Counter, dict[int, str]]:
    cnt: Counter = Counter()
    per: dict[int, str] = {}
    keys = {(a["event"], a["t"], a["market"], a["line"]) for a in dst.alerts}
    for i, a in enumerate(src.alerts):
        k = (a["event"], a["t"], a["market"], a["line"])
        if k in keys:
            why = "kept"
        else:
            tr = dst.traces.get((a["event"], a["t"]), {})
            reason = tr.get((MarketType(a["market"]), a["line"], Selection(a["selection"])))
            drop = dst.fotmob_drops.get(k)
            if reason is None:
                why = "repricing: line no longer priced"
            elif reason.startswith("gate:"):
                why = ("minute gate (30-43 / 55-85)" if "outside windows" in reason else
                       "fav_chasing redefinition (no trigger left)" if "no trigger" in reason else "gate: " + reason[5:].strip())
            elif reason == "emitted" and drop:
                why = LABEL.get(drop, drop)
            else:
                why = LABEL.get(reason, reason)
        per[i] = why
        cnt[why] += 1
    return cnt, per


def f2(v: Any, nd: int = 4, signed: bool = False) -> str:
    if v is None:
        return "–"
    return f"{v:+.{nd}f}" if signed else f"{v:.{nd}f}"


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
    ev_states, _ = load_log(dd / f"run_{a.date}_evening.log", "evening")
    fotmob: dict[str, list[dict]] = defaultdict(list)
    for e in fm_a + fm_b:
        fotmob[e["event"]].append(e)
    meta = load_meta(ROOT / "data/dryrun_http.sqlite3", list(s.espn_leagues))
    priors = load_priors(ROOT / "data/espn_summary.sqlite3")

    R = {}
    for v in ("legacy", "v3", "v4"):
        r = ReplayV(v, s, meta, priors, fotmob)
        r.run(states, restart_between_segments=(v == "legacy"))
        r.grade()
        R[v] = r
    M = {v: metrics_v4(R[v].alerts) for v in R}
    orig = original_rows(dd / "paper_trades.sqlite3")
    fr = read_frozen(ROOT / s.engine_b_params_path) or {}
    h4 = R["v4"].hash
    frozen_ok = fr.get("sha256") == h4
    a_old, per_old = attribute_v(R["legacy"], R["v4"])
    a_v3, per_v3 = attribute_v(R["v3"], R["v4"])

    names = {"legacy": "Old (legacy)", "v3": "v3", "v4": "v4"}
    fm = lambda m, k, nd=2: f2(m[k], nd, True)  # noqa: E731
    pct = lambda x: "–" if x is None else f"{100 * x:+.1f}%"  # noqa: E731
    rows = [
        ("Alerts", lambda m: str(m["alerts"])),
        ("Graded / open", lambda m: f"{m['graded']} / {m['open']}"),
        ("Matches with alerts", lambda m: str(m["matches"])),
        ("Staked (graded)", lambda m: f"{m['staked']:.0f}"),
        ("Flat 1u P&L / ROI", lambda m: f"{fm(m, 'flat_pnl')} u / {pct(m['flat_roi'])}"),
        ("Quarter-Kelly P&L / ROI", lambda m: f"{fm(m, 'qk_pnl')} / {pct(m['qk_roi'])}"),
        ("Record", lambda m: m["record"]),
        ("Brier model (n)", lambda m: f"{f2(m['brier'])} ({m['n_brier']})"),
        ("Brier prior-only", lambda m: f2(m["brier_prior"])),
        ("BSS = 1 − Brier/Brier_prior", lambda m: f2(m["bss"], 3, True)),
        ("Log-loss model", lambda m: f2(m["logloss"])),
        ("Log-loss prior-only", lambda m: f2(m["ll_prior"])),
        ("LL skill = 1 − LL/LL_prior", lambda m: f2(m["ll_skill"], 3, True)),
        ("Max stake on one match", lambda m: f"{m['max_match'][1]:.0f} ({m['max_match'][0].split(':', 1)[-1]})"),
        ("Avg fair odds of chosen lines (all / graded)", lambda m: f"{f2(m['avg_fair'], 2)} / {f2(m['avg_fair_graded'], 2)}"),
    ]
    gaps = [(states[i - 1]["_t"], states[i]["_t"]) for i in range(1, len(states)) if states[i]["_t"] - states[i - 1]["_t"] > 600]
    hhmm = lambda t: f"{datetime.utcfromtimestamp(t):%H:%M}"  # noqa: E731
    open_m = sorted({f"{x['home']} v {x['away']}" for v in R for x in R[v].alerts if not x.get("result")})
    L = [f"# Rules replay v4 – {a.date} (Old vs v3 vs v4)", "",
         f"Generated {datetime.utcnow():%Y-%m-%d %H:%M} UTC. No network calls; nothing sent.", "",
         f"* **States used: {len(states)}** logged live states from the afternoon logs (run_attempt2.log + run.log), "
         f"{len({d['event'] for d in states})} matches, {hhmm(states[0]['_t'])}–{hhmm(states[-1]['_t'])} UTC.",
         f"* **Evening run excluded** (out-of-sample): dryrun/run_{a.date}_evening.log had {len(ev_states)} states "
         f"({len({d['event'] for d in ev_states})} matches) at generation time; none are replayed here.",
         f"* Old-rules reproduction: live paper table {len(orig)} alerts vs legacy replay {len(R['legacy'].alerts)}.",
         f"* **v4 params sha256: `{h4}`** (config/engine_b_params.json, frozen_at {fr.get('frozen_at', '–')}, "
         f"file hash {'matches' if frozen_ok else 'DOES NOT match'} the replayed v4 set). v3 set: `{R['v3'].hash[:16]}…`.",
         "", "## Caveats (read first)", "",
         "* **In-sample.** v3 and v4 were both designed after seeing these exact results; any improvement is expected by "
         "construction and is not evidence of edge. The evening run is the first out-of-sample check.",
         "* **Tiny, correlated sample.** A handful of bets on ~15 matches; ROI, Brier, BSS and LL skill are noise-dominated "
         "(one result flips the sign).",
         "* **Min-odds fill.** Every bet assumed filled exactly at its minimum odds with the first-tier quarter-Kelly stake; "
         "no bookmaker odds were seen (v4 therefore always used the fair≈1.95 reverse-pricing selector, never log-growth).",
         "* **Gaps.** No states in " + ", ".join(f"{hhmm(x)}–{hhmm(y)}" for x, y in gaps) + " UTC (box paused); no rule set "
         "could alert there.",
         "* **FotMob** = only what was logged live; states carry no shots-inside-box, so v4's chasing-corner SIB exception "
         "never fires in the replay (the 1.03 cap always applies).",
         "* **Prior-only benchmark** = same engine, same state (score, corners, minute, red cards irrelevant), "
         "time-decayed pre-match lambdas, no multipliers, no shrinkage/pace; scored on each rule set's *own* chosen lines.",
         ("* Open (ungraded) alerts: " + ", ".join(open_m)) if open_m else "* All alerts graded.",
         "", "## Side by side", "", "| metric | " + " | ".join(names[v] for v in R) + " |", "|---|---|---|---|"]
    L += [f"| {lab} | " + " | ".join(fn(M[v]) for v in R) + " |" for lab, fn in rows]
    L += ["", "## Old-rules alerts removed by v4 (first blocking rule in the v4 pipeline)", "", "| rule | alerts |", "|---|---|"]
    L += [f"| {k} | {n} |" for k, n in a_old.most_common()]
    L += ["", "## v3 alerts removed by v4", "", "| rule | alerts |", "|---|---|"]
    L += [f"| {k} | {n} |" for k, n in a_v3.most_common()]
    L += ["", "'repricing' = the v4 rates (prior-only goals, Gamma-Poisson corners, |diff|=2 dampener 0.75/0.80, momentum "
          "clip, chasing cap 1.03) moved the line out of the priced range; 'single line per family' = another line in the "
          "same family was closer to fair 1.95.", "",
          "## v4 alerts", "",
          "| match | min | score | corners | line | fair | prior fair | min odds | stake | FT | result | P&L u | P&L | FotMob | notes |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for x in R["v4"].alerts:
        mk = "Corners" if x["market"] == "TOTAL_CORNERS" else "Goals"
        pf = 1 / _p(x["prior_settlement"])
        res = x.get("result")
        L.append(f"| {x['home']} v {x['away']} | {int(x['minute'])} | {x['score']} | {x['corners']} | "
                 f"{x['selection'].title()} {x['line']:g} {mk} | {x['fair']:.2f} | {pf:.2f} | {x['min_odds']:.2f} | "
                 f"{x['stake']:.0f} | {x.get('ft', '')} | {res or 'open'} | {'' if res is None else f'{x['pnl_u']:+.2f}'} | "
                 f"{'' if res is None else f'{x['pnl_m']:+.2f}'} | {x['fotmob']} | {', '.join(x.get('notes') or [])} |")
    L += ["", "## v3 alerts and their v4 fate", "", "| match | min | score | line | fair | result | P&L | v4 fate |",
          "|---|---|---|---|---|---|---|---|"]
    for i, x in enumerate(R["v3"].alerts):
        mk = "Corners" if x["market"] == "TOTAL_CORNERS" else "Goals"
        res = x.get("result")
        L.append(f"| {x['home']} v {x['away']} | {int(x['minute'])} | {x['score']} | {x['selection'].title()} {x['line']:g} {mk} |"
                 f" {x['fair']:.2f} | {res or 'open'} | {'' if res is None else f'{x['pnl_m']:+.2f}'} | {per_v3.get(i)} |")
    out = Path(a.out) if a.out else dd / f"report_{a.date}_v4.md"
    out.write_text("\n".join(L) + "\n")
    print(json.dumps({"states": len(states), "evening_states": len(ev_states), "orig": len(orig), "hash": h4,
                      "frozen_ok": frozen_ok, "M": M, "attr_old": a_old, "attr_v3": a_v3}, default=str, indent=1))


if __name__ == "__main__":
    main()
