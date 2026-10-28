"""Manual Bet365 fills for the forward run (plumbing; no network, nothing is sent).

quantbet never reads a Bet365 price: after an alert the user looks at the app, bets (or not) by hand and records
what happened with ``scripts/record_fill.py``. On GitHub Actions the alerts DB lives in the Actions cache, which
the user cannot write to, so every fill is ALSO appended to the committed ``data/fills.csv`` (one row per fill;
the user may equally add a row through the GitHub web editor). ``scripts/audit_run.py`` merges both sources:
for each signal id the row with the latest ``filled_at`` wins.

CSV columns (header required)::

    signal_id,fill_status,taken_odds,taken_stake,fill_reason,filled_at
    3f2a9c1d0b7e,filled,2.05,20,,2026-10-29T19:42:00+00:00
    a81c44e9f012,skipped,,,line_moved,2026-10-29T20:10:00+00:00
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

FILLS_HEADER = ("signal_id", "fill_status", "taken_odds", "taken_stake", "fill_reason", "filled_at")
FILL_STATUSES = ("filled", "skipped")
MIN_TAKEN_ODDS = 1.01  # taken odds must be strictly above this
ID_RE = re.compile(r"^[0-9a-fA-F]{6,32}$")


@dataclass(frozen=True, slots=True)
class Fill:
    """One recorded fill or skip."""

    signal_id: str
    fill_status: str
    taken_odds: float | None
    taken_stake: float | None
    fill_reason: str | None
    filled_at: float  # unix seconds

    def as_row(self) -> list[str]:
        return [self.signal_id, self.fill_status,
                "" if self.taken_odds is None else f"{self.taken_odds:g}",
                "" if self.taken_stake is None else f"{self.taken_stake:g}",
                self.fill_reason or "",
                datetime.fromtimestamp(self.filled_at, timezone.utc).isoformat(timespec="seconds")]


def _num(v: Any) -> float | None:
    if v is None or str(v).strip() == "":
        return None
    return float(v)


def _when(v: Any) -> float:
    s = str(v or "").strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()


def validate_fill(f: Fill) -> None:
    """Raise ``ValueError`` for an impossible fill row."""
    if not ID_RE.match(f.signal_id or ""):
        raise ValueError(f"bad signal id {f.signal_id!r}")
    if f.fill_status not in FILL_STATUSES:
        raise ValueError(f"bad fill_status {f.fill_status!r} (filled|skipped)")
    if f.fill_status == "filled":
        if f.taken_odds is None or not f.taken_odds > MIN_TAKEN_ODDS:
            raise ValueError(f"taken odds must be > {MIN_TAKEN_ODDS} (got {f.taken_odds!r})")
        if f.taken_stake is not None and not f.taken_stake > 0:
            raise ValueError(f"taken stake must be > 0 (got {f.taken_stake!r})")
    elif f.taken_odds is not None or f.taken_stake is not None:
        raise ValueError("a skipped alert has no taken odds / stake")


def read_fills(path: Path | str) -> tuple[list[Fill], list[str]]:
    """All valid rows of a fills CSV plus a list of problems (bad rows are skipped, never fatal)."""
    p = Path(path)
    if not p.is_file():
        return [], []
    out, problems = [], []
    with p.open(newline="", encoding="utf-8") as fh:
        rd = csv.DictReader(fh)
        missing = [c for c in ("signal_id", "fill_status") if c not in (rd.fieldnames or [])]
        if missing:
            return [], [f"{p}: missing columns {missing}"]
        for i, row in enumerate(rd, start=2):
            if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
                continue
            try:
                f = Fill(signal_id=(row.get("signal_id") or "").strip(),
                         fill_status=(row.get("fill_status") or "").strip().lower(),
                         taken_odds=_num(row.get("taken_odds")), taken_stake=_num(row.get("taken_stake")),
                         fill_reason=(row.get("fill_reason") or "").strip() or None,
                         filled_at=_when(row.get("filled_at")))
                validate_fill(f)
            except (ValueError, TypeError) as exc:
                problems.append(f"{p}:{i}: {exc}")
                continue
            out.append(f)
    return out, problems


def append_fill(path: Path | str, f: Fill) -> None:
    """Append one row (creating the file with its header if needed)."""
    validate_fill(f)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.is_file() or p.stat().st_size == 0
    with p.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(FILLS_HEADER)
        w.writerow(f.as_row())


def latest_by_id(fills: Iterable[Fill]) -> dict[str, Fill]:
    """signal_id -> the fill with the latest ``filled_at`` (later rows win ties)."""
    out: dict[str, Fill] = {}
    for f in fills:
        cur = out.get(f.signal_id)
        if cur is None or f.filled_at >= cur.filled_at:
            out[f.signal_id] = f
    return out


def fill_from_db_row(r: dict[str, Any]) -> Fill | None:
    """The fill stored in an ``alerts`` row (None when the columns are absent or empty)."""
    st = r.get("fill_status")
    if st not in FILL_STATUSES:
        return None
    return Fill(str(r["id"]), st, _num(r.get("taken_odds")), _num(r.get("taken_stake")),
                r.get("fill_reason") or None, float(r.get("filled_at") or 0.0))


def merge_fills(alerts: list[dict[str, Any]], csv_fills: Iterable[Fill]) -> dict[str, Any]:
    """Attach ``a["fill"]`` (a :class:`Fill` or None) to every alert: DB columns + CSV, latest wins.

    CSV ids may be a unique prefix (>= 6 chars) of the alert id. Returns {"unmatched": [...], "ambiguous": [...]}.
    """
    by_id = {str(a["id"]): a for a in alerts}
    cands: dict[str, list[Fill]] = {k: [] for k in by_id}
    for a in alerts:
        f = fill_from_db_row(a)
        if f is not None:
            cands[str(a["id"])].append(f)
    unmatched, ambiguous = [], []
    for f in csv_fills:
        if f.signal_id in by_id:
            cands[f.signal_id].append(f)
            continue
        hits = [k for k in by_id if k.startswith(f.signal_id)]
        if len(hits) == 1:
            cands[hits[0]].append(Fill(hits[0], f.fill_status, f.taken_odds, f.taken_stake, f.fill_reason,
                                       f.filled_at))
        elif hits:
            ambiguous.append(f.signal_id)
        else:
            unmatched.append(f.signal_id)
    for k, a in by_id.items():
        a["fill"] = latest_by_id(cands[k]).get(k)
    return {"unmatched": unmatched, "ambiguous": ambiguous}
