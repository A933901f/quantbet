#!/usr/bin/env python
"""v4.2 calibration: corner half split + 2-goal-lead dampeners. Writes the ``v4_2`` section of
config/calibration_report.json (other sections untouched). Offline: reads cached CSVs/summaries.

    python scripts/calibrate_v42.py
    # inputs: data/football_data/{E0,SP1,D1,I1,F1}_{2122..2425}.csv  (scripts/calibrate.py --download)
    #         data/statsbomb/corner_summary.json                       (scripts/statsbomb_corners.py)

1. Football-Data columns are checked: only full-time corners (HC/AC) exist, no half-time corners.
2. H2/H1 per-minute corner rate ratio from StatsBomb open data (corner passes by period; minutes played per
   half = last event timestamp in the half, so stoppage time is included). Bootstrap 90% CI over matches.
3. Goals lead dampener: matches with |HTHG-HTAG| >= 2. Observed 2nd-half goals vs expected 2nd-half goals, where
   expected = lambda_pre x s2. lambda_pre = Poisson total solved from the de-vigged closing O/U 2.5 price
   (Pinnacle close PC>2.5/PC<2.5, else market average AvgC>2.5/AvgC<2.5); s2 = pooled 2nd-half goal share.
   dampener = ratio(lead>=2) / ratio(all matches)  (the denominator removes any bias of the lambda_pre method).
   n and a bootstrap 90% CI reported.
4. Corner lead dampener: StatsBomb, second-half minutes with |diff| >= 2 vs the same match's other second-half
   minutes is too sparse per match, so we use pooled rates: corners/min in H2 while |diff| >= 2 divided by the
   expected corners/min for those matches (each match's own full-match corners x the H2 per-minute share).
"""
from __future__ import annotations

import glob
import json
import math
import random
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def poisson_total_from_over25(p_over: float) -> float:
    lo, hi = 0.2, 8.0
    for _ in range(60):
        mid = (lo + hi) / 2
        p = 1 - math.exp(-mid) * (1 + mid + mid * mid / 2)
        lo, hi = (mid, hi) if p < p_over else (lo, mid)
    return (lo + hi) / 2


def boot(fn, items, n=2000, seed=1):
    rng = random.Random(seed)
    vals = []
    for _ in range(n):
        s = [items[rng.randrange(len(items))] for _ in items]
        v = fn(s)
        if v is not None and np.isfinite(v):
            vals.append(v)
    return [round(float(np.percentile(vals, 5)), 3), round(float(np.percentile(vals, 95)), 3)]


def football_data() -> dict:
    files = sorted(glob.glob(str(ROOT / "data/football_data/*_2[1-4]2[2-5].csv")))
    dfs = []
    cols_seen = set()
    for f in files:
        d = pd.read_csv(f, encoding="latin-1")
        cols_seen |= set(d.columns)
        d["file"] = Path(f).name
        dfs.append(d)
    df = pd.concat(dfs, ignore_index=True)
    half_corner_cols = sorted(c for c in cols_seen if "C" in c and ("HT" in c or c.startswith(("H1", "H2", "1H", "2H"))))
    corner_cols = sorted(c for c in cols_seen if c in ("HC", "AC", "HTHC", "HTAC"))
    df = df.dropna(subset=["FTHG", "FTAG", "HTHG", "HTAG"])
    po, pu, src = None, None, []
    for o, u in (("PC>2.5", "PC<2.5"), ("AvgC>2.5", "AvgC<2.5")):
        if o in df:
            src.append(o)
            io, iu = 1 / df[o], 1 / df[u]
            p = io / (io + iu)
            po = p if po is None else po.fillna(p)
    df["p_over"] = po
    df = df.dropna(subset=["p_over"])
    df["lam_pre"] = [poisson_total_from_over25(p) for p in df["p_over"]]
    df["g1"] = df.HTHG + df.HTAG
    df["g2"] = df.FTHG + df.FTAG - df.g1
    s2 = float(df.g2.sum() / (df.g1.sum() + df.g2.sum()))
    df["exp2"] = df.lam_pre * s2
    df["htd"] = (df.HTHG - df.HTAG).abs()
    rows = df[["g2", "exp2", "htd"]].to_records(index=False).tolist()

    def ratio(rs, cond=lambda d: True):
        o = sum(r[0] for r in rs if cond(r[2]))
        e = sum(r[1] for r in rs if cond(r[2]))
        return o / e if e else None

    def damp(rs):
        a = ratio(rs)
        b = ratio(rs, lambda d: d >= 2)
        return b / a if a and b else None

    by = {f"ht_diff_{k}": {"n": int((df.htd == k).sum()), "obs_over_exp_h2": round(ratio(rows, lambda d, k=k: d == k), 3)}
          for k in (0, 1)}
    by["ht_diff_ge2"] = {"n": int((df.htd >= 2).sum()), "obs_over_exp_h2": round(ratio(rows, lambda d: d >= 2), 3)}
    return {
        "files": [Path(f).name for f in files], "matches": int(len(df)),
        "half_time_corner_columns": half_corner_cols, "corner_columns": corner_cols,
        "has_half_time_corners": bool(half_corner_cols),
        "lambda_pre_source": "de-vigged closing O/U 2.5 (" + " then ".join(src) + "), Poisson total",
        "second_half_goal_share": round(s2, 4), "all_obs_over_exp_h2": round(ratio(rows), 3), **by,
        "goal_lead_dampener": round(damp(rows), 3), "goal_lead_dampener_ci90": boot(damp, rows),
        "n_lead_matches": int((df.htd >= 2).sum()),
    }


