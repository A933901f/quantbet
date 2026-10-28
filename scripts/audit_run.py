#!/usr/bin/env python
"""Offline audit of one live run (v4.4-syndicate / v4.5-sharp tooling). No network, nothing is sent.

    python scripts/audit_run.py --log logs/live_<run>.log --alerts-db data/quantbet.sqlite3 \
        --http-db data/http_cache.sqlite3 --json-out audit.json

Inputs are the artifacts a GitHub Actions run uploads:

* the JSON run log ("live state", "signal", "fotmob confirmation", "skipped: no_live_telemetry" lines),
* the alerts DB (``alerts`` table; ``extra`` holds lift / triggers / tier / dedup context),
* the HTTP cache DB, whose latest ``soccer/all/scoreboard`` body is used as the FINAL-SCORE source.

Settlement (labelled "offline ESPN settlement" everywhere): Bet365 settles goals and corners totals on
regular time (90' + stoppage). Goals at 90' are rebuilt from the scoreboard ``details`` (goal clocks; a
``90'+N'`` clock is regular time, anything later and every shoot-out kick is excluded). Corners: the
scoreboard total for matches that ended in regular time; for AET / penalties matches the last logged
"2nd half" state (the poller's own 90' snapshot) is used and the row is flagged. Fills are assumed at the
alert's MINIMUM BET365 ODDS (no bookmaker price was ever read). Quarter lines are split into two halves.

TOTAL_CARDS (v4.5-sharp, paper only) is graded as Bet365 Total Cards on regular time: per player from the
scoreboard ``details`` (``athletesInvolved``), yellow 1, straight red 2, second-yellow / yellow-then-red
dismissal 3 (max 3 per player; :func:`quantbet.espn.card_points`). Shoot-out and extra-time bookings are
excluded. ``cards_reg`` (yellow / red entry counts per side) is kept for reference; ``y + 2r`` per side
equals the per-player count because ESPN details keep the first yellow of a second-yellow dismissal.
Paper alerts are reported separately and never added to the staked P&L.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import re
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rapidfuzz import fuzz  # noqa: E402

from quantbet.espn import SECOND_YELLOW_TEXTS, card_points_from_details  # noqa: E402
from quantbet.paper import pnl, result_label, settle_total  # noqa: E402
from quantbet.fills import merge_fills, read_fills  # noqa: E402

REG_STATUS = ("STATUS_FULL_TIME", "STATUS_FINAL")
ET_STATUS = ("STATUS_FINAL_AET", "STATUS_FINAL_PEN", "STATUS_END_OF_EXTRATIME")


def _ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def hhmm(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M")


def load_alerts(db: Path) -> list[dict[str, Any]]:
    c = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute("SELECT * FROM alerts ORDER BY created_at")]
    c.close()
    for r in rows:
        r["x"] = json.loads(r.get("extra") or "{}")
    return rows


def load_log(path: Path) -> dict[str, Any]:
    msgs: Counter[str] = Counter()
    states: dict[str, list[dict]] = defaultdict(list)
    fotmob: list[dict] = []
    header: dict[str, Any] = {}
    skips: list[dict] = []
    first = last = None
    for line in path.open():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        msgs[d.get("msg", "")] += 1
        t = _ts(d["ts"]) if d.get("ts") else None
        if t is not None:
            first = t if first is None else min(first, t)
            last = t if last is None else max(last, t)
        m = d.get("msg")
        if m == "live state":
            d["_t"] = t
            states[d["event"]].append(d)
        elif m == "fotmob confirmation":
            fotmob.append(d)
        elif m == "engine params" and not header:
            header = {"rules": d.get("rules"), "params_sha256": d.get("params_sha256")}
        elif m == "skipped: no_live_telemetry":
            skips.append(d)
        elif m == "shutting down":
            header["http_requests"] = d.get("http_requests")
            header["refusals"] = d.get("refusals")
    return {"msgs": msgs, "states": states, "fotmob": fotmob, "header": header, "skips": skips,
            "first": first, "last": last}


def _clock_regular(det: dict) -> bool:
    disp = str((det.get("clock") or {}).get("displayValue") or "")
    if det.get("shootout"):
        return False
    if disp.startswith("90'+") or disp.startswith("45'+"):
        return True
    val = float((det.get("clock") or {}).get("value") or 0.0)
    return val <= 5400.0 + 1e-6


def load_finals(http_db: Path) -> list[dict[str, Any]]:
    """Finals from the newest cached ESPN ``soccer/all`` scoreboard in the run's HTTP cache (read-only)."""
    c = sqlite3.connect(f"file:{http_db.resolve()}?mode=ro", uri=True)
    row = c.execute("SELECT body, fetched_at FROM http_cache WHERE url LIKE '%soccer/all/scoreboard%' "
                    "ORDER BY fetched_at DESC").fetchone()
    c.close()
    if not row:
        return []
    return finals_from_payload(json.loads(row[0]), row[1])


