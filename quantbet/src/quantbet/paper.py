"""Paper trading for dry runs: log would-be MIN-ODDS alerts, grade them at full time.

Assumption for grading: the bet was placed exactly at the alert's **minimum Bet365
odds** with the stake of the first tier (quarter Kelly at the minimum odds).
Totals are full-match totals (Bet365 in-play totals include goals/corners so far).
Asian quarter lines are split into two half-stakes on the neighbouring lines.
"""

from __future__ import annotations

import html
import json
import math
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from .notifier import format_min_odds_signal

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at REAL, signal_id TEXT, event_key TEXT, home TEXT, away TEXT, league TEXT,
  minute REAL, score TEXT, home_goals INT, away_goals INT, home_corners INT, away_corners INT,
  market TEXT, line REAL, selection TEXT, fair_odds REAL, p_model REAL, settlement TEXT,
  min_odds REAL, abort_floor REAL, stake REAL, tiers TEXT, triggers TEXT, ev_at_reference REAL,
  fotmob TEXT, card TEXT,
  final_home_goals INT, final_away_goals INT, final_home_corners INT, final_away_corners INT,
  result TEXT, pnl_units REAL, pnl_money REAL, graded_at REAL
);
"""
# Added for league probation (quantbet.probation); ALTERed into older DBs on open.
EXTRA_COLUMNS = (("league_slug", "TEXT"), ("liquidity_tier", "TEXT"), ("prior_fair", "REAL"), ("probation", "TEXT"))


def plain_card(sig: Any) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", format_min_odds_signal(sig)))


def settle_total(line: float, selection: str, total: float) -> list[float]:
    """Per-half-stake outcome (+1 win, 0 push, -1 loss) for an Over/Under total line."""
    frac = round((line * 4) % 4)
    halves = [line - 0.25, line + 0.25] if frac in (1, 3) else [line, line]
    out = []
    for h in halves:
        d = total - h if selection == "over" else h - total
        out.append(1.0 if d > 1e-9 else (-1.0 if d < -1e-9 else 0.0))
    return out


def result_label(parts: list[float]) -> str:
    s = sum(parts)
    if parts[0] == parts[1]:
        return {1.0: "win", 0.0: "push", -1.0: "loss"}[parts[0]]
    return "half-win" if s > 0 else "half-loss"


def pnl(parts: list[float], odds: float, stake: float) -> float:
    return sum((stake / 2) * ((odds - 1.0) if p > 0 else (0.0 if p == 0 else -1.0)) for p in parts)


class PaperBook:
    def __init__(self, db_path: str | Path, md_path: str | Path) -> None:
        self.db = sqlite3.connect(str(db_path))
        self.db.executescript(SCHEMA)
        have = {r[1] for r in self.db.execute("PRAGMA table_info(paper_trades)")}
        for col, typ in EXTRA_COLUMNS:
            if col not in have:
                self.db.execute(f"ALTER TABLE paper_trades ADD COLUMN {col} {typ}")
        self.db.commit()
        self.md_path = Path(md_path)
        self.md_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.md_path.exists():
            self.md_path.write_text("# Would-be alerts – dry run 2026-10-25 (nothing sent to Telegram)\n\n")

    def record(self, sig: Any) -> None:
        x = sig.extra or {}
        if not x.get("model_only"):
            return
        tiers = x.get("tiers") or []
        stake = float(tiers[0][1]) if tiers else 0.0
        card = plain_card(sig)
        fm = x.get("fotmob", "not_checked")
        self.db.execute(
            "INSERT INTO paper_trades (created_at, signal_id, event_key, home, away, league, minute, score, home_goals,"
            " away_goals, home_corners, away_corners, market, line, selection, fair_odds, p_model, settlement, min_odds,"
            " abort_floor, stake, tiers, triggers, ev_at_reference, fotmob, card) VALUES"
            " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), sig.signal_id, sig.event_key, sig.home, sig.away, sig.league, sig.minute, sig.score,
             x.get("home_goals"), x.get("away_goals"), x.get("home_corners"), x.get("away_corners"),
             sig.market.value, sig.line, sig.selection.value, sig.fair_odds, x.get("p_push_adjusted"),
             json.dumps(x.get("settlement")), x.get("min_odds"), x.get("abort_floor"), stake, json.dumps(tiers),
             json.dumps(x.get("triggers")), x.get("ev_at_reference"), fm, card))
        lift = x.get("lift") or {}
        self.db.execute("UPDATE paper_trades SET league_slug=?, liquidity_tier=?, prior_fair=?, probation=?"
                        " WHERE id=last_insert_rowid()",
                        (x.get("league_slug"), x.get("liquidity_tier"), lift.get("prior_fair"), x.get("probation")))
        self.db.commit()
        corners = f"{x.get('home_corners')}-{x.get('away_corners')}"
        with self.md_path.open("a") as f:
            f.write(f"## {time.strftime('%H:%M:%S', time.gmtime())} UTC – {sig.home} vs {sig.away} "
                    f"({int(sig.minute or 0)}', score {sig.score}, corners {corners})\n\n"
                    f"FotMob check: {fm} | EV at neutral reference: {x.get('ev_at_reference')}\n\n```\n{card}\n```\n\n")

    def grade(self, finals: dict[str, dict[str, Any]]) -> int:
        n = 0
        rows = self.db.execute("SELECT id, event_key, market, line, selection, min_odds, stake FROM paper_trades"
                               " WHERE result IS NULL").fetchall()
        for rid, key, market, line, sel, odds, stake in rows:
            fin = finals.get(key)
            if not fin:
                continue
            if market == "TOTAL_CORNERS":
                if fin.get("home_corners") is None or fin.get("away_corners") is None:
                    continue
                total = float(fin["home_corners"]) + float(fin["away_corners"])
            else:
                total = float(fin["home_goals"]) + float(fin["away_goals"])
            parts = settle_total(float(line), sel, total)
            self.db.execute(
                "UPDATE paper_trades SET final_home_goals=?, final_away_goals=?, final_home_corners=?,"
                " final_away_corners=?, result=?, pnl_units=?, pnl_money=?, graded_at=? WHERE id=?",
                (fin["home_goals"], fin["away_goals"], fin.get("home_corners"), fin.get("away_corners"),
                 result_label(parts), round(pnl(parts, odds, 1.0), 4), round(pnl(parts, odds, stake), 2),
                 time.time(), rid))
            n += 1
        self.db.commit()
        return n

    def report(self, path: str | Path, extra: dict[str, Any], sections: list[str] | None = None,
               title: str = "Dry-run paper-trade report – 2026-10-25") -> str:
        cur = self.db.execute("SELECT home, away, minute, score, market, line, selection, fair_odds, min_odds, stake,"
                              " settlement, result, pnl_units, pnl_money, final_home_goals, final_away_goals,"
                              " final_home_corners, final_away_corners, fotmob, triggers FROM paper_trades ORDER BY id")
        rows = cur.fetchall()
        graded = [r for r in rows if r[11] is not None]
        rec: dict[str, int] = {}
        for r in graded:
            rec[r[11]] = rec.get(r[11], 0) + 1
        staked = sum(r[9] for r in graded)
        pm = sum(r[13] for r in graded)
        pu = sum(r[12] for r in graded)
        brier, ll, nb = 0.0, 0.0, 0
        for r in graded:
            st = json.loads(r[10] or "null")
            if not st or r[11] == "push":
                continue
            w, hw, p, hl, l_ = st
            prob = (w + hw) / max(1e-9, 1 - p)
            y = 1.0 if r[11] in ("win", "half-win") else 0.0
            prob = min(max(prob, 1e-6), 1 - 1e-6)
            brier += (prob - y) ** 2
            ll += -(y * math.log(prob) + (1 - y) * math.log(1 - prob))
            nb += 1
        L = [f"# {title}", "",
             f"Generated {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC. Nothing was sent to Telegram; no bets placed.", "",
             "**Sample size is tiny – these numbers say nothing about long-run edge.**", "",
             f"* Alerts logged: {len(rows)} (graded {len(graded)})",
             f"* Record: " + (", ".join(f"{k} {v}" for k, v in sorted(rec.items())) or "–"),
             f"* Flat 1-unit P&L at min odds: {pu:+.2f} u over {len(graded)} bets"
             + (f" (ROI {100 * pu / len(graded):+.1f}%)" if graded else ""),
             f"* Quarter-Kelly stake P&L at min odds: {pm:+.2f} on {staked:.2f} staked"
             + (f" (ROI {100 * pm / staked:+.1f}%)" if staked else ""),
             f"* Brier (win/half-win vs loss/half-loss, pushes excluded, n={nb}): "
             + (f"{brier / nb:.4f}, log-loss {ll / nb:.4f}" if nb else "–"), ""]
        for k, v in extra.items():
            L.append(f"* {k}: {v}")
        L += ["", "| match | min | score | line | fair | min odds | stake | FT goals | FT corners | result | P&L u | P&L $ | FotMob | triggers |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in rows:
            mk = "Corners" if r[4] == "TOTAL_CORNERS" else "Goals"
            ftg = f"{r[14]}-{r[15]}" if r[14] is not None else ""
            ftc = f"{r[16]}-{r[17]}" if r[16] is not None else ""
            L.append(f"| {r[0]} v {r[1]} | {int(r[2] or 0)} | {r[3]} | {r[6].title()} {r[5]} {mk} | {r[7]:.2f} | {r[8]:.2f} |"
                     f" {r[9]:.0f} | {ftg} | {ftc} | {r[11] or 'open'} | {'' if r[12] is None else f'{r[12]:+.2f}'} |"
                     f" {'' if r[13] is None else f'{r[13]:+.2f}'} | {r[18]} | {', '.join(json.loads(r[19] or '[]'))} |")
        if sections:
            L += [""] + list(sections)
        text = "\n".join(L) + "\n"
        Path(path).write_text(text)
        return text


__all__ = ["PaperBook", "settle_total", "result_label", "pnl", "plain_card"]
