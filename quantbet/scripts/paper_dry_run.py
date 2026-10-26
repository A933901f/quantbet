"""Long live dry run with paper trading (no Telegram send).

    nohup python scripts/paper_dry_run.py --until 2026-10-25T18:45 --kickoff-cutoff 2026-10-25T16:35 &

* ESPN soccer/all/scoreboard every 60 s + 2-5 s jitter (honest UA, single host, backoff on 403).
* Every would-be alert -> dryrun/alerts_<date>.md (full card) + SQLite paper_trades.
* After FT: final goals/corners recorded, alerts graded at min odds; report rewritten.
* Stops at --until, or earlier once every target match kicking off before
  --kickoff-cutoff has finished.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quantbet import notifier as notifier_mod  # noqa: E402
from quantbet.config import Settings  # noqa: E402
from quantbet.heartbeat import HeartbeatMonitor, prevent_sleep_windows  # noqa: E402
from quantbet.main import run_free, setup_logging  # noqa: E402
from quantbet.paper import PaperBook  # noqa: E402

log = logging.getLogger("quantbet.paper")


def _utc(s: str) -> datetime:
    d = datetime.fromisoformat(s.replace("Z", ""))
    return d.replace(tzinfo=timezone.utc)


class Hooks:
    def __init__(self, book: PaperBook, report: Path, cutoff: datetime, until: datetime,
                 monitor: HeartbeatMonitor | None = None, title: str = "Dry-run paper-trade report",
                 kickoff_from: datetime | None = None) -> None:
        self.book, self.report, self.cutoff, self.until = book, report, cutoff, until
        self.monitor, self.title, self.kickoff_from = monitor, title, kickoff_from
        self.n_alerts = 0
        self.started = time.time()

    async def on_alerts(self, sigs, st) -> None:  # type: ignore[no-untyped-def]
        sched = getattr(getattr(self, "poller", None), "scheduled", {})
        for s in sigs:
            v = sched.get(s.event_key)
            if self.kickoff_from is not None and (v is None or not self._is_target(v)):
                log.info("alert outside target kick-off window - not recorded", extra={"event": s.event_key})
                continue
            self.book.record(s)
            self.n_alerts += 1
            log.info("paper alert recorded", extra={"event": s.event_key, "line": s.line, "market": s.market.value,
                                                    "min_odds": (s.extra or {}).get("min_odds")})

    def _extra(self, poller, fetcher) -> dict:  # type: ignore[no-untyped-def]
        targets = {k: v for k, v in poller.scheduled.items() if self._is_target(v)}
        return {
            "Alert rules": self.rules,
            "Kick-off window (UTC)": f"{self.kickoff_from:%H:%M} -> {self.cutoff:%H:%M}" if self.kickoff_from else f"<= {self.cutoff:%H:%M}",
            "Target matches": "; ".join(f"{v['home']} v {v['away']} ({v['start'][11:16]})" for v in targets.values()) or "–",
            "Scoreboard polls": getattr(poller, "n_polls", "?"),
            "Run window (UTC)": f"{datetime.fromtimestamp(self.started, timezone.utc):%H:%M} -> {datetime.now(timezone.utc):%H:%M}",
            "Target matches (kick-off <= cutoff) seen": len(targets),
            "Finished (FT recorded)": sum(1 for k in targets if k in poller.finals),
            "HTTP requests": fetcher.n_requests,
            "Refusals (host status -> count)": dict(fetcher.refusals) or "none",
            "Final results": "; ".join(f"{v['home']} {v['home_goals']}-{v['away_goals']} {v['away']} "
                                       f"(corners {v.get('home_corners')}-{v.get('away_corners')})"
                                       for k, v in poller.finals.items() if k in targets) or "–",
        }

    rules = "v3"

    def _is_target(self, v: dict) -> bool:  # type: ignore[type-arg]
        if not v.get("start"):
            return False
        ko = _utc(v["start"][:16])
        return ko <= self.cutoff and (self.kickoff_from is None or ko >= self.kickoff_from)

    def _sections(self) -> list[str]:
        if self.monitor is None:
            return []
        poller = getattr(self, "poller", None)
        fx = {}
        for k, v in (getattr(poller, "scheduled", {}) or {}).items():
            if self._is_target(v):
                ls = (getattr(poller, "last_seen", {}) or {}).get(k, {})
                fx[k] = {"match": f"{v.get('home')} v {v.get('away')}", "kickoff_ts": _utc(v["start"][:16]).timestamp(),
                         "minute": ls.get("minute")}
        return ["## Polling gaps (heartbeat monitor, threshold 180 s)", ""] + self.monitor.report_lines(self.started - 300, fx)

    def _write(self, poller, fetcher) -> None:  # type: ignore[no-untyped-def]
        self.book.grade(poller.finals)
        self.book.report(self.report, self._extra(poller, fetcher), self._sections(), self.title)

    async def tick(self, poller, fetcher, stop) -> None:  # type: ignore[no-untyped-def]
        self.poller = poller
        while not stop.is_set():
            try:
                self._write(poller, fetcher)
                targets = [k for k, v in poller.scheduled.items() if self._is_target(v)]
                if targets and all(k in poller.finals for k in targets):
                    log.info("all target matches finished - stopping", extra={"n": len(targets)})
                    stop.set()
                if datetime.now(timezone.utc) >= self.until:
                    stop.set()
            except Exception:  # noqa: BLE001
                log.exception("paper tick failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    async def final(self, poller, fetcher) -> None:  # type: ignore[no-untyped-def]
        self._write(poller, fetcher)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", required=True)
    ap.add_argument("--kickoff-cutoff", required=True)
    ap.add_argument("--date", default="2026-10-25")
    ap.add_argument("--tag", default="", help="suffix for output files, e.g. 'evening' -> report_<date>_evening.md")
    ap.add_argument("--kickoff-from", default=None, help="ignore target matches kicking off before this (UTC)")
    a = ap.parse_args()
    until, cutoff = _utc(a.until), _utc(a.kickoff_cutoff)
    out = ROOT / "dryrun"
    out.mkdir(exist_ok=True)
    s = Settings(dry_run=True, enable_the_odds_api=False, db_path=ROOT / "data/dryrun.sqlite3",  # type: ignore[call-arg]
                 http_cache_path=ROOT / "data/dryrun_http.sqlite3", espn_summary_db_path=ROOT / "data/espn_summary.sqlite3")
    setup_logging("INFO", as_json=True)

    async def no_send(self, text: str) -> bool:  # never contacts Telegram
        return True

    notifier_mod.TelegramNotifier.send_text = no_send  # type: ignore[method-assign]
    sfx = f"{a.date}_{a.tag}" if a.tag else a.date
    book = PaperBook(out / (f"paper_trades_{sfx}.sqlite3" if a.tag else "paper_trades.sqlite3"), out / f"alerts_{sfx}.md")
    monitor = HeartbeatMonitor(out / "heartbeat.sqlite3", run_id=sfx)
    win = prevent_sleep_windows()
    hooks = Hooks(book, out / f"report_{sfx}.md", cutoff, until, monitor,
                  title=f"Dry-run paper-trade report – {a.date}" + (f" ({a.tag})" if a.tag else ""),
                  kickoff_from=_utc(a.kickoff_from) if a.kickoff_from else None)
    hooks.rules = s.alert_rules
    log.info("paper dry run starting", extra={"tag": sfx, "rules": s.alert_rules, "windows_keepalive": win,
                                              "inhibited_by": __import__("os").environ.get("QB_KEEPALIVE", "none")})
    max_rt = max(60.0, (until - datetime.now(timezone.utc)).total_seconds())
    asyncio.run(run_free(s, asyncio.Event(), max_runtime_s=max_rt, hooks=hooks, heartbeat=monitor))


if __name__ == "__main__":
    main()
