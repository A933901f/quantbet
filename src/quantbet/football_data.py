"""Football-Data.co.uk CSV loader for calibration, team ratings and CLV look-ups.

Free historical CSVs (https://www.football-data.co.uk/data.php), e.g.
``https://www.football-data.co.uk/mmz4281/2526/E0.csv`` (season 2025/26,
Premier League). Columns used (see https://www.football-data.co.uk/notes.txt):

* results / stats: ``Date, HomeTeam, AwayTeam, FTHG, FTAG, HS, AS, HST, AST, HC, AC``
* Bet365: ``B365H/D/A``, ``B365>2.5``, ``B365<2.5``
* Pinnacle (closing): ``PSCH/PSCD/PSCA``, ``PC>2.5``, ``PC<2.5``, ``AHCh``, ``PCAHH/PCAHA``
* Pinnacle (pre-closing): ``PSH/PSD/PSA``, ``P>2.5``, ``P<2.5``
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import minimize_scalar
from scipy.special import gammaln

from .auditor import ClosingRow
from .engine import PreMatchRatings
from .poller import normalize_team, team_similarity

log = logging.getLogger(__name__)

BASE_URL = "https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"
# "Extra leagues" (one CSV per league, all seasons, different column layout):
# https://www.football-data.co.uk/new/ARG.csv  (Country, League, Season, Date, Time, Home, Away, HG, AG, Res,
# PSCH/PSCD/PSCA, MaxC*, AvgC*, B365C*, ...). They have NO corners/shots columns (checked 2026-10-25).
EXTRA_BASE_URL = "https://www.football-data.co.uk/new/{code}.csv"
LEAGUE_CODES = {
    "E0": "premier league", "E1": "championship", "E2": "league one", "D1": "bundesliga", "D2": "2. bundesliga",
    "I1": "serie a", "I2": "serie b", "SP1": "la liga", "SP2": "laliga 2", "F1": "ligue 1", "F2": "ligue 2",
    "N1": "eredivisie", "P1": "primeira liga", "B1": "belgian pro league", "T1": "super lig",
    "SC0": "scottish premiership",
}
# v4.3-global: secondary European "main" CSVs and the extra non-European CSVs, with their ESPN slugs.
V43_MAIN_CODES = ("E1", "E2", "SP2", "D2", "I2", "F2", "N1", "P1", "B1", "T1", "SC0")
EXTRA_CODES = ("ARG", "BRA", "USA", "MEX", "JPN")
FD_TO_ESPN = {
    "E0": "eng.1", "E1": "eng.2", "E2": "eng.3", "D1": "ger.1", "D2": "ger.2", "I1": "ita.1", "I2": "ita.2",
    "SP1": "esp.1", "SP2": "esp.2", "F1": "fra.1", "F2": "fra.2", "N1": "ned.1", "P1": "por.1", "B1": "bel.1",
    "T1": "tur.1", "SC0": "sco.1", "ARG": "arg.1", "BRA": "bra.1", "USA": "usa.1", "MEX": "mex.1", "JPN": "jpn.1",
}
EXTRA_LEAGUE_NAMES = {"ARG": "argentina primera", "BRA": "brazil serie a", "USA": "mls", "MEX": "liga mx",
                      "JPN": "j-league"}
EXTRA_RENAME = {"Home": "HomeTeam", "Away": "AwayTeam", "HG": "FTHG", "AG": "FTAG", "Res": "FTR"}
NUMERIC = ["FTHG", "FTAG", "HTHG", "HTAG", "HS", "AS", "HST", "AST", "HC", "AC", "HY", "AY", "HR", "AR",
           "B365H", "B365D", "B365A", "B365>2.5", "B365<2.5", "PSH", "PSD", "PSA", "PSCH", "PSCD", "PSCA",
           "P>2.5", "P<2.5", "PC>2.5", "PC<2.5", "AHCh", "PCAHH", "PCAHA", "B365CH", "B365CD", "B365CA"]


def season_code(start_year: int) -> str:
    """2025 -> ``"2526"``."""
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


def csv_url(code: str, start_year: int) -> str:
    """Download URL for a league code (``E0``) and season start year (extra leagues: one file, any year)."""
    if code.upper() in EXTRA_CODES:
        return EXTRA_BASE_URL.format(code=code.upper())
    return BASE_URL.format(season=season_code(start_year), code=code)


def is_extra(code: str) -> bool:
    return code.upper() in EXTRA_CODES


def load_extra_csv(path_or_buf: str | Path | object, code: str | None = None, seasons: Sequence[str] | None = None) -> pd.DataFrame:
    """Load an extra-league CSV (``new/ARG.csv`` layout) into the main-CSV column names.

    ``Home/Away/HG/AG/Res`` -> ``HomeTeam/AwayTeam/FTHG/FTAG/FTR``; ``Div`` = code; ``League`` from
    :data:`EXTRA_LEAGUE_NAMES`. ``seasons`` filters the ``Season`` column (e.g. ``["2025", "2025/2026"]``).
    Corners/shots columns do not exist in these files, so ``HC/AC/HS/AS`` stay absent.
    """
    df = pd.read_csv(path_or_buf, encoding="utf-8-sig", on_bad_lines="skip")  # type: ignore[arg-type]
    df = df.rename(columns=EXTRA_RENAME)
    if seasons is not None and "Season" in df.columns:
        df = df[df["Season"].astype(str).isin([str(x) for x in seasons])]
    df = df.dropna(subset=["HomeTeam", "AwayTeam"]).copy()
    for c in NUMERIC:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce", format="mixed")
    c = (code or "").upper()
    df["Div"] = c
    df["League"] = EXTRA_LEAGUE_NAMES.get(c, c.lower())
    return df.dropna(subset=["FTHG", "FTAG"]).reset_index(drop=True)


def league_baselines(df: pd.DataFrame) -> dict[str, object]:
    """Per-file averages used for the v4.3 league baseline sanity check (goals always; corners/shots if present)."""
    out: dict[str, object] = {"n": int(len(df)), "goals_avg": round(float((df["FTHG"] + df["FTAG"]).mean()), 3)
                              if len(df) else None,
                              "home_goals_avg": round(float(df["FTHG"].mean()), 3) if len(df) else None}
    for key, (h, a) in {"corners_avg": ("HC", "AC"), "shots_avg": ("HS", "AS"), "sot_avg": ("HST", "AST")}.items():
        if h in df.columns and a in df.columns and df[[h, a]].notna().all(axis=1).sum() > 0:
            sub = df[[h, a]].dropna()
            out[key] = round(float((sub[h] + sub[a]).mean()), 3)
            out[key.replace("_avg", "_n")] = int(len(sub))
        else:
            out[key] = None
    return out


def load_csv(path_or_buf: str | Path | object, league: str | None = None) -> pd.DataFrame:
    """Load one CSV (path, URL or buffer) into a cleaned DataFrame.

    Dates (``dd/mm/yy`` or ``dd/mm/yyyy``) are parsed day-first; numeric columns
    are coerced; rows without teams or full-time goals are dropped. A
    ``League`` column is added (from ``Div`` via :data:`LEAGUE_CODES` if not given).
    """
    df = pd.read_csv(path_or_buf, encoding="utf-8-sig", on_bad_lines="skip")  # type: ignore[arg-type]
    df = df.dropna(subset=["HomeTeam", "AwayTeam"]).copy()
    for c in NUMERIC:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce", format="mixed")
    if league is None and "Div" in df.columns:
        df["League"] = df["Div"].map(LEAGUE_CODES).fillna(df["Div"])
    else:
        df["League"] = league or ""
    return df.dropna(subset=["FTHG", "FTAG"]).reset_index(drop=True)


def load_many(paths: Iterable[str | Path]) -> pd.DataFrame:
    """Concatenate several CSVs."""
    frames = [load_csv(p) for p in paths]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


async def download(code: str, start_year: int, dest_dir: Path | str, user_agent: str, timeout_s: float = 30.0) -> Path:
    """Download one CSV politely (single request, cached on disk)."""
    import aiohttp

    from .polite_http import assert_allowed_url

    dest = Path(dest_dir) / (f"{code.upper()}_all.csv" if is_extra(code) else f"{code}_{season_code(start_year)}.csv")
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    url = csv_url(code, start_year)
    assert_allowed_url(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with aiohttp.ClientSession(headers={"User-Agent": user_agent}) as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=timeout_s)) as r:
            r.raise_for_status()
            dest.write_bytes(await r.read())
    return dest


# ---------------------------------------------------------------------------
# CLV look-ups for the tipster auditor
# ---------------------------------------------------------------------------
def to_closing_rows(df: pd.DataFrame) -> list[ClosingRow]:
    """Pinnacle closing prices per match as :class:`~quantbet.auditor.ClosingRow`."""

    def g(r: pd.Series, k: str) -> float | None:
        v = r.get(k)
        return float(v) if v is not None and not pd.isna(v) and float(v) > 1.0 else None

    rows: list[ClosingRow] = []
    for _, r in df.iterrows():
        if pd.isna(r["Date"]):
            continue
        h, d, a = g(r, "PSCH"), g(r, "PSCD"), g(r, "PSCA")
        o, u = g(r, "PC>2.5"), g(r, "PC<2.5")
        ahh, aha = g(r, "PCAHH"), g(r, "PCAHA")
        ahl = r.get("AHCh")
        rows.append(ClosingRow(
            r["Date"].date(), str(r["HomeTeam"]), str(r["AwayTeam"]),
            (h, d, a) if h and d and a else None, (o, u) if o and u else None,
            float(ahl) if ahl is not None and not pd.isna(ahl) else None,
            (ahh, aha) if ahh and aha else None,
        ))
    return rows


def bet365_vs_pinnacle_close(df: pd.DataFrame) -> pd.DataFrame:
    """Per-match Bet365 opening vs Pinnacle no-vig closing EV for 1X2 / O2.5 (backtest helper)."""
    from .engine import devig

    out = []
    for _, r in df.iterrows():
        rec: dict[str, object] = {"Date": r["Date"], "HomeTeam": r["HomeTeam"], "AwayTeam": r["AwayTeam"]}
        try:
            p = devig([r["PSCH"], r["PSCD"], r["PSCA"]], "power")
            for side, b, pp in zip("HDA", (r["B365H"], r["B365D"], r["B365A"]), p, strict=True):
                rec[f"ev_{side}"] = pp * b - 1
        except (ValueError, KeyError, TypeError):
            pass
        try:
            po = devig([r["PC>2.5"], r["PC<2.5"]], "power")
            rec["ev_O25"] = po[0] * r["B365>2.5"] - 1
            rec["ev_U25"] = po[1] * r["B365<2.5"] - 1
        except (ValueError, KeyError, TypeError):
            pass
        out.append(rec)
    return pd.DataFrame(out)


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------
def negbin_loglik(k: np.ndarray, mu: np.ndarray, r: float) -> float:
    """Sum of NegBin log-pmf with mean ``mu`` and size ``r`` (var = mu + mu^2/r)."""
    k = np.asarray(k, float)
    mu = np.asarray(mu, float)
    return float(np.sum(gammaln(k + r) - gammaln(r) - gammaln(k + 1) + r * np.log(r / (r + mu)) + k * np.log(mu / (r + mu))))


def fit_negbin_dispersion(counts: Sequence[float], mus: Sequence[float] | None = None,
                          bounds: tuple[float, float] = (0.5, 500.0)) -> float:
    """MLE of the NegBin size ``r`` for counts (optionally with per-match means).

    Large ``r`` => close to Poisson. Returns the upper bound if the data are
    under-dispersed relative to the supplied means.
    """
    k = np.asarray(counts, float)
    mu = np.full_like(k, k.mean()) if mus is None else np.asarray(mus, float)
    res = minimize_scalar(lambda lr: -negbin_loglik(k, mu, math.exp(lr)),
                          bounds=(math.log(bounds[0]), math.log(bounds[1])), method="bounded")
    return float(math.exp(res.x))


def moment_dispersion(counts: Sequence[float]) -> float:
    """Method-of-moments ``r = mu^2 / (var - mu)`` (inf if not over-dispersed)."""
    k = np.asarray(counts, float)
    m, v = k.mean(), k.var(ddof=1)
    return float(m * m / (v - m)) if v > m else math.inf


@dataclass(slots=True)
class LeagueCalibration:
    """Calibrated league-level parameters."""

    league: str
    n_matches: int
    home_goals: float
    away_goals: float
    home_corners: float
    away_corners: float
    goal_correlation: float
    corner_dispersion_mle: float
    corner_dispersion_mom: float
    corner_dispersion_conditional: float  # given team-rating means
    total_goals_var: float
    total_corners_var: float

    def to_dict(self) -> dict[str, object]:
        """JSON-friendly dict."""
        return asdict(self)


@dataclass(slots=True)
class TeamRatingsTable:
    """Multiplicative attack/defence ratings for goals and corners (shrunk to 1).

    ``E[home goals] = league_home_goals * att_h[home] * def_a[away]`` (same for
    corners). Implements :class:`~quantbet.engine.TeamRatingsSource`.
    """

    league_home_goals: float
    league_away_goals: float
    league_home_corners: float
    league_away_corners: float
    att_home_g: dict[str, float] = field(default_factory=dict)
    def_home_g: dict[str, float] = field(default_factory=dict)
    att_away_g: dict[str, float] = field(default_factory=dict)
    def_away_g: dict[str, float] = field(default_factory=dict)
    att_home_c: dict[str, float] = field(default_factory=dict)
    def_home_c: dict[str, float] = field(default_factory=dict)
    att_away_c: dict[str, float] = field(default_factory=dict)
    def_away_c: dict[str, float] = field(default_factory=dict)
    league_by_team: dict[str, str] = field(default_factory=dict)
    min_similarity: float = 85.0

    def _team(self, name: str) -> str | None:
        n = normalize_team(name)
        if n in self.att_home_g:
            return n
        best, score = None, 0.0
        for t in self.att_home_g:
            s = team_similarity(name, t)
            if s > score:
                best, score = t, s
        return best if score >= self.min_similarity else None

    def expected(self, home: str, away: str) -> tuple[float, float, float, float] | None:
        """(home goals, away goals, home corners, away corners) or None if a team is unknown."""
        h, a = self._team(home), self._team(away)
        if h is None or a is None:
            return None
        hg = self.league_home_goals * self.att_home_g[h] * self.def_away_g.get(a, 1.0)
        ag = self.league_away_goals * self.att_away_g.get(a, 1.0) * self.def_home_g.get(h, 1.0)
        hc = self.league_home_corners * self.att_home_c.get(h, 1.0) * self.def_away_c.get(a, 1.0)
        ac = self.league_away_corners * self.att_away_c.get(a, 1.0) * self.def_home_c.get(h, 1.0)
        return hg, ag, hc, ac

    def ratings(self, home: str, away: str, league: str | None = None) -> PreMatchRatings | None:
        """PreMatchRatings for a fixture (``None`` if a team is unknown)."""
        e = self.expected(home, away)
        if e is None:
            return None
        hg, ag, hc, ac = e
        return PreMatchRatings(hg - ag, hg + ag, hc - ac, hc + ac)


def fit_team_ratings(df: pd.DataFrame, shrink_k: float = 6.0, half_life_days: float | None = 180.0) -> TeamRatingsTable:
    """Fit ratings from results/corners with exponential time-decay and shrinkage.

    Ratio estimates per team (e.g. home goals scored / league home average)
    are shrunk towards 1 with weight ``n / (n + shrink_k)``.
    """
    d = df.dropna(subset=["FTHG", "FTAG"]).copy()
    d["h"] = d["HomeTeam"].map(normalize_team)
    d["a"] = d["AwayTeam"].map(normalize_team)
    if half_life_days and d["Date"].notna().any():
        age = (d["Date"].max() - d["Date"]).dt.days.fillna(0).to_numpy(float)
        w = 0.5 ** (age / half_life_days)
    else:
        w = np.ones(len(d))
    d["w"] = w
    has_c = "HC" in d.columns and d["HC"].notna().any()

    def wavg(col: str, mask: pd.Series | None = None) -> float:
        sub = d if mask is None else d[mask]
        sub = sub.dropna(subset=[col])
        return float(np.average(sub[col], weights=sub["w"])) if len(sub) else float("nan")

    lhg, lag = wavg("FTHG"), wavg("FTAG")
    lhc, lac = (wavg("HC"), wavg("AC")) if has_c else (5.5, 4.5)
    t = TeamRatingsTable(lhg, lag, lhc, lac)

    def ratio(team_col: str, val_col: str, league_avg: float, team: str) -> float:
        sub = d[(d[team_col] == team)].dropna(subset=[val_col])
        if not len(sub) or not league_avg:
            return 1.0
        n = float(sub["w"].sum())
        raw = float(np.average(sub[val_col], weights=sub["w"])) / league_avg
        z = n / (n + shrink_k)
        return z * raw + (1 - z)

    teams = sorted(set(d["h"]) | set(d["a"]))
    for team in teams:
        t.att_home_g[team] = ratio("h", "FTHG", lhg, team)
        t.def_home_g[team] = ratio("h", "FTAG", lag, team)
        t.att_away_g[team] = ratio("a", "FTAG", lag, team)
        t.def_away_g[team] = ratio("a", "FTHG", lhg, team)
        if has_c:
            t.att_home_c[team] = ratio("h", "HC", lhc, team)
            t.def_home_c[team] = ratio("h", "AC", lac, team)
            t.att_away_c[team] = ratio("a", "AC", lac, team)
            t.def_away_c[team] = ratio("a", "HC", lhc, team)
        lg = d.loc[(d["h"] == team) | (d["a"] == team), "League"]
        t.league_by_team[team] = str(lg.iloc[-1]) if len(lg) else ""
    return t


def calibrate_league(df: pd.DataFrame, league: str = "", table: TeamRatingsTable | None = None) -> LeagueCalibration:
    """League goal rates, goal correlation and corner NegBin dispersion (MLE / MoM / conditional)."""
    d = df.dropna(subset=["FTHG", "FTAG"])
    tg = (d["FTHG"] + d["FTAG"]).to_numpy(float)
    cov = float(np.cov(d["FTHG"], d["FTAG"])[0, 1]) if len(d) > 2 else 0.0
    rho = cov / max(min(d["FTHG"].mean(), d["FTAG"].mean()), 1e-9)
    dc = d.dropna(subset=["HC", "AC"]) if "HC" in d.columns else d.iloc[0:0]
    tc = (dc["HC"] + dc["AC"]).to_numpy(float) if len(dc) else np.array([10.0, 10.0])
    r_mle = fit_negbin_dispersion(tc)
    r_mom = moment_dispersion(tc)
    r_cond = r_mle
    if table is not None and len(dc):
        mus = []
        for _, r in dc.iterrows():
            e = table.expected(r["HomeTeam"], r["AwayTeam"])
            mus.append((e[2] + e[3]) if e else tc.mean())
        r_cond = fit_negbin_dispersion(tc, mus)
    return LeagueCalibration(
        league, int(len(d)), float(d["FTHG"].mean()), float(d["FTAG"].mean()),
        float(dc["HC"].mean()) if len(dc) else float("nan"), float(dc["AC"].mean()) if len(dc) else float("nan"),
        float(max(0.0, rho)), r_mle, r_mom, r_cond, float(tg.var(ddof=1)) if len(tg) > 1 else float("nan"),
        float(tc.var(ddof=1)) if len(tc) > 1 else float("nan"),
    )


def league_default_ratings(cal: LeagueCalibration) -> PreMatchRatings:
    """League-default PreMatchRatings from a calibration."""
    return PreMatchRatings(cal.home_goals - cal.away_goals, cal.home_goals + cal.away_goals,
                           cal.home_corners - cal.away_corners, cal.home_corners + cal.away_corners)


__all__ = [
    "BASE_URL", "EXTRA_BASE_URL", "LEAGUE_CODES", "V43_MAIN_CODES", "EXTRA_CODES", "FD_TO_ESPN", "load_extra_csv",
    "league_baselines", "is_extra", "season_code", "csv_url", "load_csv", "load_many", "download",
    "to_closing_rows", "bet365_vs_pinnacle_close", "negbin_loglik", "fit_negbin_dispersion",
    "moment_dispersion", "LeagueCalibration", "TeamRatingsTable", "fit_team_ratings",
    "calibrate_league", "league_default_ratings",
]


def save_ratings(table: TeamRatingsTable, path: str | Path) -> None:
    """Write a ratings table as JSON."""
    import json

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(asdict(table), indent=1, sort_keys=True))


def load_ratings(path: str | Path) -> TeamRatingsTable | None:
    """Read a ratings table written by :func:`save_ratings` (None if missing)."""
    import json

    p = Path(path)
    if not p.exists():
        return None
    return TeamRatingsTable(**json.loads(p.read_text()))


__all__ += ["save_ratings", "load_ratings"]
