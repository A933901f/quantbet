"""Short real live dry-run: ESPN only, no Telegram send - prints states and would-be alerts.

    python scripts/live_dry_run.py --minutes 3 [--leagues ita.1,eng.1]

Uses the same polite settings as production (honest UA from QB_USER_AGENT,
>= 15 s per URL, concurrency 2, cache, backoff). Never contacts bet365.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quantbet import notifier as notifier_mod  # noqa: E402
from quantbet.config import Settings  # noqa: E402
from quantbet.main import run_free, setup_logging  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=3.0)
    ap.add_argument("--leagues", default="")
    a = ap.parse_args()
    kw = dict(dry_run=True, enable_the_odds_api=False,
              db_path=Path("data/dryrun.sqlite3"), http_cache_path=Path("data/dryrun_http.sqlite3"))
    if a.leagues:
        kw["espn_leagues"] = [x.strip() for x in a.leagues.split(",") if x.strip()]
    s = Settings(**kw)  # type: ignore[arg-type]
    setup_logging("INFO", as_json=True)

    orig = notifier_mod.TelegramNotifier.send_text

    async def show(self, text: str) -> bool:  # print the would-be Telegram message as plain text
        plain = html.unescape(re.sub(r"<[^>]+>", "", text))
        print("\n===== WOULD SEND (dry-run) =====\n" + plain + "\n================================", flush=True)
        return await orig(self, text)

    notifier_mod.TelegramNotifier.send_text = show  # type: ignore[method-assign]
    logging.getLogger("quantbet.notifier").setLevel(logging.WARNING)
    asyncio.run(run_free(s, asyncio.Event(), max_runtime_s=a.minutes * 60))


if __name__ == "__main__":
    main()