def finals_from_payload(payload: dict[str, Any], fetched_at: Any = None) -> list[dict[str, Any]]:
    """Per event: status, 90' goals (from goal clocks), corners, fouls and 90' cards of an ESPN scoreboard."""
    out = []
    for e in payload.get("events") or []:
        comp = (e.get("competitions") or [{}])[0]
        cs = {x.get("homeAway"): x for x in comp.get("competitors") or []}
        if "home" not in cs or "away" not in cs:
            continue
        st = (e.get("status") or {}).get("type") or {}
        sd = {k: {s.get("name"): s.get("displayValue") for s in cs[k].get("statistics") or []} for k in cs}
        hid = str(cs["home"]["team"].get("id"))
        reg = {"home": 0, "away": 0}
        cards = {"home": {"y": 0, "r": 0}, "away": {"y": 0, "r": 0}}
        for det in comp.get("details") or []:
            side = "home" if str((det.get("team") or {}).get("id")) == hid else "away"
            if det.get("scoringPlay") and _clock_regular(det):
                reg[side] += 1
            if _clock_regular(det):
                txt = str((det.get("type") or {}).get("text") or "").lower()
                red = bool(det.get("redCard")) or txt in ("red card",) + SECOND_YELLOW_TEXTS
                if det.get("yellowCard") and not red:
                    cards[side]["y"] += 1
                if red:
                    cards[side]["r"] += 1
        dets = comp.get("details")
        cards_b365 = None
        if isinstance(dets, list):
            hp, ap = card_points_from_details([d for d in dets if isinstance(d, dict) and _clock_regular(d)],
                                              hid, str(cs["away"]["team"].get("id")))
            cards_b365 = (hp, ap) if hp is not None and ap is not None else None

        def num(v: Any) -> float | None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        out.append({
            "id": str(e.get("id")), "name": e.get("name"), "status": st.get("name"), "completed": st.get("completed"),
            "home": cs["home"]["team"].get("displayName"), "away": cs["away"]["team"].get("displayName"),
            "ft_goals": (num(cs["home"].get("score")), num(cs["away"].get("score"))),
            "reg_goals": (reg["home"], reg["away"]),
            "corners": (num(sd["home"].get("wonCorners")), num(sd["away"].get("wonCorners"))),
            "fouls": (num(sd["home"].get("foulsCommitted")), num(sd["away"].get("foulsCommitted"))),
            "cards_reg": cards, "cards_bet365_reg": cards_b365, "fetched_at": fetched_at,
        })
    return out


def match_final(home: str, away: str, finals: list[dict]) -> dict | None:
    best, score = None, 0.0
    for f in finals:
        s = (fuzz.token_set_ratio(home, f["home"]) + fuzz.token_set_ratio(away, f["away"])) / 2
        if s > score:
            best, score = f, s
    return best if score >= 85 else None


def regulation_corners(states: list[dict]) -> tuple[float, float] | None:
    reg = [d for d in states if str(d.get("status", "")).startswith("2nd") and d.get("corners")]
    if not reg:
        return None
    c = reg[-1]["corners"]
    return (float(c[0] or 0), float(c[1] or 0))


