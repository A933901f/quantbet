#!/usr/bin/env python3
"""Offline v4.5-sharp / v4.4-syndicate replay of a live run (log + SQLite artifact). No network calls.

Feeds every logged ``live state`` (telemetry-gated exactly like v4.3-global) through
:class:`quantbet.syndicate.SyndicateSignalEngine` under ``--rules`` (default ``v4.5-sharp``; the
``v4.4-syndicate`` replay is still reproducible) and compares the result with the alerts that the
live run actually stored (``alerts`` table of ``data/quantbet.sqlite3``):

* **survivor**  - the replay alerts the same match + family within +-5 match minutes of the original
  (any line / direction; the matched replay alert is shown);
* **blocked**   - otherwise; the reason is the replay decision trace for the original line at the poll
  nearest to the original alert (``unmapped_league_blocked``, ``youth_reserve_blocked``,
  ``outside_minute_window``, ``lift > cap``, ``momentum_flurry_cap``, ``no_sustained_momentum``,
  ``no_activity_confirm``, ``match_direction_conflict``, ``cooldown`` ...).

Output keys: ``orig`` (the stored alerts, graded), ``replay`` (the replayed rule set, graded; this
key was called ``v44`` before the v4.5 patch), ``all_replay``, ``by_market_replay``, ``by_tier_replay``,
``replay_breakdown``, ``orig_breakdown``, ``survivors``, ``blocked``, ``per_alert``, ``groups`` ...

New replay alerts (not near any original) are listed too. Everything is graded with 90-minute
rules from the saved final ESPN scoreboard (``soccer/all`` JSON, regulation totals). Paper-only
TOTAL_CARDS alerts (v4.5) are graded as Bet365 Total Cards (yellow 1 / red 2 / second-yellow red 3)
and reported separately - never inside the staked P&L.

IN-SAMPLE WARNING: v4.5-sharp's thresholds were chosen after looking at this very run. A replay
on the same log is a consistency check of the implementation, NOT validation of the edge.

Replay limits (stated in the report): the log stores corners and shots on target only; total shots
are reconstructed from the logged activity values (``replay_global.reconstruct_off_target``);
fouls / yellow cards were not logged by v4.3, so the **cards family cannot be replayed** (cards
need ESPN ``foulsCommitted``; no telemetry -> no alert, which is what the engine does here). v4.5
logs ``card_points`` (Bet365 per-player points) and fouls, so future runs are replayable.
FotMob xG verdicts are only available where the live run fetched them.

Usage (from the repo root)::

    python scripts/replay_syndicate.py --log ../src_in/logs/live_40554836425.log \
        --db ../src_in/data/quantbet.sqlite3 --summary-db ../src_in/data/espn_summary.sqlite3 \
        --finals ../work/all_20261027_final.json --rules v4.5-sharp --out ../work/replay_v45.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from rapidfuzz import fuzz  # noqa: E402
from replay_global import ReplayGlobal, load_meta_files, reconstruct_off_target, telemetry_ok  # noqa: E402
from replay_rules import Meta, build_state, load_log, load_priors  # noqa: E402

from quantbet.config import Settings  # noqa: E402
from quantbet.engine import LiveFootballModel, SignalEngine, rules_preset  # noqa: E402
from quantbet.football_data import load_ratings  # noqa: E402
from quantbet.frozen import param_set, params_hash  # noqa: E402
from quantbet.main import build_engine_config, build_global, build_risk, build_syndicate, global_params_dict, load_live_params  # noqa: E402
from audit_run import finals_from_payload, load_finals, settle_alerts  # noqa: E402

FAM = {"TOTAL_GOALS": "goals", "TOTAL_CORNERS": "corners", "TOTAL_CARDS": "cards"}
UNIT = 10.0


def load_db_alerts(db: Path) -> list[dict[str, Any]]:
    """Alerts stored by the live run (``alerts`` table)."""
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    out = []
    for r in c.execute("SELECT * FROM alerts ORDER BY created_at"):
        d = dict(r)
        x = json.loads(d.get("extra") or "{}") if isinstance(d.get("extra"), str) else {}
        d["x"] = x
        out.append(d)
    return out


def fuzzy_meta(meta: dict[str, Meta], keys: set[str]) -> dict[str, Meta]:
    """Map event keys without exact meta (e.g. transliterated umlauts) onto the closest ESPN final."""
    out = dict(meta)
    for k in keys:
        if k in out:
            continue
        _, h, a = k.split(":", 2)
        best, score = None, 0.0
        for mk, m in meta.items():
            _, mh, ma = mk.split(":", 2)
            sc = min(fuzz.ratio(h, mh), fuzz.ratio(a, ma))
            if sc > score:
                best, score = m, sc
        if best is not None and score >= 80:
            out[k] = best
    return out


class ReplaySyndicate(ReplayGlobal):
    """v4.4-syndicate / v4.5-sharp engine on logged states (shots reconstructed, slugs/tiers from the log)."""

    VERSION = "v4.5-sharp"

    def __init__(self, *a: Any, off: dict | None = None, rules: str | None = None, **kw: Any) -> None:
        if rules:
            self.VERSION = rules
        self.off = off or {}
        self.views: dict[tuple[str, float], dict] = {}
        super().__init__(*a, **kw)

    def reset(self) -> None:
        from quantbet.global_layer import GlobalRatingsResolver
        from quantbet.syndicate import SyndicateSignalEngine

        s = self.s
        kw = dict(target_edge=s.target_edge, abort_edge=s.abort_edge, kelly_multiplier=s.kelly_multiplier,
                  min_minute=s.min_alert_minute, max_minute=s.max_alert_minute, realert_cooldown_s=s.realert_cooldown_s,
                  bankroll=s.bankroll, max_match_stake_frac=s.max_match_stake_frac, family_cooldown_min=s.family_cooldown_min)
        pricing, lp = rules_preset(self.VERSION, load_live_params(ROOT / s.engine_b_params_path), **kw)
        registry, gparams = build_global(s)
        self.registry = registry
        self.pricing, self.params = pricing, lp
        self.hash = params_hash(param_set(self.VERSION, pricing, lp, global_params_dict(s)))
        import dataclasses

        self.prior_model = LiveFootballModel(dataclasses.replace(lp, prior_only=True))

        def prior_lookup(key: str):  # type: ignore[no-untyped-def]
            if not str(self.cur_src.get(key, "")).startswith("espn_"):
                return None
            m = self.meta.get(key)
            return self.priors.get(m.id) if m else None

        resolver = GlobalRatingsResolver(registry, None, load_ratings(ROOT / s.football_data_ratings_path),
                                         devig_method=s.devig_method, prior_lookup=prior_lookup,
                                         slug_lookup=lambda k: self.slugs.get(k),
                                         team_table_slugs=gparams.team_table_slugs)
        risk = build_risk(s)
        self.risk = risk
        sparams, spriors = build_syndicate(s)
        self.free = SyndicateSignalEngine(registry, risk, resolver, pricing, SignalEngine(build_engine_config(s), lp),
                                          sizer=lambda f, o: risk.stake_from_kelly(f), params=sparams, priors=spriors)
        self.free.auto_commit = False

    def run(self, states: list[dict], restart_between_segments: bool = False) -> None:  # noqa: ARG002
        for d in states:
            if not telemetry_ok(d):
                continue
            self.cur_src[d["event"]] = d.get("ratings_src") or ""
            st = build_state(d, self.meta.get(d["event"]))
            if d.get("slug"):
                st.league_slug = d["slug"]
            off = self.off.get((d["event"], d["_t"]))
            sot = d.get("sot") or [None, None]
            if off is not None and sot[0] is not None and sot[1] is not None:
                st.home_shots, st.away_shots = sot[0] + off[0], sot[1] + off[1]
            sh = d.get("shots")  # logged by v4.4 runs (exact), preferred when present
            if sh and sh[0] is not None:
                st.home_shots, st.away_shots = sh[0], sh[1]
            for side, key in (("home", 0), ("away", 1)):
                f = d.get("fouls")
                y = d.get("yellows")
                rd = d.get("reds")
                cp = d.get("card_points")  # v4.5 logs: Bet365 per-player card points
                if f and f[key] is not None:
                    setattr(st, f"{side}_fouls", f[key])
                if y and y[key] is not None:
                    setattr(st, f"{side}_yellow_cards", y[key])
                if rd and rd[key] is not None:
                    setattr(st, f"{side}_red_cards", rd[key])
                if cp and cp[key] is not None:
                    setattr(st, f"{side}_card_points", cp[key])
            sigs = self.free.on_match_state(st, now=d["_t"])
            self.traces[(d["event"], d["_t"])] = dict(self.free.last_trace.get(d["event"], {}))
            mv = self.free.last_momentum.get(d["event"])
            if mv is not None:
                self.views[(d["event"], d["_t"])] = dict(mv.to_dict(), minute=d["minute"])
            sigs = self.confirm(sigs, st, d["_t"], False)
            for sg in sigs:
                self.free.commit(sg)
            ratings = self.free.engine.ratings.get(d["event"])
            for sg in self.risk.size_all(sigs):
                x = sg.extra
                tiers = x.get("tiers") or []
                if sg.market.value == "TOTAL_CARDS":
                    psett = None
                else:
                    sp = self.prior_model.predict(st, ratings).settlement(sg.market, sg.selection, sg.line)
                    psett = [sp.win, sp.half_win, sp.push, sp.half_loss, sp.loss]
                self.alerts.append({
                    "t": d["_t"], "event": sg.event_key, "home": sg.home, "away": sg.away, "minute": sg.minute,
                    "score": sg.score, "corners": f"{x.get('home_corners')}-{x.get('away_corners')}",
                    "market": sg.market.value, "line": sg.line, "selection": sg.selection.value, "fair": sg.fair_odds,
                    "min_odds": x.get("min_odds"), "abort_floor": x.get("abort_floor"),
                    "stake": float(tiers[0][1]) if tiers else 0.0, "settlement": x.get("settlement"),
                    "triggers": x.get("triggers"), "fotmob": x.get("fotmob"), "lift": x.get("lift"),
                    "tier": x.get("liquidity_tier"), "slug": x.get("league_slug"), "league": sg.league,
                    "momentum": x.get("momentum"), "syndicate": x.get("syndicate"), "boost_target": x.get("boost_target"),
                    "combos": x.get("combos"), "dedup": x.get("dedup"), "prior_settlement": psett,
                    "paper_only": bool(x.get("paper_only")), "x": {"paper_only": bool(x.get("paper_only"))},
                    "_signal": sg,
                })


def grade(alerts: list[dict], finals: list[dict], states_by_event: dict[str, list[dict]]) -> None:
    """90-minute settlement with the AUDIT rules (``audit_run.settle_alerts``: goals from goal clocks,
    AET/PEN corners from the last logged 2nd-half state, cards as Bet365 Total Cards Y=1 / R=2 per
    player from the regular-time booking details)."""
    todo = []
    for a in alerts:
        a.setdefault("event_key", a["event"])
        a.setdefault("x", {})
        a["odds"] = a["min_odds"]
        a["stake_units"] = a["stake"] / UNIT
        todo.append(a)
    settle_alerts(todo, finals, states_by_event)
    for a in todo:
        a["pnl_m"] = a.get("pnl_money")
        a["pnl_u"] = a.get("pnl_flat")
        a.pop("final", None)


def summary(alerts: list[dict], unit: float) -> dict[str, Any]:
    """Staked book only (paper-only cards are summarised separately by :func:`paper_summary`)."""
    g = [a for a in alerts if a.get("result") and not a.get("paper_only")]
    rec = Counter(a["result"] for a in g)
    staked = sum(a["stake"] for a in g)
    pm = sum(a["pnl_m"] for a in g)
    # Brier on the model's push-adjusted win probability (pushes excluded)
    br = [((a["settlement"][0] + 0.5 * a["settlement"][1]) / max(1e-9, 1 - a["settlement"][2])
           - (1.0 if a["result"] in ("win", "half-win") else 0.0)) ** 2
          for a in g if a.get("settlement") and a["result"] != "push"]
    return {"n": len(alerts), "graded": len(g), "record": dict(rec), "flat_u": round(sum(a["pnl_u"] for a in g), 3),
            "staked": staked, "pnl": round(pm, 2), "pnl_u": round(pm / unit, 3) if unit else None,
            "roi": round(pm / staked, 4) if staked else None,
            "brier": round(sum(br) / len(br), 4) if br else None}


def paper_summary(alerts: list[dict]) -> dict[str, Any]:
    p = [a for a in alerts if a.get("paper_only")]
    g = [a for a in p if a.get("result")]
    return {"n": len(p), "graded": len(g), "record": dict(Counter(a["result"] for a in g)),
            "flat_u": round(sum(a["pnl_u"] for a in g), 3), "note": "PAPER ONLY - UNCALIBRATED, never staked"}


def breakdown(alerts: list[dict], unit: float) -> dict[str, Any]:
    """Totals and P&L by market+direction and by liquidity tier (staked alerts only)."""
    out: dict[str, Any] = {"by_market": {}, "by_tier": {}}
    for key, fn in (("by_market", lambda a: f"{a['market']}:{a['selection']}"), ("by_tier", lambda a: str(a.get("tier")))):
        groups: dict[str, list[dict]] = defaultdict(list)
        for a in alerts:
            if not a.get("paper_only"):
                groups[fn(a)].append(a)
        out[key] = {k: summary(v, unit) for k, v in sorted(groups.items())}
    return out


def nearest_trace(traces: dict, event: str, t: float) -> tuple[float | None, dict]:
    best, bt = None, None
    for (k, tt), tr in traces.items():
        if k != event:
            continue
        if bt is None or abs(tt - t) < abs(bt - t):
            best, bt = tr, tt
    return bt, best or {}


def main(argv: list[str] | None = None) -> dict[str, Any]:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--log", type=Path, required=True)
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--summary-db", type=Path, required=True)
    ap.add_argument("--finals", type=Path, nargs="+", required=True, help="saved final ESPN soccer/all scoreboards")
    ap.add_argument("--http-db", type=Path, default=None, help="run HTTP cache (its last scoreboard is a fallback)")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--slate-minutes", type=float, default=340.0)
    ap.add_argument("--rules", default="v4.5-sharp", choices=["v4.5-sharp", "v4.4-syndicate"])
    a = ap.parse_args(argv)
    rules = a.rules

    s = Settings(_env_file=None, alert_rules=rules, leagues_config_path=ROOT / "config/leagues.json",  # type: ignore[call-arg]
                 engine_b_params_path=ROOT / "config/engine_b_params.json",
                 syndicate_priors_path=ROOT / "config/syndicate_priors.json")
    states, fm = load_log(a.log, "run")
    states.sort(key=lambda d: d["_t"])
    reg, _ = build_global(s)
    keys = {d["event"] for d in states}
    meta = fuzzy_meta(load_meta_files(a.finals, reg), keys)
    priors = load_priors(a.summary_db)
    fot: dict[str, list[dict]] = defaultdict(list)
    for e in fm:
        fot[e["event"]].append(e)
    slugs = {d["event"]: d.get("slug") for d in states if d.get("slug")}
    off = reconstruct_off_target(states)
    r = ReplaySyndicate(rules, s, meta, priors, fot, slugs, off=off, rules=rules)
    r.run(states)
    finals: list[dict] = []
    for p in a.finals:
        finals += finals_from_payload(json.loads(Path(p).read_text()), str(p))
    if a.http_db:
        finals += load_finals(a.http_db)
    by_ev: dict[str, list[dict]] = defaultdict(list)
    for d in states:
        by_ev[d["event"]].append(d)
    global UNIT
    UNIT = s.unit_size
    grade(r.alerts, finals, by_ev)

    orig = load_db_alerts(a.db)
    for o in orig:
        o["event"] = o["event_key"]
        o["t"] = float(o["created_at"])
        o["min_odds"] = float((o.get("x") or {}).get("min_odds") or o.get("odds"))
        o["stake"] = float(o.get("stake") or 0.0)
        o["settlement"] = (o.get("x") or {}).get("settlement")
        o["tier"] = (o.get("x") or {}).get("liquidity_tier")
    grade(orig, finals, by_ev)

    surv, blocked = [], []
    used: set[int] = set()
    for o in orig:
        fam = FAM[o["market"]]
        hit = None
        for i, n in enumerate(r.alerts):
            if n["event"] == o["event"] and FAM[n["market"]] == fam and abs(float(n["minute"]) - float(o["minute"])) <= 5.0:
                hit = (i, n)
                break
        if hit:
            used.add(hit[0])
            surv.append({"orig": o, "replay": hit[1]})
            o["_v45"] = ("survives", f"{hit[1]['minute']:g}' {hit[1]['selection']} {hit[1]['line']:g} @{hit[1]['min_odds']}"
                         f" {hit[1]['tier']} stake {hit[1]['stake']:g} -> {hit[1].get('result')}")
            continue
        bt, tr = nearest_trace(r.traces, o["event"], o["t"])
        reason = None
        for (mk, line, sel), why in tr.items():
            if getattr(mk, "value", mk) == o["market"] and abs(line - float(o["line"])) < 1e-9 and getattr(sel, "value", sel) == o["selection"]:
                reason = why
        mv = None
        if bt is not None:
            mv = r.views.get((o["event"], bt))
        reason = reason or ("no state at this time (telemetry gate)" if not tr else "line not priced")
        blocked.append({"orig": o, "reason": reason, "momentum": mv})
        o["_v45"] = ("blocked", str(reason))
    new = [n for i, n in enumerate(r.alerts) if i not in used]
    t0 = min(d["_t"] for d in states)
    t1 = max(d["_t"] for d in states)
    slate_n = len(r.alerts) * a.slate_minutes / max(1.0, (t1 - t0) / 60.0)
    per_alert = []
    for i, o in enumerate(orig, 1):
        lift = ((o.get("x") or {}).get("lift") or {}).get("lift")
        per_alert.append({"n": i, "event": o["event"], "minute": o["minute"], "market": o["market"],
                          "selection": o["selection"], "line": o["line"], "lift": lift,
                          "slug": (o.get("x") or {}).get("league_slug"), "tier": o.get("tier"),
                          "result": o.get("result"), "orig_flat_u": o.get("pnl_u"), "orig_pnl": o.get("pnl_m"),
                          "status": o["_v45"][0], "detail": o["_v45"][1]})

    def grp(pred) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        rows = [x for x in per_alert if pred(x)]
        return {"n": len(rows), "survive": sum(1 for x in rows if x["status"] == "survives"),
                "blocked": sum(1 for x in rows if x["status"] == "blocked"),
                "orig_flat_u": round(sum(x["orig_flat_u"] or 0 for x in rows), 3),
                "surviving_orig_flat_u": round(sum(x["orig_flat_u"] or 0 for x in rows if x["status"] == "survives"), 3),
                "block_reasons": dict(Counter(_bucket(x["detail"]) for x in rows if x["status"] == "blocked"))}

    groups = {
        "profitable_lift_lt_15": grp(lambda x: x["lift"] is not None and x["lift"] < 0.15 and (x["orig_flat_u"] or 0) > 0),
        "lift_lt_15_all": grp(lambda x: x["lift"] is not None and x["lift"] < 0.15),
        "lift_gt_20": grp(lambda x: x["lift"] is not None and x["lift"] > 0.20),
        "lift_gt_20_losers": grp(lambda x: x["lift"] is not None and x["lift"] > 0.20 and (x["orig_flat_u"] or 0) < 0),
        "espn_unmapped": grp(lambda x: str(x["slug"] or "").startswith("espn.")),
        "tier_3": grp(lambda x: x["tier"] == "TIER_3"),
    }
    out = {
        "rules": rules, "params_sha256": r.hash, "states": len(states),
        "window_utc": [t0, t1], "orig": summary(orig, s.unit_size), "replay": summary(r.alerts, s.unit_size),
        "alerts_per_340min": round(slate_n, 2),
        "survivors": [{"event": x["orig"]["event"], "orig": f"{x['orig']['minute']:g}' {x['orig']['market']} {x['orig']['selection']} {x['orig']['line']:g} -> {x['orig'].get('result')}",
                       "replay": f"{x['replay']['minute']:g}' {x['replay']['market']} {x['replay']['selection']} {x['replay']['line']:g} @{x['replay']['min_odds']} {x['replay']['tier']} stake {x['replay']['stake']:g} -> {x['replay'].get('result')}"}
                      for x in surv],
        "blocked_reasons": dict(Counter(_bucket(b["reason"]) for b in blocked)),
        "blocked": [{"event": b["orig"]["event"], "minute": b["orig"]["minute"], "market": b["orig"]["market"],
                     "line": b["orig"]["line"], "result": b["orig"].get("result"), "reason": b["reason"],
                     "momentum_adj": (b["momentum"] or {}).get("adj_index"), "labels": (b["momentum"] or {}).get("labels")}
                    for b in blocked],
        "new_alerts": [{k: v for k, v in n.items() if k != "_signal"} for n in new],
        "all_replay": [{k: v for k, v in n.items() if k != "_signal"} for n in r.alerts],
        "by_market_replay": dict(Counter(f"{n['market']}:{n['selection']}" for n in r.alerts)),
        "by_tier_replay": dict(Counter(n["tier"] for n in r.alerts)),
        "blocked_by_result": {res: dict(Counter(_bucket(b["reason"]) for b in blocked if b["orig"].get("result") == res))
                              for res in sorted({str(b["orig"].get("result")) for b in blocked})},
        "replay_breakdown": breakdown(r.alerts, s.unit_size),
        "orig_breakdown": breakdown(orig, s.unit_size),
        "paper_cards": paper_summary(r.alerts),
        "groups": groups,
        "per_alert": per_alert,
        "in_sample_warning": "thresholds were tuned on this same run - consistency check, not validation",
        # every (poll, priced line) decision of the replay, bucketed - shows how often each gate fired
        "decision_counts": dict(Counter(_bucket(w) for tr in r.traces.values() for w in tr.values()).most_common()),
    }
    out["_alert_objects"] = r.alerts  # in-process use (sample cards); not serialised
    out["_views"] = r.views
    vals = sorted(v["adj_index"] for v in r.views.values()
                  if v.get("inputs") and (30 <= v["minute"] <= 43 or 55 <= v["minute"] <= 85))
    if vals:
        q = lambda f: vals[min(len(vals) - 1, int(f * len(vals)))]  # noqa: E731
        out["momentum_percentiles_in_window"] = {"n": len(vals), "p10": q(0.10), "p25": q(0.25), "p50": q(0.50),
                                                 "p75": q(0.75), "p90": q(0.90), "p95": q(0.95)}
    if a.out:
        a.out.write_text(json.dumps({k: v for k, v in out.items() if not k.startswith("_")}, indent=2, default=str))
    return out


def _bucket(reason: str | None) -> str:
    r = str(reason or "?")
    for p in ("tier_lift", "lift > ", "lift: net multiplier <= ", "lift: net multiplier < ", "gate:", "bearish", "cards:",
              "youth_reserve_blocked", "momentum_flurry_cap", "tier3_market_blocked", "no_activity_confirm",
              "bullish"):
        if r.startswith(p):
            return p.strip()
    return r


if __name__ == "__main__":
    res = main()
    print(json.dumps({k: v for k, v in res.items() if k not in ("new_alerts", "all_replay", "blocked", "per_alert")
                      and not k.startswith("_")},
                     indent=2, default=str))
    for n in res["all_replay"]:
        print(f"{n['event']:<55} {n['minute']:>5.1f}' {n['market'][6:]:<8} {n['selection']:<5} {n['line']:>5g} "
              f"fair {n['fair']:.2f} min {n['min_odds']:.2f} {n['tier']} {n['stake']:g} {n['triggers']} -> {n.get('result')}")
