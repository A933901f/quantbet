#!/usr/bin/env python
"""Calibrate Engine B from free Football-Data.co.uk CSVs.

Examples::

    python scripts/calibrate.py --leagues E0 D1 I1 SP1 --seasons 2024 2025 --download
    python scripts/calibrate.py --csv data/football_data/E0_2526.csv

Writes ``config/engine_b_params.json`` (corner NegBin dispersion, goal
correlation), ``config/team_ratings.json`` (used by RatingsResolver when no
cached Pinnacle price exists) and ``config/calibration_report.json``.
Downloads are single sequential requests with the configured User-Agent and
are cached on disk (one file per league/season).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quantbet.engine import LiveModelParams  # noqa: E402
from quantbet.football_data import (  # noqa: E402
    bet365_vs_pinnacle_close,
    calibrate_league,
    download,
    fit_team_ratings,
    load_many,
    save_ratings,
)


async def _download_all(leagues: list[str], seasons: list[int], dest: Path, ua: str) -> list[Path]:
    out = []
    for code in leagues:
        for yr in seasons:
            try:
                out.append(await download(code, yr, dest, ua))
                await asyncio.sleep(2.0)  # polite: sequential, spaced
            except Exception as exc:  # noqa: BLE001
                print(f"download failed {code} {yr}: {exc}", file=sys.stderr)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", nargs="*", default=[], help="local CSV files")
    ap.add_argument("--leagues", nargs="*", default=["E0"], help="Football-Data codes (E0, D1, I1, SP1, F1, ...)")
    ap.add_argument("--seasons", nargs="*", type=int, default=[2025], help="season start years")
    ap.add_argument("--download", action="store_true", help="download missing CSVs")
    ap.add_argument("--data-dir", default="data/football_data")
    ap.add_argument("--out-dir", default="config")
    ap.add_argument("--shrink-k", type=float, default=6.0)
    ap.add_argument("--half-life-days", type=float, default=180.0)
    ap.add_argument("--v42", action="store_true",
                    help="only (re)write the v4.2 half-split / lead-dampener section (scripts/calibrate_v42.py)")
    a = ap.parse_args(argv)
    if a.v42:
        import runpy
        runpy.run_path(str(Path(__file__).with_name("calibrate_v42.py")), run_name="__main__")
        return 0

    paths = [Path(p) for p in a.csv]
    if a.download:
        ua = os.environ.get("QB_USER_AGENT", "quantbet-signal-engine/0.2 (personal calibration)")
        paths += asyncio.run(_download_all(a.leagues, a.seasons, Path(a.data_dir), ua))
    else:
        for code in a.leagues:
            for yr in a.seasons:
                p = Path(a.data_dir) / f"{code}_{yr % 100:02d}{(yr + 1) % 100:02d}.csv"
                if p.exists():
                    paths.append(p)
    if not paths:
        print("no CSVs found (use --download or --csv)", file=sys.stderr)
        return 1
    df = load_many(paths)
    table = fit_team_ratings(df, a.shrink_k, a.half_life_days)
    report = {}
    for lg, sub in df.groupby("League"):
        report[str(lg)] = calibrate_league(sub, str(lg), table).to_dict()
    overall = calibrate_league(df, "all", table)
    report["all"] = overall.to_dict()
    ev = bet365_vs_pinnacle_close(df)
    report["bet365_preclose_vs_pinnacle_close_mean_ev"] = {
        c: round(float(ev[c].mean()), 4) for c in ev.columns if c.startswith("ev_") and ev[c].notna().any()
    }

    params = dataclasses.asdict(LiveModelParams())
    # The conditional fit is in-sample (team means fitted on the same games) and
    # tends to understate variance; take the more dispersed (conservative) value.
    r_corner = min(overall.corner_dispersion_mle, overall.corner_dispersion_conditional)
    params["corner_dispersion"] = round(min(max(r_corner, 3.0), 200.0), 3)
    params["goal_correlation"] = round(min(overall.goal_correlation, 0.3), 4)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "engine_b_params.json").write_text(json.dumps(params, indent=1, default=list))
    save_ratings(table, out / "team_ratings.json")
    (out / "calibration_report.json").write_text(json.dumps(report, indent=1, default=float))
    print(json.dumps({"matches": len(df), "files": [str(p) for p in paths],
                      "corner_dispersion": params["corner_dispersion"], "goal_correlation": params["goal_correlation"],
                      "teams": len(table.att_home_g)}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
