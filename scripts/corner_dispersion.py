#!/usr/bin/env python
"""Measure corner-count dispersion (variance/mean) from a Football-Data CSV (offline, no network).

    python scripts/corner_dispersion.py [data/football_data/E0_2526.csv]

Reports:
* raw dispersion of total corners per match (HC+AC): sample variance (ddof=1) / mean;
* team-adjusted Pearson dispersion of total corners from a Poisson GLM ``total ~ home_team + away_team``
  (sum((y-mu)^2/mu) / (n - p));
* per-side (team corner count) raw and team-adjusted dispersion (``corners ~ home + attack + defence``).
Values ~1.0 = Poisson; >1 = over-dispersed (Negative-Binomial territory).
Used to keep the figure quoted in ``scripts/consult_gemini.py`` honest (tests recompute it).
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / "data" / "football_data" / "E0_2526.csv"


def _load(path: Path) -> list[tuple[str, str, float, float]]:
    rows = []
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        for r in csv.DictReader(fh):
            try:
                rows.append((r["HomeTeam"], r["AwayTeam"], float(r["HC"]), float(r["AC"])))
            except (KeyError, TypeError, ValueError):
                continue
    return rows


def _poisson_fit(X: np.ndarray, y: np.ndarray, iters: int = 60) -> np.ndarray:
    b = np.zeros(X.shape[1])
    b[0] = np.log(y.mean())
    for _ in range(iters):  # IRLS
        mu = np.exp(X @ b)
        z = X @ b + (y - mu) / mu
        b = np.linalg.solve(X.T @ (mu[:, None] * X) + 1e-9 * np.eye(X.shape[1]), X.T @ (mu * z))
    return np.exp(X @ b)


def _pearson(y: np.ndarray, mu: np.ndarray, p: int) -> float:
    return float(((y - mu) ** 2 / mu).sum() / (len(y) - p))


def measure(path: Path | str = DEFAULT) -> dict:
    rows = _load(Path(path))
    hc = np.array([r[2] for r in rows])
    ac = np.array([r[3] for r in rows])
    tot = hc + ac
    teams = sorted({r[0] for r in rows} | {r[1] for r in rows})

    def dummies(names: list[str]) -> np.ndarray:
        return np.array([[1.0 if n == t else 0.0 for t in teams[1:]] for n in names])

    n = len(rows)
    X = np.hstack([np.ones((n, 1)), dummies([r[0] for r in rows]), dummies([r[1] for r in rows])])
    adj_total = _pearson(tot, _poisson_fit(X, tot), X.shape[1])
    ys = np.concatenate([hc, ac])
    att = [r[0] for r in rows] + [r[1] for r in rows]
    dfn = [r[1] for r in rows] + [r[0] for r in rows]
    X2 = np.hstack([np.ones((2 * n, 1)), np.r_[np.ones(n), np.zeros(n)][:, None], dummies(att), dummies(dfn)])
    adj_side = _pearson(ys, _poisson_fit(X2, ys), X2.shape[1])
    return {
        "file": Path(path).name, "n_matches": n, "n_teams": len(teams),
        "total_mean": round(float(tot.mean()), 3), "total_var": round(float(tot.var(ddof=1)), 3),
        "total_var_over_mean": round(float(tot.var(ddof=1) / tot.mean()), 3),
        "total_team_adjusted_dispersion": round(adj_total, 3),
        "home_var_over_mean": round(float(hc.var(ddof=1) / hc.mean()), 3),
        "away_var_over_mean": round(float(ac.var(ddof=1) / ac.mean()), 3),
        "side_team_adjusted_dispersion": round(adj_side, 3),
        "home_away_corr": round(float(np.corrcoef(hc, ac)[0, 1]), 3),
    }


if __name__ == "__main__":
    print(json.dumps(measure(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT), indent=1))