def settle_alerts(alerts: list[dict], finals: list[dict], states: dict[str, list[dict]]) -> None:
    for a in alerts:
        f = match_final(a["home"], a["away"], finals)
        a["final"] = f
        a["result"] = a["pnl_u"] = a["pnl_money"] = None
        a["settle_note"] = ""
        if not f or not f.get("completed"):
            a["settle_note"] = "no final in cache"
            continue
        et = f["status"] in ET_STATUS
        if a["market"] == "TOTAL_CARDS":
            cb = f.get("cards_bet365_reg")
            if cb is None:
                a["settle_note"] = "no booking details"
                continue
            total = float(cb[0] + cb[1])
            a["settle_note"] = "Bet365 Total Cards (Y=1, R=2, per player max 3), regular time" + (
                " [PAPER]" if (a.get("x") or {}).get("paper_only") else "")
        elif a["market"] == "TOTAL_CORNERS":
            if et:
                rc = regulation_corners(states.get(a["event_key"], []))
                if rc is None:
                    a["settle_note"] = "AET/PEN: 90' corners unknown"
                    continue
                total = rc[0] + rc[1]
                a["settle_note"] = f"{f['status'][7:]}: 90' corners from last logged 2nd-half state"
            else:
                if f["corners"][0] is None:
                    a["settle_note"] = "no corner stat"
                    continue
                total = f["corners"][0] + f["corners"][1]
        else:
            total = float(f["reg_goals"][0] + f["reg_goals"][1])
            if et:
                a["settle_note"] = f"{f['status'][7:]}: 90' score {f['reg_goals'][0]}-{f['reg_goals'][1]} from goal clocks"
        a["settle_total"] = total
        parts = settle_total(float(a["line"]), a["selection"], total)
        a["parts"] = parts
        a["result"] = result_label(parts)
        a["pnl_u"] = pnl(parts, float(a["odds"]), float(a["stake_units"]))
        a["pnl_money"] = pnl(parts, float(a["odds"]), float(a["stake"]))
        a["pnl_flat"] = pnl(parts, float(a["odds"]), 1.0)


def dist(vals: list[float], edges: list[float]) -> list[tuple[str, int]]:
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        out.append((f"[{lo:g}, {hi:g})", sum(1 for v in vals if lo <= v < hi)))
    return out


