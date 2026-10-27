#!/usr/bin/env python
"""Update league-probation counts from graded paper trades (read-only on the paper DBs).

    python scripts/update_probation.py                       # all dryrun/paper_trades*.sqlite3
    python scripts/update_probation.py --paper dryrun/paper_trades_2026-10-25_americas.sqlite3 --db data/league_probation.sqlite3

Out of sample = graded paper trades created at/after the v4.3-global ``frozen_at`` (override: --since ISO-UTC).
Prints per-group and per-league n / BSS vs prior and whether the group is still on probation (n < 150 or BSS <= 0).
Safe to run while a dry run is writing: paper DBs are opened with ``mode=ro``.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quantbet.global_layer import load_registry  # noqa: E402
from quantbet.probation import PROBATION_N, ProbationBook, oos_cutoff, update_from_paper_dbs  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--paper", action="append", default=[], help="paper-trade sqlite (repeatable)")
    ap.add_argument("--db", default=str(ROOT / "data/league_probation.sqlite3"))
    ap.add_argument("--params", default=str(ROOT / "config/engine_b_params.json"))
    ap.add_argument("--leagues", default=str(ROOT / "config/leagues.json"))
    ap.add_argument("--since", default=None, help="OOS cutoff, ISO UTC (default: v4.3-global frozen_at)")
    a = ap.parse_args(argv)
    papers = [Path(p) for p in a.paper] or sorted((ROOT / "dryrun").glob("paper_trades*.sqlite3"))
    cutoff = (datetime.fromisoformat(a.since.replace("Z", "+00:00")).replace(tzinfo=timezone.utc).timestamp()
              if a.since else oos_cutoff(a.params))
    reg = load_registry(a.leagues) if Path(a.leagues).exists() else None
    book = ProbationBook(a.db)
    n, rows = update_from_paper_dbs(book, papers, cutoff, reg)
    print(f"OOS cutoff {datetime.fromtimestamp(cutoff, timezone.utc):%Y-%m-%dT%H:%M:%SZ}; "
          f"{n} graded OOS rows read from {len(papers)} paper DB(s) -> {a.db}")
    for r in sorted(rows, key=lambda r: (r["scope"] != "group", r["grp"], str(r["key"]))):
        bss = "n/a" if r["bss"] is None else f"{r['bss']:+.3f}"
        flag = f"PROBATION {r['n_graded']}/{PROBATION_N}" if r["on_probation"] else ("core" if r["grp"] == "CORE_V42" else "graduated")
        print(f"{r['scope']:6} {str(r['key']):28} grp={r['grp']:11} n={r['n_graded']:4} scored={r['n_scored']:4} BSS={bss:>7}  {flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
