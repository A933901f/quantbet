"""League probation: out-of-sample (OOS) track record of the leagues added by the v4.3-global layer.

Why
---
Engine B (v4.2 maths) was calibrated on the **original set** only. The v4.3-global layer feeds it many more
leagues with league-average baselines. Until a *newly added* tier proves itself out of sample, its alert cards
carry ``[PROBATION: n/150]``.

Definitions (documented rule, not a frozen Engine-B parameter)
-------------------------------------------------------------
* **Original calibrated set** (never on probation): the v4.2 live target list = ESPN ``DEFAULT_LEAGUES``
  (eng.1, esp.1, ger.1, ita.1, fra.1, uefa.champions, uefa.europa) + ``CUP_LEAGUES`` (the ``--cups`` list:
  UCL, UEL, UECL, FA Cup, Carabao Cup, Copa del Rey, DFB-Pokal, Coppa Italia, Coupe de France).
* **Newly added global tiers**: every other slug, grouped by its liquidity tier from config/leagues.json
  (``TIER_1`` = usa.1, bra.1; ``TIER_2`` = second tiers / mid leagues; ``TIER_3`` = thin leagues and anything
  auto-discovered). The probation group of a bet is ``NEW_<tier>``; core-league bets are group ``CORE_V42``
  (tracked for reference, never tagged).
* **Out-of-sample graded bet**: a paper trade with a result (win/half-win/push/half-loss/loss) whose
  ``created_at`` is at/after the ``frozen_at`` of the v4.3-global params set (so the frozen rules were never fit
  on it). De-duplicated by ``signal_id`` across paper DBs.
* **n** = OOS graded bets in the group (pushes included).
* **BSS vs prior** = ``1 - Brier(model) / Brier(prior)`` over OOS decided bets (pushes excluded; win/half-win = 1,
  loss/half-loss = 0) that carry a prior. ``p_model = 1/fair_odds``, ``p_prior = 1/prior_fair`` (the ESPN/DraftKings
  pre-match prior repriced at the alert's state, shown on the card as "Prior fair X -> model Y"); both are on
  the same push-adjusted fair-odds scale.
* **Graduation**: ``n >= 150 AND BSS > 0`` (BSS undefined counts as not > 0). Until then the card shows
  ``[PROBATION: n/150]``. Per-league rows are kept for diagnostics; the tag uses the tier group.

Update counts with ``python scripts/update_probation.py`` (reads paper DBs read-only).
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .espn import CUP_LEAGUES, DEFAULT_LEAGUES, LEAGUE_NAMES

PROBATION_N = 150
CORE_GROUP = "CORE_V42"
ORIGINAL_CALIBRATED_SLUGS: frozenset[str] = frozenset(DEFAULT_LEAGUES) | frozenset(CUP_LEAGUES)
TIERS = ("TIER_1", "TIER_2", "TIER_3")

SCHEMA = """
CREATE TABLE IF NOT EXISTS probation_bets (
  signal_id TEXT PRIMARY KEY, source_db TEXT, created_at REAL, league TEXT, league_slug TEXT, tier TEXT,
  grp TEXT, market TEXT, line REAL, selection TEXT, fair_odds REAL, prior_fair REAL, result TEXT,
  y REAL, p_model REAL, p_prior REAL, imported_at REAL
);
CREATE TABLE IF NOT EXISTS league_probation (
  scope TEXT NOT NULL,             -- 'group' (NEW_TIER_n / CORE_V42) or 'league' (slug or league name)
  key TEXT NOT NULL,
  grp TEXT, n_graded INTEGER, n_scored INTEGER, brier_model REAL, brier_prior REAL, bss REAL,
  on_probation INTEGER, updated_at REAL,
  PRIMARY KEY (scope, key)
);
"""

_TIER_TAG = re.compile(r"\[LIQUIDITY: TIER (\d)")
_PRIOR = re.compile(r"Prior fair ([0-9.]+)\s*->")


def probation_group(slug: str | None, tier: str | None) -> str:
    """``CORE_V42`` for the original calibrated set, else ``NEW_<tier>`` (unknown tier -> TIER_3)."""
    if slug and slug in ORIGINAL_CALIBRATED_SLUGS:
        return CORE_GROUP
    t = tier if tier in TIERS else "TIER_3"
    return f"NEW_{t}"


def is_new_league(slug: str | None) -> bool:
    return probation_group(slug, None) != CORE_GROUP


def oos_cutoff(params_path: str | Path = "config/engine_b_params.json", rules: str = "v4.3-global") -> float:
    """``frozen_at`` of the rules set (UTC epoch seconds); bets created at/after it are out of sample."""
    raw = json.loads(Path(params_path).read_text())
    fs = (raw.get("frozen_sets") or {}).get(rules) or (raw if raw.get("rules_version") == rules else {})
    ts = fs.get("frozen_at") or raw.get("frozen_at")
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def _slug_from_name(name: str | None, registry_names: Mapping[str, str] | None = None) -> str | None:
    if not name:
        return None
    names = dict(LEAGUE_NAMES)
    names.update(registry_names or {})
    n = name.strip().lower()
    for slug, nm in names.items():
        if nm and nm.strip().lower() == n:
            return slug
    return None


def outcome(result: str | None) -> float | None:
    """1 for win/half-win, 0 for loss/half-loss, None for push/ungraded."""
    return {"win": 1.0, "half-win": 1.0, "loss": 0.0, "half-loss": 0.0}.get(result or "")


class ProbationBook:
    def __init__(self, db_path: str | Path = "data/league_probation.sqlite3", n_required: int = PROBATION_N,
                 cache_ttl_s: float = 300.0) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(db_path))
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.n_required = n_required
        self.cache_ttl_s = cache_ttl_s
        self._cache: tuple[float, dict[str, tuple[int, float | None]]] | None = None

    # -- import ---------------------------------------------------------------------------------
    def import_paper_db(self, paper_db: str | Path, cutoff: float,
                        registry: Any | None = None) -> int:
        """Copy OOS graded paper trades from ``paper_db`` (opened read-only). Returns rows upserted."""
        src = sqlite3.connect(f"file:{Path(paper_db).resolve()}?mode=ro", uri=True)
        try:
            cols = {r[1] for r in src.execute("PRAGMA table_info(paper_trades)")}
            if not cols:
                return 0
            extra = [c for c in ("league_slug", "liquidity_tier", "prior_fair") if c in cols]
            q = ("SELECT signal_id, created_at, league, market, line, selection, fair_odds, result, card"
                 + "".join(f", {c}" for c in extra) + " FROM paper_trades WHERE result IS NOT NULL AND created_at >= ?")
            rows = src.execute(q, (cutoff,)).fetchall()
        finally:
            src.close()
        reg_names = {s: i.name for s, i in getattr(registry, "leagues", {}).items()} if registry is not None else {}
        n = 0
        now = time.time()
        for r in rows:
            sig, created, league, market, line, sel, fair, result, card = r[:9]
            ex = dict(zip(extra, r[9:]))
            card = card or ""
            slug = ex.get("league_slug") or _slug_from_name(league, reg_names)
            tier = ex.get("liquidity_tier")
            if not tier:
                m = _TIER_TAG.search(card)
                tier = f"TIER_{m.group(1)}" if m else (registry.tier_of(slug) if registry is not None and slug else None)
            prior = ex.get("prior_fair")
            if prior is None:
                m = _PRIOR.search(card)
                prior = float(m.group(1)) if m else None
            grp = probation_group(slug, tier)
            y = outcome(result)
            pm = 1.0 / float(fair) if fair else None
            pp = 1.0 / float(prior) if prior else None
            self.db.execute("INSERT OR REPLACE INTO probation_bets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (sig, str(paper_db), created, league, slug, tier, grp, market, line, sel, fair, prior,
                             result, y, pm, pp, now))
            n += 1
        self.db.commit()
        self._cache = None
        return n

    # -- aggregate --------------------------------------------------------------------------------
    @staticmethod
    def _stats(rows: Iterable[tuple[float | None, float | None, float | None]]) -> tuple[int, int, float | None,
                                                                                         float | None, float | None]:
        n = k = 0
        bm = bp = 0.0
        for y, pm, pp in rows:
            n += 1
            if y is None or pm is None or pp is None:
                continue
            k += 1
            bm += (pm - y) ** 2
            bp += (pp - y) ** 2
        if not k:
            return n, 0, None, None, None
        bm, bp = bm / k, bp / k
        bss = (1.0 - bm / bp) if bp > 0 else None
        return n, k, bm, bp, bss

    def recompute(self) -> list[dict[str, Any]]:
        now = time.time()
        out = []
        self.db.execute("DELETE FROM league_probation")
        for scope, col in (("group", "grp"), ("league", "COALESCE(league_slug, league)")):
            keys = [r[0] for r in self.db.execute(f"SELECT DISTINCT {col} FROM probation_bets")]
            for key in keys:
                rows = self.db.execute(f"SELECT y, p_model, p_prior, grp FROM probation_bets WHERE {col} IS ?",
                                       (key,)).fetchall()
                grp = rows[0][3] if scope == "league" else key
                n, k, bm, bp, bss = self._stats((r[0], r[1], r[2]) for r in rows)
                onp = int(grp != CORE_GROUP and not (n >= self.n_required and bss is not None and bss > 0))
                self.db.execute("INSERT INTO league_probation VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (scope, key, grp, n, k, bm, bp, bss, onp, now))
                out.append({"scope": scope, "key": key, "grp": grp, "n_graded": n, "n_scored": k,
                            "brier_model": bm, "brier_prior": bp, "bss": bss, "on_probation": bool(onp)})
        self.db.commit()
        self._cache = None
        return out

    # -- lookup / card tag --------------------------------------------------------------------------
    def _groups(self) -> dict[str, tuple[int, float | None]]:
        now = time.time()
        if self._cache is None or now - self._cache[0] > self.cache_ttl_s:
            rows = self.db.execute("SELECT key, n_graded, bss FROM league_probation WHERE scope='group'").fetchall()
            self._cache = (now, {k: (int(n or 0), b) for k, n, b in rows})
        return self._cache[1]

    def status(self, slug: str | None, tier: str | None) -> tuple[str, int, float | None, bool]:
        """(group, n, bss, on_probation)."""
        grp = probation_group(slug, tier)
        n, bss = self._groups().get(grp, (0, None))
        onp = grp != CORE_GROUP and not (n >= self.n_required and bss is not None and bss > 0)
        return grp, n, bss, onp

    def tag(self, slug: str | None, tier: str | None) -> str:
        """``[PROBATION: n/150]`` for a newly added tier that has not graduated, else ``""``."""
        _, n, _, onp = self.status(slug, tier)
        return f"[PROBATION: {n}/{self.n_required}]" if onp else ""


def update_from_paper_dbs(book: ProbationBook, paper_dbs: Iterable[str | Path], cutoff: float,
                          registry: Any | None = None) -> tuple[int, list[dict[str, Any]]]:
    n = sum(book.import_paper_db(p, cutoff, registry) for p in paper_dbs)
    return n, book.recompute()


__all__ = ["CORE_GROUP", "ORIGINAL_CALIBRATED_SLUGS", "PROBATION_N", "ProbationBook", "is_new_league", "oos_cutoff",
           "outcome", "probation_group", "update_from_paper_dbs"]