def summarise(alerts: list[dict]) -> dict[str, Any]:
    S: dict[str, Any] = {"n": len(alerts)}
    S["by_market"] = Counter({"TOTAL_CORNERS": "Corners", "TOTAL_CARDS": "Cards"}.get(a["market"], "Goals")
                             + " " + a["selection"] for a in alerts)
    paper = [a for a in alerts if (a.get("x") or {}).get("paper_only")]
    pg = [a for a in paper if a.get("result")]
    S["paper_cards"] = {"n": len(paper), "graded": len(pg), "record": Counter(a["result"] for a in pg),
                        "flat": sum(a["pnl_flat"] for a in pg)}
    alerts = [a for a in alerts if not (a.get("x") or {}).get("paper_only")]  # staked book only below
    S["n_staked"] = len(alerts)
    S["by_tier"] = Counter(a["x"].get("liquidity_tier") for a in alerts)
    S["by_league"] = Counter((a["league"], a["x"].get("league_slug"), a["x"].get("liquidity_tier")) for a in alerts)
    S["probation"] = Counter(bool(a["x"].get("probation")) for a in alerts)
    nets = [a["x"]["lift"]["net_multiplier"] for a in alerts if a["x"].get("lift")]
    lifts = [a["x"]["lift"]["lift"] for a in alerts if a["x"].get("lift")]
    S["net"] = {"mean": statistics.mean(nets), "median": statistics.median(nets), "min": min(nets), "max": max(nets)}
    S["lift"] = {"mean": statistics.mean(lifts), "median": statistics.median(lifts), "min": min(lifts), "max": max(lifts)}
    for fam in ("TOTAL_GOALS", "TOTAL_CORNERS"):
        ls = [a["x"]["lift"]["lift"] for a in alerts if a["market"] == fam]
        ns = [a["x"]["lift"]["net_multiplier"] for a in alerts if a["market"] == fam]
        S[f"lift_{fam}"] = {"n": len(ls), "mean": statistics.mean(ls) if ls else None,
                            "median": statistics.median(ls) if ls else None,
                            "net_mean": statistics.mean(ns) if ns else None}
    S["net_dist"] = dist(nets, [1.06, 1.08, 1.10, 1.12, 1.15, 1.20])
    S["lift_dist"] = dist(lifts, [0.0, 0.08, 0.12, 0.16, 0.20, 0.30, 0.45, 0.70])
    S["ev"] = {"mean": statistics.mean(a["ev"] for a in alerts), "min": min(a["ev"] for a in alerts),
               "max": max(a["ev"] for a in alerts)}
    trig = Counter()
    combos = Counter()
    for a in alerts:
        ts = a["x"].get("triggers") or []
        combos[", ".join(sorted(ts)) or "(none)"] += 1
        for t in ts or ["(none)"]:
            trig[t] += 1
    S["triggers"] = trig
    S["trigger_combos"] = combos
    S["minute_dist"] = dist([float(a["minute"]) for a in alerts], [30, 32, 35, 40, 44, 55, 57, 60, 65, 70, 80, 86])
    S["window"] = Counter("30'-43'" if a["minute"] < 44 else "55'-85'" for a in alerts)
    st = Counter()
    for a in alerts:
        h, w = (int(x) for x in a["score"].split("-"))
        st["level" if h == w else ("lead by 1" if abs(h - w) == 1 else "lead by 2+")] += 1
    S["score_state"] = st
    S["scores"] = Counter(a["score"] for a in alerts)
    per = defaultdict(list)
    for a in alerts:
        per[a["event_key"]].append(a)
    S["per_match"] = Counter(len(v) for v in per.values())
    S["matches"] = len(per)
    near = []
    for k, v in per.items():
        v = sorted(v, key=lambda a: a["created_at"])
        for i in range(1, len(v)):
            dm = float(v[i]["minute"]) - float(v[i - 1]["minute"])
            near.append((k, v[i - 1]["market"], v[i]["market"], dm))
    S["same_match_pairs"] = near
    S["same_family_dupes"] = sum(1 for k, v in per.items()
                                 for fam in ("TOTAL_GOALS", "TOTAL_CORNERS")
                                 if sum(1 for a in v if a["market"] == fam) > 1)
    S["lines"] = Counter((("C" if a["market"] == "TOTAL_CORNERS" else "G"), float(a["line"]) - (
        a["x"].get("home_corners", 0) + a["x"].get("away_corners", 0) if a["market"] == "TOTAL_CORNERS"
        else a["x"].get("home_goals", 0) + a["x"].get("away_goals", 0))) for a in alerts)
    S["line_kind"] = Counter(("quarter" if round(float(a["line"]) * 4) % 2 else
                              ("half" if round(float(a["line"]) * 2) % 2 else "whole")) for a in alerts)
    S["odds"] = {"mean": statistics.mean(float(a["odds"]) for a in alerts),
                 "fair_mean": statistics.mean(float(a["fair_odds"]) for a in alerts)}
    S["stake"] = {"total": sum(float(a["stake"]) for a in alerts), "units": sum(float(a["stake_units"]) for a in alerts)}
    S["fotmob"] = Counter(a["x"].get("fotmob") or "–" for a in alerts)
    g = [a for a in alerts if a.get("result")]
    S["graded"] = len(g)
    S["record"] = Counter(a["result"] for a in g)
    S["pnl_units"] = sum(a["pnl_u"] for a in g)
    S["pnl_money"] = sum(a["pnl_money"] for a in g)
    S["pnl_flat"] = sum(a["pnl_flat"] for a in g)
    S["staked_units_graded"] = sum(float(a["stake_units"]) for a in g)
    by_fam = {}
    for fam in ("TOTAL_GOALS", "TOTAL_CORNERS"):
        gg = [a for a in g if a["market"] == fam]
        by_fam[fam] = {"n": len(gg), "record": Counter(a["result"] for a in gg),
                       "pnl_u": sum(a["pnl_u"] for a in gg), "flat": sum(a["pnl_flat"] for a in gg),
                       "staked_u": sum(float(a["stake_units"]) for a in gg)}
    S["by_family_pnl"] = by_fam
    by_tier = {}
    for t in ("TIER_1", "TIER_2", "TIER_3"):
        gg = [a for a in g if a["x"].get("liquidity_tier") == t]
        by_tier[t] = {"n": len(gg), "record": Counter(a["result"] for a in gg),
                      "pnl_u": sum(a["pnl_u"] for a in gg), "flat": sum(a["pnl_flat"] for a in gg),
                      "staked_u": sum(float(a["stake_units"]) for a in gg)}
    S["by_tier_pnl"] = by_tier
    # model-implied expectation (Brier vs outcome) - settlement vector p = (W + HW) / (1 - P)
    br, n = 0.0, 0
    exp_flat = 0.0
    for a in g:
        sv = a["x"].get("settlement")
        if not sv:
            continue
        w, hw, p, hl, l_ = sv
        exp_flat += w * (float(a["odds"]) - 1) + hw * (float(a["odds"]) - 1) / 2 - hl / 2 - l_
        if a["result"] == "push":
            continue
        prob = (w + hw) / max(1e-9, 1 - p)
        y = 1.0 if a["result"] in ("win", "half-win") else 0.0
        br += (prob - y) ** 2
        n += 1
    S["brier"] = br / n if n else None
    S["model_expected_flat"] = exp_flat
    return S


