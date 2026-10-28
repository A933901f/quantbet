#!/usr/bin/env python3
"""Estimate the v4.4-syndicate / v4.5-sharp priors offline (no network) -> config/syndicate_priors.json.

Inputs (all already on disk):

* ``data/football_data/*.csv`` (Football-Data.co.uk season files with HF/AF/HY/AY/HR/AR/HC/AC):
  - per-division cards/90 (yellows + reds, 1 each) and fouls/90 -> ``cards_baselines``;
  - correlation priors between the three families (goals, corners, cards) -> ``rho``.
    ``rho`` is the phi coefficient of the full-time Over indicators at each family's
    median-ish line (goals 2.5, corners 9.5/10.5, cards 3.5/4.5, the line nearest p=0.5),
    i.e. exactly the quantity used in P(A and B) = PA*PB + rho*sqrt(PA(1-PA)PB(1-PB)).
    Pearson correlations of the totals are reported alongside for reference.
* an ESPN ``all/scoreboard`` JSON body of finished matches (``--espn-finals``; optional): booking
  clocks -> second-half share of cards and the 60'-85' |goal diff|<=1 urgency ratio.

v4.5-sharp adds ``cards_bet365`` (the v4.4 keys stay byte-identical so the v4.4 freeze is unchanged):
Bet365 Total Cards = ``HY + AY + 2 x (HR + AR)`` (yellow 1, red 2) per division / pooled / per slug,
and the Negative Binomial shape ``nb_r`` of the 90-minute count with division-specific means
(maximum likelihood, common r; method-of-moments ``sum n m^2 / sum n (v - m)`` reported alongside).

Football-Data semantics (data/football_data/notes.txt): "English and Scottish yellow cards do not
include the initial yellow card when a second is shown to a player converting it into a red, but
this is included as a yellow (plus red) for European games." So HY + AY + 2R is the exact Bet365
count (a second-yellow dismissal = 1 + 2 = 3) for the European divisions, and undercounts each
second-yellow dismissal by 1 for E0 / E1 / E2 / SC0 (reported per division as ``sy_bias_max``).

These are PRIORS: full-time, pre-match-unconditional correlations used for live remaining-time legs.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NEED = ("FTHG", "FTAG", "HF", "AF", "HY", "AY", "HR", "AR", "HC", "AC")


def _f(v: str) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def load_rows(folder: Path) -> dict[str, list[dict[str, float]]]:
    out: dict[str, list[dict[str, float]]] = defaultdict(list)
    for f in sorted(folder.glob("*.csv")):
        div = f.stem.split("_")[0]
        with f.open(encoding="utf-8-sig", errors="replace", newline="") as fh:
            rd = csv.DictReader(fh)
            if not rd.fieldnames or not all(c in rd.fieldnames for c in NEED):
                continue
            for r in rd:
                vals = {k: _f(r.get(k, "")) for k in NEED}
                if any(v is None for v in vals.values()):
                    continue
                out[div].append(vals)  # type: ignore[arg-type]
    return out


def totals(r: dict[str, float]) -> tuple[float, float, float, float]:
    goals = r["FTHG"] + r["FTAG"]
    corners = r["HC"] + r["AC"]
    cards = r["HY"] + r["AY"] + r["HR"] + r["AR"]
    fouls = r["HF"] + r["AF"]
    return goals, corners, cards, fouls


UK_DIVS = ("E0", "E1", "E2", "E3", "EC", "SC0", "SC1", "SC2", "SC3")  # HY excludes the 1st yellow of a 2nd-yellow red


def cards_bet365(r: dict[str, float]) -> float:
    """Bet365 Total Cards (yellow 1, red 2) of one full-time row."""
    return r["HY"] + r["AY"] + 2.0 * (r["HR"] + r["AR"])


def nb_r_mom(groups: list[list[float]]) -> float | None:
    """Common NegBin shape with group-specific means, method of moments: ``sum n m^2 / sum n (v - m)``."""
    num = den = 0.0
    for xs in groups:
        if len(xs) < 2:
            continue
        m, v = statistics.fmean(xs), statistics.pvariance(xs)
        num += len(xs) * m * m
        den += len(xs) * (v - m)
    return (num / den) if den > 0 else None


def nb_r_mle(groups: list[list[float]]) -> float | None:
    """Common NegBin shape with group-specific means (the group mean is the MLE of each mean), MLE in r."""
    import numpy as np
    from scipy.optimize import minimize_scalar
    from scipy.special import gammaln

    data = [(np.asarray(xs, dtype=float), float(statistics.fmean(xs))) for xs in groups if len(xs) >= 2]
    if not data:
        return None

    def nll(logr: float) -> float:
        r = math.exp(logr)
        tot = 0.0
        for k, m in data:
            tot += float(np.sum(gammaln(k + r) - gammaln(r) - gammaln(k + 1) + r * math.log(r / (r + m))
                                + k * math.log(m / (r + m))))
        return -tot

    res = minimize_scalar(nll, bounds=(math.log(0.5), math.log(1e5)), method="bounded",
                          options={"xatol": 1e-6})
    r = math.exp(res.x)
    return None if r > 0.99e5 else r


def pearson(x: list[float], y: list[float]) -> float:
    mx, my = statistics.fmean(x), statistics.fmean(y)
    sx = math.sqrt(sum((a - mx) ** 2 for a in x))
    sy = math.sqrt(sum((b - my) ** 2 for b in y))
    if sx == 0 or sy == 0:
        return 0.0
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy)


def nearest_half_line(values: list[float], candidates: tuple[float, ...]) -> float:
    return min(candidates, key=lambda L: abs(sum(v > L for v in values) / len(values) - 0.5))


def phi(a: list[bool], b: list[bool]) -> float:
    return pearson([float(x) for x in a], [float(x) for x in b])


def clock_minute(d: dict) -> float | None:
    disp = str((d.get("clock") or {}).get("displayValue") or "")
    m = re.match(r"(\d+)'(?:\+(\d+))?", disp)
    if m:
        return float(m.group(1)) + (float(m.group(2)) / 100.0 if m.group(2) else 0.0)
    v = (d.get("clock") or {}).get("value")
    return float(v) / 60.0 if v is not None else None


def espn_card_profile(path: Path) -> dict:
    body = json.loads(path.read_text())
    n = h1 = h2 = 0
    late_close_cards = late_close_minutes = 0.0
    late_all_cards = late_all_minutes = 0.0
    matches = 0
    for e in body.get("events") or []:
        st = ((e.get("status") or {}).get("type") or {})
        if st.get("state") != "post":
            continue
        comp = (e.get("competitions") or [{}])[0]
        det = comp.get("details")
        if not isinstance(det, list):
            continue
        cs = {c.get("homeAway"): c for c in comp.get("competitors") or []}
        try:
            hid = str(cs["home"]["team"]["id"])
        except (KeyError, TypeError):
            continue
        matches += 1
        goals: list[tuple[float, int]] = []
        cards: list[float] = []
        for d in det:
            if d.get("shootout"):
                continue
            m = clock_minute(d)
            if m is None:
                continue
            disp = str((d.get("clock") or {}).get("displayValue") or "")
            regular = m <= 90.99 or disp.startswith("90'+") or disp.startswith("45'+")
            if not regular:
                continue
            if d.get("scoringPlay"):
                goals.append((m, 1 if str((d.get("team") or {}).get("id")) == hid else -1))
            if d.get("yellowCard") or d.get("redCard"):
                cards.append(m)
                n += 1
                if m < 46.0:  # "45'+2" parses as 45.02 -> first half
                    h1 += 1
                else:
                    h2 += 1
        # 60'-85' exposure split by |goal diff| <= 1
        for minute in range(60, 85):
            gd = sum(s for t, s in goals if t <= minute)
            k = sum(1 for c in cards if minute < c <= minute + 1)
            late_all_cards += k
            late_all_minutes += 1
            if abs(gd) <= 1:
                late_close_cards += k
                late_close_minutes += 1
    out = {"matches": matches, "cards": n, "first_half": h1, "second_half": h2,
           "second_half_share": round(h2 / n, 4) if n else None}
    if late_all_minutes and late_close_minutes:
        r_all = late_all_cards / late_all_minutes
        r_close = late_close_cards / late_close_minutes
        out.update(late_60_85_rate_all=round(r_all, 5), late_60_85_rate_close=round(r_close, 5),
                   late_60_85_close_minutes=int(late_close_minutes), late_60_85_all_minutes=int(late_all_minutes),
                   urgency_ratio=round(r_close / r_all, 4) if r_all > 0 else None)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fd-dir", default=str(ROOT / "data/football_data"))
    ap.add_argument("--baselines", default=str(ROOT / "data/football_data/league_baselines.json"))
    ap.add_argument("--espn-finals", default=None, help="ESPN all/scoreboard JSON of finished matches (optional)")
    ap.add_argument("--out", default=str(ROOT / "config/syndicate_priors.json"))
    a = ap.parse_args(argv)
    rows = load_rows(Path(a.fd_dir))
    if not rows:
        print("no Football-Data rows with fouls/cards found", file=sys.stderr)
        return 1
    allr = [r for v in rows.values() for r in v]
    g, c, k, fl = (list(x) for x in zip(*(totals(r) for r in allr)))
    gl = nearest_half_line(g, (1.5, 2.5, 3.5))
    cl = nearest_half_line(c, (8.5, 9.5, 10.5, 11.5))
    kl = nearest_half_line(k, (2.5, 3.5, 4.5, 5.5))
    og, oc, ok = [x > gl for x in g], [x > cl for x in c], [x > kl for x in k]
    rho = {"goals|corners": round(phi(og, oc), 4), "goals|cards": round(phi(og, ok), 4),
           "corners|cards": round(phi(oc, ok), 4)}
    pear = {"goals|corners": round(pearson(g, c), 4), "goals|cards": round(pearson(g, k), 4),
            "corners|cards": round(pearson(c, k), 4)}
    # per-division card / foul baselines, mapped to ESPN slugs via league_baselines.json fd_code
    fd_to_slug: dict[str, list[str]] = defaultdict(list)
    try:
        for slug, info in json.loads(Path(a.baselines).read_text()).items():
            if isinstance(info, dict) and info.get("fd_code"):
                fd_to_slug[str(info["fd_code"])].append(slug)
    except (OSError, ValueError):
        pass
    by_div = {}
    for div, rs in sorted(rows.items()):
        kk = [totals(r)[2] for r in rs]
        ff = [totals(r)[3] for r in rs]
        by_div[div] = {"n": len(rs), "cards90": round(statistics.fmean(kk), 3), "fouls90": round(statistics.fmean(ff), 3),
                       "cards_var": round(statistics.pvariance(kk), 3), "slugs": sorted(fd_to_slug.get(div, []))}
    cards_baselines = {}
    for div, v in by_div.items():
        for slug in v["slugs"]:
            cards_baselines[slug] = {"cards90": v["cards90"], "fouls90": v["fouls90"], "n": v["n"], "fd_code": div}
    pooled = {"cards90": round(statistics.fmean(k), 3), "fouls90": round(statistics.fmean(fl), 3), "n": len(k),
              "cards_var": round(statistics.pvariance(k), 3)}
    espn = espn_card_profile(Path(a.espn_finals)) if a.espn_finals else None
    # v4.5-sharp: Bet365 Total Cards (yellow 1, red 2) + NegBin over-dispersion (new keys only)
    b_by_div = {}
    groups = []
    for div, rs in sorted(rows.items()):
        kk = [cards_bet365(r) for r in rs]
        ff = [totals(r)[3] for r in rs]
        reds = statistics.fmean([r["HR"] + r["AR"] for r in rs])
        groups.append(kk)
        m, v = statistics.fmean(kk), statistics.pvariance(kk)
        b_by_div[div] = {"n": len(rs), "cards90": round(m, 3), "cards_var": round(v, 3),
                         "fouls90": round(statistics.fmean(ff), 3), "reds90": round(reds, 4),
                         "nb_r_mom": round(m * m / (v - m), 3) if v > m else None,
                         "hy_includes_first_yellow": div not in UK_DIVS,
                         "sy_bias_max": round(reds, 4) if div in UK_DIVS else 0.0,
                         "slugs": sorted(fd_to_slug.get(div, []))}
    b_base = {}
    for div, v in b_by_div.items():
        for slug in v["slugs"]:
            b_base[slug] = {"cards90": v["cards90"], "fouls90": v["fouls90"], "n": v["n"], "fd_code": div}
    kb = [x for gp in groups for x in gp]
    mb, vb = statistics.fmean(kb), statistics.pvariance(kb)
    r_mle, r_mom = nb_r_mle(groups), nb_r_mom(groups)
    # corners (full time, both teams) dispersion with division means: sanity check of corner_dispersion
    cgroups = [[totals(r)[1] for r in rs] for _, rs in sorted(rows.items())]
    c_mle, c_mom = nb_r_mle(cgroups), nb_r_mom(cgroups)
    bet365 = {
        "counting": "Bet365 Total Cards: yellow = 1, red = 2 (second yellow -> red = 1 + 2 = 3; max 3 per player). "
                    "NOT the 10/25 booking-points market.",
        "fd_semantics": "HY + AY + 2 x (HR + AR). Football-Data notes.txt: English/Scottish HY exclude the first "
                        "yellow of a second-yellow dismissal (undercount <= reds90 per match for E0/E1/E2/SC0); "
                        "European divisions include it (exact).",
        "pooled": {"cards90": round(mb, 3), "cards_var": round(vb, 3), "fouls90": round(statistics.fmean(fl), 3),
                   "n": len(kb), "nb_r_pooled_naive": round(mb * mb / (vb - mb), 3) if vb > mb else None},
        "nb_r": round(r_mle, 3) if r_mle else (round(r_mom, 3) if r_mom else None),
        "nb_r_method": "MLE of a common shape r with division-specific means (Football-Data full time)",
        "nb_r_mle_within": round(r_mle, 3) if r_mle else None,
        "nb_r_mom_within": round(r_mom, 3) if r_mom else None,
        "by_division": b_by_div,
        "baselines": b_base,
    }
    corners_check = {"nb_r_mle_within": round(c_mle, 3) if c_mle else None,
                     "nb_r_mom_within": round(c_mom, 3) if c_mom else None,
                     "note": "full-time both-team corners, division means; basis of "
                             "LiveModelParams.corner_dispersion in the v4.5-sharp preset (65.0; v4.2 file 149.56)"}
    out = {
        "_doc": "v4.4-syndicate priors. Generated offline by scripts/estimate_syndicate_priors.py; "
                "hashed into the v4.4-syndicate freeze (global.syndicate.priors). Re-freeze after editing.",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {"football_data": "data/football_data/*.csv (Football-Data.co.uk; files with HF/AF/HY/AY/HR/AR/HC/AC)",
                   "divisions": sorted(rows), "matches": len(allr),
                   "espn_finals": "ESPN all/scoreboard final details (booking clocks)" if espn else None},
        "rho": rho,
        "rho_label": ("PRIOR: phi coefficient of full-time Over indicators at goals %.1f / corners %.1f / cards %.1f; "
                      "pre-match-unconditional, applied to live remaining-time legs" % (gl, cl, kl)),
        "rho_lines": {"goals": gl, "corners": cl, "cards": kl},
        "pearson_totals": pear,
        "cards_counting": "yellow = 1, red = 1 (a second yellow shown as a red counts the yellow and the red)",
        "cards_pooled": pooled,
        "cards_by_division": by_div,
        "cards_baselines": cards_baselines,
        "cards_time_profile": espn,
        "cards_bet365": bet365,
        "corners_dispersion_check": corners_check,
    }
    Path(a.out).write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"rho": rho, "pearson": pear, "pooled": pooled, "espn": espn,
                      "cards_bet365": {k: v for k, v in bet365.items() if k not in ("by_division", "baselines")},
                      "corners_dispersion_check": corners_check}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