def statsbomb() -> dict:
    d = json.load(open(ROOT / "data/statsbomb/corner_summary.json"))
    ms = d["matches"]
    items = [(m["corners"]["1"], m["corners"]["2"], m["end"]["1"], m["end"]["2"]) for m in ms]

    def r21(it):
        c1, c2 = sum(x[0] for x in it), sum(x[1] for x in it)
        t1, t2 = sum(x[2] for x in it), sum(x[3] for x in it)
        return (c2 / t2) / (c1 / t1) if c1 and t1 and t2 else None

    c1, c2 = sum(x[0] for x in items), sum(x[1] for x in items)
    t1, t2 = sum(x[2] for x in items), sum(x[3] for x in items)
    # corner lead dampener: H2 minutes while |diff| >= 2
    lead_items = []
    for m in ms:
        tot = m["corners"]["1"] + m["corners"]["2"]
        if tot == 0:
            continue
        end2 = 45.0 + m["end"]["2"]
        goals = sorted((g[1] if g[0] == 1 else max(g[1], 45.0), g[2]) for g in m["goals"])
        home = m["home"]
        # sequence of states over H2 time
        hg = ag = 0
        segs = []
        t_prev = 45.0
        for t, team in goals:
            if t > 45.0:
                segs.append((t_prev, t, abs(hg - ag)))
                t_prev = t
            if team == home:
                hg += 1
            else:
                ag += 1
        segs.append((t_prev, end2, abs(hg - ag)))
        lead_min = sum(b - a for a, b, dd in segs if dd >= 2 and b > a)
        if lead_min <= 0:
            lead_items.append((0.0, 0.0, 0.0))
            continue
        lead_c = sum(1 for per, t, _ in m["corner_min"] if per == 2 and any(a <= t < b and dd >= 2 for a, b, dd in segs))
        # expected per-minute H2 corner rate for this match: its own total x pooled H2 share / H2 minutes
        share2 = c2 / (c1 + c2)
        exp_rate = tot * share2 / max(m["end"]["2"], 1.0)
        lead_items.append((float(lead_c), lead_min, exp_rate * lead_min))

    def cdamp(it):
        o, e = sum(x[0] for x in it), sum(x[2] for x in it)
        return o / e if e else None

    n_lead = sum(1 for x in lead_items if x[1] > 0)
    return {
        "source": "StatsBomb open data (github.com/statsbomb/open-data), " + ", ".join(sorted({m['league'] for m in ms})),
        "matches": len(ms), "h1_corners": c1, "h2_corners": c2, "h1_minutes": round(t1, 1), "h2_minutes": round(t2, 1),
        "h2_share_of_corners": round(c2 / (c1 + c2), 4), "h1_share_of_corners": round(c1 / (c1 + c2), 4),
        "h2_h1_per_minute_ratio": round(r21(items), 3), "h2_h1_ci90": boot(r21, items),
        "corner_lead_dampener_raw": round(cdamp(lead_items), 3) if n_lead else None,
        "corner_lead_dampener_ci90": boot(cdamp, lead_items) if n_lead else None,
        "n_lead_matches": n_lead, "lead_minutes": round(sum(x[1] for x in lead_items), 1),
        "lead_corners_observed": int(sum(x[0] for x in lead_items)),
        "lead_corners_expected": round(sum(x[2] for x in lead_items), 1),
        "note": "corner lead dampener uses each match's own total corners as the baseline, which already includes "
                "the lead period (biases toward 1); indicative only",
    }


def main() -> None:
    fd = football_data()
    sb = statsbomb()
    path = ROOT / "config/calibration_report.json"
    rep = json.loads(path.read_text()) if path.exists() else {}
    rep["v4_2"] = {"football_data": fd, "statsbomb": sb}
    path.write_text(json.dumps(rep, indent=2))
    print(json.dumps(rep["v4_2"], indent=1))


if __name__ == "__main__":
    main()