# ---------------------------------------------------------------------------
# Plumbing (forward run): manual Bet365 fills
# ---------------------------------------------------------------------------
def min_odds_of(a: dict) -> float:
    """The card's MINIMUM BET365 ODDS: ``extra.min_odds`` (the DB ``min_odds`` column holds the abort floor
    for model-only alerts, ``odds`` the minimum)."""
    return float((a.get("x") or {}).get("min_odds") or a["odds"])


def fill_summary(alerts: list[dict]) -> dict[str, Any]:
    """Theoretical (at the alert's minimum odds) vs realized (at the Bet365 odds you took) P&L.

    Staked alerts only (paper-only cards are never bets). ``fill rate`` = filled / staked alerts;
    ``recorded`` = filled + skipped. Slippage = taken odds - minimum odds (positive = you beat the minimum).
    P&L uses the taken stake (default: the alert's first-tier stake) and, separately, 1u flat.
    """
    staked = [a for a in alerts if not (a.get("x") or {}).get("paper_only")]
    filled = [a for a in staked if a.get("fill") is not None and a["fill"].fill_status == "filled"]
    skipped = [a for a in staked if a.get("fill") is not None and a["fill"].fill_status == "skipped"]
    out: dict[str, Any] = {"n_staked": len(staked), "n_filled": len(filled), "n_skipped": len(skipped),
                           "n_unrecorded": len(staked) - len(filled) - len(skipped),
                           "fill_rate_pct": 100.0 * len(filled) / len(staked) if staked else None,
                           "fill_rate_of_recorded_pct": (100.0 * len(filled) / (len(filled) + len(skipped))
                                                         if filled or skipped else None),
                           "skip_reasons": Counter(a["fill"].fill_reason or "(none)" for a in skipped),
                           "paper_fills_ignored": sum(1 for a in alerts if (a.get("x") or {}).get("paper_only")
                                                      and a.get("fill") is not None)}
    slips = [a["fill"].taken_odds - min_odds_of(a) for a in filled]
    out["avg_slippage"] = statistics.mean(slips) if slips else None
    out["avg_slippage_pct"] = (statistics.mean(a["fill"].taken_odds / min_odds_of(a) - 1.0 for a in filled)
                               if filled else None)
    out["below_min_odds"] = sum(1 for d in slips if d < -1e-9)
    g = [a for a in filled if a.get("parts")]
    out["graded_filled"] = len(g)
    stake_of = [(a, float(a["fill"].taken_stake if a["fill"].taken_stake is not None else a["stake"])) for a in g]
    out["theoretical_pnl_min_odds"] = sum(pnl(a["parts"], min_odds_of(a), st) for a, st in stake_of)
    out["realized_pnl"] = sum(pnl(a["parts"], a["fill"].taken_odds, st) for a, st in stake_of)
    out["turnover_filled"] = sum(st for _, st in stake_of)
    out["theoretical_flat_min_odds"] = sum(pnl(a["parts"], min_odds_of(a), 1.0) for a in g)
    out["realized_flat"] = sum(pnl(a["parts"], a["fill"].taken_odds, 1.0) for a in g)
    out["realized_yield_pct"] = (100.0 * out["realized_pnl"] / out["turnover_filled"]
                                 if out["turnover_filled"] else None)
    # theoretical book of EVERY graded staked alert (what the model claims if every alert were filled at min)
    ga = [a for a in staked if a.get("parts")]
    out["theoretical_flat_all_graded"] = sum(pnl(a["parts"], min_odds_of(a), 1.0) for a in ga)
    return out


