#!/usr/bin/env python
"""Extract per-half corner counts from StatsBomb open data (free, CC BY-NC-SA-style licence; github.com/statsbomb/open-data).

Downloads matches/<comp>/<season>.json and a capped sample of events/<match>.json from raw.githubusercontent.com,
keeps only a compact summary (data/statsbomb/corner_summary.json) and discards the event files.
Per match: H1/H2 corners, H1/H2 played minutes (last event timestamp per period), goals by minute,
and corner minutes (for lead-state rates). Low concurrency, identifiable User-Agent.

    python scripts/statsbomb_corners.py --per-league 40
"""
from __future__ import annotations

import argparse
import json
import random
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = "https://raw.githubusercontent.com/statsbomb/open-data/master/data"
UA = "quantbet-signal-engine/0.2 (personal calibration, read-only)"
COMPS = [(2, 27, "EPL 2015/16"), (11, 27, "La Liga 2015/16"), (12, 27, "Serie A 2015/16"),
         (9, 27, "Bundesliga 2015/16"), (7, 27, "Ligue 1 2015/16")]


def get(url: str) -> object:
    for i in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", None)
            if code in (403, 404):
                raise
            time.sleep(2 ** i + random.random())
    raise RuntimeError(url)


def summarise(mid: int) -> dict:
    ev = get(f"{BASE}/events/{mid}.json")
    out = {"match_id": mid, "corners": {1: 0, 2: 0}, "end": {1: 45.0, 2: 45.0}, "corner_min": [], "goals": []}
    teams: list[str] = []
    for e in ev:
        per = e.get("period")
        if per not in (1, 2):
            continue
        t = e["minute"] + e["second"] / 60.0
        rel = t if per == 1 else t - 45.0
        out["end"][per] = max(out["end"][per], rel)
        tm = e.get("team", {}).get("name")
        if tm and tm not in teams:
            teams.append(tm)
        if e["type"]["name"] == "Pass" and e.get("pass", {}).get("type", {}).get("name") == "Corner":
            out["corners"][per] += 1
            out["corner_min"].append([per, round(t, 2), tm])
        if (e["type"]["name"] == "Shot" and e.get("shot", {}).get("outcome", {}).get("name") == "Goal"):
            out["goals"].append([per, round(t, 2), tm])
        if e["type"]["name"] == "Own Goal For":
            out["goals"].append([per, round(t, 2), tm])
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-league", type=int, default=40)
    ap.add_argument("--out", default="data/statsbomb/corner_summary.json")
    a = ap.parse_args()
    rng = random.Random(42)
    todo = []
    for cid, sid, name in COMPS:
        ms = get(f"{BASE}/matches/{cid}/{sid}.json")
        ms = sorted(ms, key=lambda m: m["match_id"])
        for m in rng.sample(ms, min(a.per_league, len(ms))):
            todo.append((name, m["match_id"], m["home_team"]["home_team_name"], m["away_team"]["away_team_name"],
                         m["home_score"], m["away_score"]))
    res = []
    with ThreadPoolExecutor(4) as ex:
        for meta, s in zip(todo, ex.map(lambda t: summarise(t[1]), todo)):
            s.update(league=meta[0], home=meta[2], away=meta[3], fthg=meta[4], ftag=meta[5])
            res.append(s)
            if len(res) % 20 == 0:
                print(len(res), flush=True)
    Path(a.out).write_text(json.dumps({"source": BASE, "matches": res}))
    print("done", len(res))


if __name__ == "__main__":
    main()
