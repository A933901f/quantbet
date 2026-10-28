"""SQLite paper-trading log with closing-line value (CLV) and ROI/yield reporting."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from .models import Signal

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id              TEXT PRIMARY KEY,
    created_at      REAL NOT NULL,
    urgency         TEXT NOT NULL,
    engine          TEXT NOT NULL,
    event_key       TEXT NOT NULL,
    home            TEXT NOT NULL,
    away            TEXT NOT NULL,
    league          TEXT NOT NULL,
    commence_time   TEXT,
    is_live         INTEGER NOT NULL,
    minute          REAL,
    score           TEXT,
    market          TEXT NOT NULL,
    line            REAL,
    selection       TEXT NOT NULL,
    bookmaker       TEXT NOT NULL,
    odds            REAL NOT NULL,
    p_true          REAL NOT NULL,
    fair_odds       REAL NOT NULL,
    min_odds        REAL NOT NULL,
    ev              REAL NOT NULL,
    kelly_fraction  REAL NOT NULL,
    stake           REAL NOT NULL,
    stake_units     REAL NOT NULL,
    high_limit_risk INTEGER NOT NULL,
    method          TEXT,
    sharp_source    TEXT,
    extra           TEXT,
    closing_fair_odds   REAL,
    closing_target_odds REAL,
    closing_at          REAL,
    clv                 REAL,
    result          TEXT,
    pnl             REAL,
    settled_at      REAL
);
CREATE INDEX IF NOT EXISTS ix_alerts_event ON alerts(event_key);
CREATE INDEX IF NOT EXISTS ix_alerts_open ON alerts(closing_fair_odds, result);
"""

# Plumbing (forward run): manual Bet365 fill record, added to existing DBs by a safe column migration.
FILL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("taken_odds", "REAL"),
    ("taken_stake", "REAL"),
    ("fill_status", "TEXT"),  # 'filled' | 'skipped' (NULL = not recorded yet)
    ("fill_reason", "TEXT"),
    ("filled_at", "REAL"),
)

# Paper-only alerts (v4.5 cards) are logged for grading but never staked: excluded from the staked report.
STAKED_ONLY = ("(CASE WHEN extra IS NOT NULL AND json_valid(extra) "
               "THEN COALESCE(json_extract(extra, '$.paper_only'), 0) ELSE 0 END) = 0")