# ---------------------------------------------------------------------------
# Plumbing: prior provenance (true pre-match DK fetch?) and the pre-match reference "CLV"
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def load_summary_index(summary_db: Path | None) -> list[dict[str, Any]]:
    """One record per stored /summary: teams, state AS FETCHED, first + latest pre-match DK lines."""
    if summary_db is None or not Path(summary_db).is_file():
        return []
    from quantbet.free_stats_poller import dk_lines, summary_body_status

    c = sqlite3.connect(f"file:{Path(summary_db).resolve()}?mode=ro", uri=True)
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(espn_summary_once)")}
        sel = ["event_id", "fetched_at", "body"] + [k for k in ("fetched_state", "fetched_minute", "kickoff",
                                                                "latest_body", "latest_fetched_at", "latest_state")
                                                    if k in cols]
        rows = [dict(zip(sel, r)) for r in c.execute(f"SELECT {', '.join(sel)} FROM espn_summary_once")]
    except sqlite3.Error:
        rows = []
    finally:
        c.close()
    out = []
    for r in rows:
        try:
            body = json.loads(r.get("body") or "null")
        except ValueError:
            continue
        comp = (((body or {}).get("header") or {}).get("competitions") or [{}])[0] or {}
        teams = {t.get("homeAway"): ((t.get("team") or {}).get("displayName") or "") for t in comp.get("competitors") or []}
        st, mi, ko = summary_body_status(body)
        rec = {"event_id": r["event_id"], "home": teams.get("home", ""), "away": teams.get("away", ""),
               "fetched_state": r.get("fetched_state") or st, "fetched_minute": r.get("fetched_minute", mi),
               "fetched_at": r.get("fetched_at"), "kickoff": r.get("kickoff") or ko, "first": dk_lines(body),
               "latest": None}
        if r.get("latest_body") and r.get("latest_state") == "pre":
            with contextlib.suppress(ValueError, TypeError, KeyError):
                rec["latest"] = dk_lines(json.loads(r["latest_body"]))
                rec["latest_fetched_at"] = r.get("latest_fetched_at")
        out.append(rec)
    return out


def _match_summary(a: dict, index: list[dict]) -> dict | None:
    best, score = None, 0.0
    for rec in index:
        sc = min(fuzz.ratio(_norm(a["home"]), _norm(rec["home"])), fuzz.ratio(_norm(a["away"]), _norm(rec["away"])))
        if sc > score:
            best, score = rec, sc
    return best if score >= 80 else None


