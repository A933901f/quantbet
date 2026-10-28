#!/usr/bin/env python
"""Record what you actually did on Bet365 for an alert (forward run). No network, nothing is sent.

    python scripts/record_fill.py <signal_id> --odds <bet365_odds> [--stake <amount>]
    python scripts/record_fill.py <signal_id> --skipped [--reason "line_moved"]
    python scripts/record_fill.py --list [--hours 72]

``<signal_id>`` is the ``ID:`` line at the bottom of the Telegram card (a unique prefix of >= 6 characters is
enough when the alerts DB is available). Every fill is written twice:

* ``data/fills.csv`` (append-only, COMMITTED to the repo - the durable record; on GitHub Actions the alerts DB
  lives in the Actions cache and cannot be written from your machine), and
* the alerts DB (``taken_odds, taken_stake, fill_status, fill_reason, filled_at``; columns added on first use)
  when it exists locally, e.g. the ``data/quantbet.sqlite3`` from a downloaded run artifact.

Validation: odds must be > 1.01, the id must exist in the alerts DB (``--no-db`` skips the check when you only
have the CSV), a second fill of the same id is refused without ``--force``, and a paper-only alert cannot be
"filled" (it is never a bet) without ``--force``. ``--stake`` defaults to the alert's first-tier stake.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quantbet.database import migrate_fill_columns  # noqa: E402
from quantbet.fills import ID_RE, MIN_TAKEN_ODDS, Fill, append_fill, latest_by_id, read_fills  # noqa: E402

DEFAULT_DB = ROOT / "data" / "quantbet.sqlite3"
DEFAULT_FILLS = ROOT / "data" / "fills.csv"


class FillError(Exception):
    """User-facing validation error (exit code 2)."""


def _rows(conn: sqlite3.Connection, where: str = "", args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(f"SELECT * FROM alerts {where} ORDER BY created_at", args)]


def _is_paper(row: dict[str, Any]) -> bool:
    try:
        return bool((json.loads(row.get("extra") or "{}") or {}).get("paper_only"))
    except (TypeError, ValueError):
        return False


def resolve_id(conn: sqlite3.Connection, sid: str) -> dict[str, Any]:
    """Exact id, else a unique prefix (>= 6 chars)."""
    rows = _rows(conn, "WHERE id = ?", (sid,))
    if not rows:
        rows = _rows(conn, "WHERE id LIKE ?", (sid.replace("%", "").replace("_", "") + "%",))
    if not rows:
        raise FillError(f"no alert with id {sid!r} in the alerts DB")
    if len(rows) > 1:
        raise FillError(f"id prefix {sid!r} is ambiguous: {', '.join(r['id'] for r in rows[:5])}")
    return rows[0]


def min_odds_of(r: dict[str, Any]) -> float:
    """The card's MINIMUM BET365 ODDS (``extra.min_odds``; the ``min_odds`` column is the abort floor)."""
    try:
        x = json.loads(r.get("extra") or "{}") or {}
    except (TypeError, ValueError):
        x = {}
    return float(x.get("min_odds") or r["odds"])


def describe(r: dict[str, Any]) -> str:
    when = datetime.fromtimestamp(float(r["created_at"]), timezone.utc).strftime("%m-%d %H:%M")
    mk = {"TOTAL_GOALS": "Goals", "TOTAL_CORNERS": "Corners", "TOTAL_CARDS": "Cards"}.get(r["market"], r["market"])
    minute = f"{int(r['minute'])}'" if r.get("minute") is not None else "pre"
    return (f"{r['id']}  {when}Z  {r['home']} v {r['away']}  {minute} {r.get('score') or ''}  "
            f"{str(r['selection']).capitalize()} {float(r['line']):g} {mk}  min {min_odds_of(r):.2f}  "
            f"stake {float(r['stake']):g}")


def cmd_list(db: Path, fills_path: Path, hours: float) -> int:
    csv_done = latest_by_id(read_fills(fills_path)[0])
    if not db.is_file():
        raise FillError(f"--list needs the alerts DB ({db}); download it from the run artifact or pass --db")
    conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(alerts)")}
        rows = _rows(conn, "WHERE created_at >= ?", (time.time() - hours * 3600,))
    finally:
        conn.close()
    open_ = [r for r in rows if not _is_paper(r)
             and not ("fill_status" in cols and r.get("fill_status"))
             and not any(r["id"].startswith(k) for k in csv_done)]
    print(f"{len(open_)} unfilled staked alert(s) in the last {hours:g} h (paper-only cards are never bets):")
    for r in open_:
        print("  " + describe(r))
    return 0


