"""One-shot polite ESPN probe: soccer/all scoreboard + per-league scoreboards (via PoliteFetcher).

Saves raw payloads to data/espn_probe/<slug>.json and prints a JSON summary per slug
(league id/name, n events, live, stats keys present). Read-only; honest UA; cached.
Usage: python scripts/espn_probe.py [slug ...]   (default: every slug in config/leagues.json)
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quantbet.config import get_settings  # noqa: E402
from quantbet.espn import EspnEndpoints, league_id_from_scoreboard  # noqa: E402
from quantbet.polite_http import PoliteFetcher, ResponseCache  # noqa: E402

STAT_KEYS = ("wonCorners", "totalShots", "shotsOnTarget")


def summarise(payload: dict) -> dict:
    evs = payload.get("events") or []
    out = []
    for e in evs:
        comp = (e.get("competitions") or [{}])[0]
        cs = {c.get("homeAway"): c for c in comp.get("competitors") or []}
        st = e.get("status") or {}
        names = set()
        for c in cs.values():
            names |= {s.get("name") for s in c.get("statistics") or []}
        out.append({
            "id": e.get("id"), "uid": e.get("uid"), "date": e.get("date"),
            "home": ((cs.get("home") or {}).get("team") or {}).get("displayName"),
            "away": ((cs.get("away") or {}).get("team") or {}).get("displayName"),
            "score": f"{(cs.get('home') or {}).get('score')}-{(cs.get('away') or {}).get('score')}",
            "state": (st.get("type") or {}).get("state"), "status": (st.get("type") or {}).get("name"),
            "clock": st.get("displayClock"),
            "stats_keys": all(k in names for k in STAT_KEYS),
            "stats": {ha: {s.get("name"): s.get("displayValue") for s in (c.get("statistics") or []) if s.get("name") in STAT_KEYS}
                      for ha, c in cs.items()},
        })
    return {"events": out}


async def main(slugs: list[str]) -> None:
    s = get_settings()
    ep = EspnEndpoints(base=s.espn_base)
    res: dict[str, dict] = {}
    async with PoliteFetcher(s.user_agent, ResponseCache(s.http_cache_path), min_interval_s=15, global_concurrency=1,
                             global_min_gap_s=1.5, jitter_s=2.0) as f:
        for slug in ["all", *slugs]:
            r = await f.get_json(ep.scoreboard(slug), cache_ttl_s=120)
            info = {"status": r.status, "ok": r.ok, "error": r.error, "from_cache": r.from_cache}
            if r.ok and isinstance(r.data, dict):
                (ROOT / "data/espn_probe" / f"{slug}.json").write_text(json.dumps(r.data))
                lg = (r.data.get("leagues") or [{}])[0]
                info.update(league_id=league_id_from_scoreboard(r.data), name=lg.get("name"), slug_echo=lg.get("slug"),
                            **summarise(r.data))
            res[slug] = info
            if r.blocked:
                print("BLOCKED - stopping", slug, r.error, file=sys.stderr)
                break
        print(json.dumps({"requests": f.n_requests, "refusals": f.refusals}), file=sys.stderr)
    (ROOT / "data/espn_probe/_summary.json").write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    slugs = sys.argv[1:]
    if not slugs:
        slugs = [x["slug"] for x in json.loads((ROOT / "config/leagues.json").read_text())["leagues"]]
    asyncio.run(main(slugs))