def _dk_lambda(dk: dict) -> float | None:
    """Goals lambda implied by a DK moneyline + total (same de-vig + ratings mapping as the live prior)."""
    from quantbet.engine import devig, ratings_from_prices

    try:
        ph, pd_, pa = devig([dk["dk_home"], dk["dk_draw"], dk["dk_away"]], "power")
        p_over = None
        if dk.get("dk_total_line") is not None and dk.get("dk_over") and dk.get("dk_under"):
            p_over = devig([dk["dk_over"], dk["dk_under"]], "power")[0]
        return float(ratings_from_prices(ph, pd_, pa, p_over, dk.get("dk_total_line") or 2.5, 10.0).goal_total)
    except (KeyError, TypeError, ValueError):
        return None


def _poisson_ev(line: float, selection: str, goals_now: float, lam_rem: float, odds: float) -> float:
    """Flat EV of the alert at ``odds`` if the remaining goals were Poisson(lam_rem) (prior-only world)."""
    import math

    ev, pk = 0.0, math.exp(-lam_rem)
    for k in range(0, 25):
        if k:
            pk *= lam_rem / k
        ev += pk * pnl(settle_total(line, selection, goals_now + k), odds, 1.0)
    return ev


def prior_provenance(alerts: list[dict], index: list[dict]) -> dict[str, Any]:
    """Per staked alert: did its prior come from a TRUE pre-match DK fetch, and how did the latest pre line move?

    ``pre_ref_ev`` (goals alerts whose prior is a true pre-match DK line only) is the change in flat EV of the alert
    at its minimum odds, in a prior-only Poisson world, when the prior's remaining-goals lambda
    (``lift.lambda_prior``) is rescaled from the lambda the model used to the one implied by the LATEST pre-match
    DK line: > 0 means the pre-match market moved toward the bet before kick-off (a pre-match "CLV" of the prior).
    It is NOT closing-line value of the bet: DraftKings pre-match totals are not the Bet365 in-play price, and
    the prior-only EV itself is negative by construction (an alert needs in-play lift over the prior). When the
    model already used the latest pre line the value is exactly 0.
    """
    rows, cnt = [], Counter()
    for a in alerts:
        x = a.get("x") or {}
        if x.get("paper_only"):
            continue
        pr = x.get("prior") or {}
        fetch = pr.get("fetch")
        src = pr.get("source") or x.get("ratings_source") or "?"
        rec = _match_summary(a, index) if index else None
        if fetch:
            how, state = "alert_extra", fetch.get("fetched_state")
        elif rec is not None:
            how, state = "summary_db", rec.get("fetched_state")
        else:
            how, state = "unknown", None
        market_prior = str(src).startswith("espn_")
        label = ("true_pre_match" if state == "pre" else "in_play_fetch" if state in ("in", "post") else "unknown") \
            if market_prior else "no_market_prior"
        cnt[label] += 1
        dk_latest = pr.get("dk_latest") or pr.get("dk_first") if fetch else None
        if dk_latest is None and rec is not None and rec.get("fetched_state") == "pre":
            dk_latest = rec.get("latest") or rec.get("first")
        r = {"id": a["id"], "match": f"{a['home']} v {a['away']}", "market": a["market"], "prior_source": src,
             "provenance": label, "via": how, "fetched_state": state,
             "fetched_minute": (fetch or {}).get("fetched_minute") if fetch else (rec or {}).get("fetched_minute"),
             "pre_ref_ev": None, "lambda_move_pct": None}
        lam_used = x.get("lambda_pre_total") or pr.get("lambda_goals_prior")
        lam_rem = (x.get("lift") or {}).get("lambda_prior")
        if (a["market"] == "TOTAL_GOALS" and market_prior and label == "true_pre_match" and dk_latest
                and lam_used and lam_rem):
            lam_latest = _dk_lambda(dk_latest)
            if lam_latest:
                goals_now = float(x.get("home_goals", 0) or 0) + float(x.get("away_goals", 0) or 0)
                r["lambda_move_pct"] = 100.0 * (lam_latest / float(lam_used) - 1.0)
                mo = min_odds_of(a)
                ev_used = _poisson_ev(float(a["line"]), a["selection"], goals_now, float(lam_rem), mo)
                ev_latest = _poisson_ev(float(a["line"]), a["selection"], goals_now,
                                        float(lam_rem) * lam_latest / float(lam_used), mo)
                r["pre_ref_ev"] = ev_latest - ev_used
        rows.append(r)
    evs = [r["pre_ref_ev"] for r in rows if r["pre_ref_ev"] is not None]
    return {"counts": cnt, "rows": rows, "n_pre_ref": len(evs),
            "pre_ref_ev_mean": statistics.mean(evs) if evs else None,
            "pre_ref_positive_pct": 100.0 * sum(1 for v in evs if v > 0) / len(evs) if evs else None,
            "note": "pre_ref_ev = flat-EV change at min odds when the prior moves to the latest PRE-match "
                    "DraftKings line (prior-only Poisson); NOT Bet365 in-play CLV"}