def cmd_record(a: argparse.Namespace) -> int:
    sid = a.signal_id.strip()
    if not ID_RE.match(sid):
        raise FillError(f"bad signal id {sid!r} (the hex id on the card's ID: line)")
    if a.odds is not None and not a.odds > MIN_TAKEN_ODDS:
        raise FillError(f"--odds must be > {MIN_TAKEN_ODDS}")
    if a.stake is not None and not a.stake > 0:
        raise FillError("--stake must be > 0")
    if a.skipped and (a.odds is not None or a.stake is not None):
        raise FillError("--skipped takes no --odds / --stake")
    db, fills_path = Path(a.db), Path(a.fills)
    row: dict[str, Any] | None = None
    conn = None
    if db.is_file() and not a.no_db:
        conn = sqlite3.connect(str(db))
        migrate_fill_columns(conn)
        conn.commit()
        row = resolve_id(conn, sid)
        sid = row["id"]
    elif not a.no_db:
        raise FillError(f"alerts DB {db} not found: download it from the run artifact, pass --db, or use --no-db "
                        "to record into the CSV without the id check")
    existing = latest_by_id(read_fills(fills_path)[0]).get(sid)
    if row is not None and row.get("fill_status"):
        existing = existing or Fill(sid, row["fill_status"], row.get("taken_odds"), row.get("taken_stake"),
                                    row.get("fill_reason"), float(row.get("filled_at") or 0.0))
    if existing is not None and not a.force:
        raise FillError(f"{sid} already recorded as {existing.fill_status}"
                        + (f" @ {existing.taken_odds:g}" if existing.taken_odds else "") + " - use --force to replace")
    if row is not None and _is_paper(row) and not a.skipped and not a.force:
        raise FillError(f"{sid} is a PAPER-ONLY alert (never a bet); use --force if you really bet it")
    stake = a.stake
    if not a.skipped and stake is None and row is not None and float(row.get("stake") or 0) > 0:
        stake = float(row["stake"])
    f = Fill(sid, "skipped" if a.skipped else "filled", None if a.skipped else float(a.odds), stake,
             a.reason, time.time())
    append_fill(fills_path, f)
    if conn is not None:
        conn.execute("UPDATE alerts SET taken_odds=?, taken_stake=?, fill_status=?, fill_reason=?, filled_at=? "
                     "WHERE id=?", (f.taken_odds, f.taken_stake, f.fill_status, f.fill_reason, f.filled_at, sid))
        conn.commit()
        conn.close()
    msg = f"recorded {f.fill_status} {sid}"
    if f.fill_status == "filled":
        msg += f" @ {f.taken_odds:g}" + (f" stake {f.taken_stake:g}" if f.taken_stake is not None else "")
        if row is not None and f.taken_odds is not None and f.taken_odds < min_odds_of(row) - 1e-9:
            msg += f"  WARNING: below the alert's minimum odds {min_odds_of(row):.2f}"
    elif f.fill_reason:
        msg += f" ({f.fill_reason})"
    print(msg + f"  -> {fills_path}" + (f" + {db}" if conn is not None else ""))
    if fills_path.resolve().is_relative_to(ROOT.resolve()):
        print("commit data/fills.csv (git add data/fills.csv && git commit && git push) so audit_run sees it")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("signal_id", nargs="?")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--odds", type=float, help="Bet365 odds you actually got (> 1.01)")
    g.add_argument("--skipped", action="store_true", help="you did not bet the alert")
    ap.add_argument("--stake", type=float, help="amount staked (default: the alert's first-tier stake)")
    ap.add_argument("--reason", default=None, help='skip reason, e.g. "line_moved", "price_below_min", "suspended"')
    ap.add_argument("--list", action="store_true", help="show recent staked alerts with no fill recorded")
    ap.add_argument("--hours", type=float, default=72.0, help="--list window (default 72 h)")
    ap.add_argument("--force", action="store_true", help="replace an existing fill / allow a paper-only fill")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--fills", default=str(DEFAULT_FILLS))
    ap.add_argument("--no-db", action="store_true", help="CSV only, no id check (no local alerts DB)")
    a = ap.parse_args(argv)
    try:
        if a.list:
            return cmd_list(Path(a.db), Path(a.fills), a.hours)
        if not a.signal_id:
            ap.error("signal_id is required (or --list)")
        if a.odds is None and not a.skipped:
            ap.error("give --odds <bet365_odds> or --skipped")
        return cmd_record(a)
    except FillError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