def migrate_fill_columns(conn: Any) -> list[str]:
    """Add the fill columns to an existing ``alerts`` table (sync ``sqlite3`` connection). Returns added names."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(alerts)").fetchall()}
    added = []
    for name, typ in FILL_COLUMNS:
        if have and name not in have:
            conn.execute(f"ALTER TABLE alerts ADD COLUMN {name} {typ}")
            added.append(name)
    return added


_PAYOUT = {"win": 1.0, "half_win": 0.5, "push": 0.0, "half_loss": -0.5, "loss": -1.0, "void": 0.0}


@dataclass(frozen=True, slots=True)
class PerformanceReport:
    """Aggregate paper-trading performance."""

    n_alerts: int
    n_settled: int
    turnover: float
    profit: float
    yield_pct: float  # profit / turnover
    roi_pct: float  # profit / starting bankroll
    n_with_clv: int
    avg_clv_pct: float  # mean(odds / closing_fair_odds - 1)
    beat_close_pct: float  # share of bets with CLV > 0
    avg_ev_pct: float


class AlertDatabase:
    """Async SQLite store (aiosqlite). Use as an async context manager."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None

    async def __aenter__(self) -> AlertDatabase:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def connect(self) -> None:
        """Open the connection and create the schema."""
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(SCHEMA)
        cur = await self._db.execute("PRAGMA table_info(alerts)")
        have = {r[1] for r in await cur.fetchall()}
        for name, typ in FILL_COLUMNS:
            if name not in have:
                await self._db.execute(f"ALTER TABLE alerts ADD COLUMN {name} {typ}")
        await self._db.commit()

    async def close(self) -> None:
        """Close the connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("database not connected")
        return self._db

    async def insert_signal(self, s: Signal) -> None:
        """Log a triggered alert (paper trade at the alerted odds and stake)."""
        await self.db.execute(
            """INSERT OR IGNORE INTO alerts (id, created_at, urgency, engine, event_key, home, away,
            league, commence_time, is_live, minute, score, market, line, selection, bookmaker, odds,
            p_true, fair_odds, min_odds, ev, kelly_fraction, stake, stake_units, high_limit_risk,
            method, sharp_source, extra) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                s.signal_id, s.created_at, s.urgency.value, s.engine, s.event_key, s.home, s.away,
                s.league, s.commence_time.isoformat() if s.commence_time else None, int(s.is_live),
                s.minute, s.score, s.market.value, s.line, s.selection.value, s.bookmaker,
                s.bookmaker_odds, s.p_true, s.fair_odds, s.min_acceptable_odds, s.ev,
                s.kelly_fraction, s.stake_currency, s.stake_units, int(s.high_limit_risk), s.method,
                s.sharp_source, json.dumps(s.extra, default=str),
            ),
        )
        await self.db.commit()

    async def alerts_needing_close(self, now_iso: str) -> list[dict[str, Any]]:
        """Pre-match alerts whose kick-off has passed and that have no closing line yet."""
        cur = await self.db.execute(
            """SELECT * FROM alerts WHERE closing_fair_odds IS NULL AND is_live = 0
               AND commence_time IS NOT NULL AND commence_time <= ?""",
            (now_iso,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def update_closing_line(
        self, alert_id: str, closing_fair_odds: float, closing_target_odds: float | None = None
    ) -> float:
        """Store the closing no-vig sharp odds and compute ``CLV = odds/close - 1``."""
        cur = await self.db.execute("SELECT odds FROM alerts WHERE id = ?", (alert_id,))
        row = await cur.fetchone()
        if row is None:
            raise KeyError(alert_id)
        clv = row["odds"] / closing_fair_odds - 1.0
        await self.db.execute(
            """UPDATE alerts SET closing_fair_odds=?, closing_target_odds=?, closing_at=?, clv=?
               WHERE id=?""",
            (closing_fair_odds, closing_target_odds, time.time(), clv, alert_id),
        )
        await self.db.commit()
        return clv

    async def settle(self, alert_id: str, result: str) -> float:
        """Settle with ``win|half_win|push|half_loss|loss|void``; returns P&L."""
        if result not in _PAYOUT:
            raise ValueError(f"bad result {result!r}")
        cur = await self.db.execute("SELECT odds, stake FROM alerts WHERE id = ?", (alert_id,))
        row = await cur.fetchone()
        if row is None:
            raise KeyError(alert_id)
        mult = _PAYOUT[result]
        pnl = row["stake"] * (mult * (row["odds"] - 1.0) if mult > 0 else mult)
        await self.db.execute(
            "UPDATE alerts SET result=?, pnl=?, settled_at=? WHERE id=?",
            (result, pnl, time.time(), alert_id),
        )
        await self.db.commit()
        return pnl

    async def record_fill(self, alert_id: str, status: str, taken_odds: float | None = None,
                          taken_stake: float | None = None, reason: str | None = None,
                          filled_at: float | None = None) -> None:
        """Store the user's manual Bet365 fill (``filled``) or skip (``skipped``) for one alert."""
        if status not in ("filled", "skipped"):
            raise ValueError(f"bad fill status {status!r}")
        cur = await self.db.execute(
            "UPDATE alerts SET taken_odds=?, taken_stake=?, fill_status=?, fill_reason=?, filled_at=? WHERE id=?",
            (taken_odds, taken_stake, status, reason, time.time() if filled_at is None else filled_at, alert_id),
        )
        if cur.rowcount == 0:
            raise KeyError(alert_id)
        await self.db.commit()

    async def report(self, starting_bankroll: float) -> PerformanceReport:
        """ROI, yield and CLV summary over the STAKED alerts (paper-only alerts are excluded)."""
        q = """SELECT COUNT(*) n,
                  SUM(result IS NOT NULL) n_settled,
                  COALESCE(SUM(CASE WHEN result IS NOT NULL AND result != 'void' THEN stake END), 0) turnover,
                  COALESCE(SUM(pnl), 0) profit,
                  SUM(clv IS NOT NULL) n_clv,
                  AVG(clv) avg_clv,
                  AVG(CASE WHEN clv IS NOT NULL THEN (clv > 0) END) beat,
                  AVG(ev) avg_ev
               FROM alerts WHERE """ + STAKED_ONLY
        cur = await self.db.execute(q)
        r = await cur.fetchone()
        assert r is not None
        turnover = float(r["turnover"] or 0.0)
        profit = float(r["profit"] or 0.0)
        return PerformanceReport(
            n_alerts=int(r["n"] or 0),
            n_settled=int(r["n_settled"] or 0),
            turnover=turnover,
            profit=profit,
            yield_pct=100.0 * profit / turnover if turnover else 0.0,
            roi_pct=100.0 * profit / starting_bankroll if starting_bankroll else 0.0,
            n_with_clv=int(r["n_clv"] or 0),
            avg_clv_pct=100.0 * float(r["avg_clv"] or 0.0),
            beat_close_pct=100.0 * float(r["beat"] or 0.0),
            avg_ev_pct=100.0 * float(r["avg_ev"] or 0.0),
        )