def jsonable(o: Any) -> Any:
    """Counters / tuple-keyed dicts -> JSON-safe structures."""
    if isinstance(o, dict):
        return {(k if isinstance(k, (str, int, float, bool)) or k is None else " | ".join(map(str, k))): jsonable(v)
                for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [jsonable(v) for v in o]
    return o


def fmt_counter(c: Counter, total: int | None = None) -> str:
    return ", ".join(f"{k}: {v}" + (f" ({100 * v / total:.0f}%)" if total else "") for k, v in c.most_common())


def main(argv: list[str] | None = None) -> dict[str, Any]:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True)
    ap.add_argument("--alerts-db", required=True)
    ap.add_argument("--http-db", required=True)
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--fills", default=str(ROOT / "data" / "fills.csv"),
                    help="committed fills CSV written by scripts/record_fill.py (merged with the DB fill columns)")
    ap.add_argument("--summary-db", default=None, help="espn_summary.sqlite3 (prior provenance for old alerts)")
    a = ap.parse_args(argv)
    L = load_log(Path(a.log))
    alerts = load_alerts(Path(a.alerts_db))
    finals = load_finals(Path(a.http_db))
    settle_alerts(alerts, finals, L["states"])
    S = summarise(alerts)
    csv_fills, problems = read_fills(Path(a.fills))
    merge_info = merge_fills(alerts, csv_fills)
    S["fills"] = fill_summary(alerts)
    S["fills"].update({"csv_rows": len(csv_fills), "csv_problems": problems, **merge_info})
    S["prior_provenance"] = prior_provenance(alerts, load_summary_index(Path(a.summary_db) if a.summary_db else None))
    S["log"] = {"msgs": dict(L["msgs"]), "header": L["header"], "first": L["first"], "last": L["last"],
                "matches_tracked": len(L["states"]), "skips": len(L["skips"]),
                "fotmob_verdicts": Counter(f.get("verdict") for f in L["fotmob"])}
    S["alerts"] = [{k: (dataclasses.asdict(v) if k == "fill" and v is not None else v)
                    for k, v in x.items() if k not in ("extra",)} for x in alerts]
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(jsonable(S), default=str,
                                               indent=1, ensure_ascii=False))
    print(f"alerts={S['n']} graded={S['graded']} record={dict(S['record'])} pnl_u={S['pnl_units']:+.2f} "
          f"flat={S['pnl_flat']:+.2f}")
    F = S["fills"]
    if F["n_filled"] or F["n_skipped"]:
        print(f"fills: filled={F['n_filled']} skipped={F['n_skipped']} unrecorded={F['n_unrecorded']} "
              f"fill_rate={F['fill_rate_pct']:.0f}% realized={F['realized_pnl']:+.2f} "
              f"theoretical@min={F['theoretical_pnl_min_odds']:+.2f} "
              f"avg_slippage={F['avg_slippage'] if F['avg_slippage'] is not None else float('nan'):+.3f} "
              f"skips={dict(F['skip_reasons'])}")
    else:
        print(f"fills: none recorded ({F['n_staked']} staked alerts) - see scripts/record_fill.py")
    print(f"prior provenance: {dict(S['prior_provenance']['counts'])}")
    return S


if __name__ == "__main__":
    main()
