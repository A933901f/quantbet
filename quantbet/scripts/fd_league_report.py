#!/usr/bin/env python
"""v4.3-global: download Football-Data CSVs for the extra leagues (politely) and report columns + baselines.

    python scripts/fd_league_report.py            # downloads missing files (2 s gap), writes the report

One file per league/season is cached under data/football_data/ (never re-downloaded). Output:
``dryrun/football_data_leagues_report.md`` and ``data/football_data/league_baselines.json``.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

from quantbet.config import Settings  # noqa: E402
from quantbet.football_data import (EXTRA_CODES, FD_TO_ESPN, V43_MAIN_CODES, download, league_baselines,  # noqa: E402
                                    load_csv, load_extra_csv)
from quantbet.global_layer import load_registry  # noqa: E402

DEST = ROOT / "data" / "football_data"
SEASON = 2025  # 2025/26 = last complete European season; calendar-year leagues use Season 2025
WANT = ["HS", "AS", "HST", "AST", "HC", "AC", "PSCH", "PC>2.5", "B365>2.5", "B365C>2.5", "AvgC>2.5", "PSCH", "B365CH"]


async def fetch_all(ua: str) -> dict[str, Path | str]:
    out: dict[str, Path | str] = {}
    for code in list(V43_MAIN_CODES) + list(EXTRA_CODES) + ["E0", "SP1", "D1", "I1", "F1"]:
        try:
            p = await download(code, SEASON, DEST, ua)
            out[code] = p
        except Exception as exc:  # noqa: BLE001
            out[code] = f"failed: {exc}"
        time.sleep(2.0)
    return out


def main() -> int:
    ua = Settings().user_agent
    files = asyncio.run(fetch_all(ua))
    reg = load_registry(ROOT / "config" / "leagues.json")
    rows, base = [], {}
    for code, p in files.items():
        slug = FD_TO_ESPN.get(code, "?")
        if isinstance(p, str):
            rows.append(f"| {code} | {slug} | – | {p} | | | | |")
            continue
        if code in EXTRA_CODES:
            raw = pd.read_csv(p, encoding="utf-8-sig", nrows=5)
            cols = list(raw.columns)
            seasons = sorted(pd.read_csv(p, encoding="utf-8-sig", usecols=["Season"])["Season"].astype(str).unique())
            pick = [x for x in seasons if x.startswith(str(SEASON))]
            df = load_extra_csv(p, code, seasons=pick[-1:] if pick else seasons[-1:])
        else:
            df = load_csv(p)
            cols = list(df.columns)
        b = league_baselines(df)
        base[slug] = {"fd_code": code, **b}
        have = [c for c in WANT if c in cols]
        miss = [c for c in ("HC", "AC", "HS", "AS", "HST", "AST") if c not in cols]
        cfg = reg.get(slug)
        user_c = cfg.corner_baseline if cfg else None
        flag = ""
        if b.get("corners_avg") is not None and user_c is not None:
            d = user_c - float(b["corners_avg"])
            flag = f"{d:+.2f}" + ("  ⚠️ >0.8 off" if abs(d) > 0.8 else "")
        rows.append(f"| {code} | {slug} | {b['n']} | {', '.join(have) or '–'} | {', '.join(miss) or 'none'} | "
                    f"{b['goals_avg']} | {b.get('corners_avg')} | {user_c} {flag} |")
    DEST.mkdir(parents=True, exist_ok=True)
    (DEST / "league_baselines.json").write_text(json.dumps(base, indent=2, sort_keys=True))
    md = ["# Football-Data league report (v4.3-global)", "",
          f"Season used: {SEASON}/{(SEASON + 1) % 100:02d} (main CSVs) or Season {SEASON} (extra `new/*.csv`).", "",
          "| Code | ESPN slug | Matches | Useful columns present | Stat columns MISSING | Goals/match | Corners/match | config corner baseline (Δ vs FD) |",
          "|---|---|---|---|---|---|---|---|", *rows, "",
          "Extra-league files (`new/ARG.csv` etc.) contain results + closing 1X2/O-U odds only: no corners/shots, so their",
          "corner baselines cannot be checked against Football-Data and stay as configured."]
    out = ROOT / "dryrun" / "football_data_leagues_report.md"
    out.write_text("\n".join(md) + "\n")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
