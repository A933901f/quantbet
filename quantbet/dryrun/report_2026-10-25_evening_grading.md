# Rules replay 2026-10-25 – v4 vs v4.1 vs v4.2 (session: evening)

Generated 2026-10-25 22:34 UTC from logged states only (no network, nothing sent). Afternoon 539 states (in-sample); evening log 72 states up to 19:54 UTC.

## Frozen parameter sets

| version | sha256 | frozen_at | replayed hash matches |
|---|---|---|---|
| v4 | `e3d1af358c4505e6466437b45d93152435d7c3c30ed09e426b6a168b9b77535c` | 2026-10-25T18:50:49Z | yes |
| v4.1 | `1fa70e7a3cbe5cfbca9b4024277095be41c80f487b2b37e97fc43bf52ef62e4a` | 2026-10-25T19:14:10Z | yes |
| v4.2 | `40e806b910db93de34f9b26d9ebf361168077f1f38a8fe8d7dbc4e7cb6729aec` | 2026-10-25T19:31:39Z | yes |

## v4.2 calibration (config/calibration_report.json → v4_2)

* Football-Data CSVs (20 files, 7155 matches): corner columns ['AC', 'HC'] only – **no half-time corners** (has_half_time_corners=False).
* Corner H2/H1 per-minute rate ratio **1.064** (90% CI [1.011, 1.122]), H2 share of corners 52.9%; StatsBomb open data (github.com/statsbomb/open-data), Bundesliga 2015/16, EPL 2015/16, La Liga 2015/16, Ligue 1 2015/16, Serie A 2015/16, 354 matches (1706 H1 / 1914 H2 corners, minutes incl. stoppage).
* Goals 2-goal-lead dampener fit (HT |diff| >= 2, n=1223): observed/expected 2nd-half goals 1.04 vs 1.007 for all matches → **1.033** (90% CI [1.0, 1.065]); expected = de-vigged closing O/U 2.5 (PC>2.5 then AvgC>2.5), Poisson total × 2nd-half share 0.5494. No dampening found → v4.2 uses **1.00** (capped: shields only).
* Corner lead dampener (both teams, StatsBomb, 163 matches with a 2-goal lead in H2, 4826 min): 0.952 (90% CI [0.899, 1.004]) → 0.952; indicative (baseline = each match's own corners, biased toward 1).
* v4.2 corner shrinkage: observed pace vs half-adjusted *expected* corners so far (profile-weighted minutes), Gamma-Poisson weight eff/(eff+180); before 55' clamped at >= prior (may only raise). Prior benchmark uses the same half profile (normalised: mean weight 1 over 90', total prior unchanged). Unders disabled.

## Caveats

* Afternoon is in-sample for v4/v4.1/v4.2 (all designed after seeing it); v4.2's afternoon result is a fit, not evidence.
* Tiny samples (v4.2: a handful of bets); ROI/BSS are noise-dominated. Min-odds fill assumed; no bookmaker odds seen.
* Afternoon gaps (box paused) 13:45–14:19, 14:23–14:35, 14:35–16:46, 16:54–17:10 UTC.
* Lead dampeners are fitted at half time and applied at any minute with |diff| >= 2.

## Evening matches: sample status per version

Out-of-sample = first logged in-play state after that version's frozen_at (continuation matches of the afternoon were in play before any freeze, so they are at best quasi-out-of-sample).

| match | kind | states | first–last state UTC | at cut | final | v4 | v4.1 | v4.2 |
|---|---|---|---|---|---|---|---|---|
| Real Sociedad v Levante | continuation | 45 | 18:35–19:21 | 92' 2-1 | 2-1 (corners 2.0-3.0) | quasi / in-sample | quasi / in-sample | quasi / in-sample |
| Lecce v Juventus | continuation | 26 | 18:35–19:01 | 95' 0-2 | 0-2 (corners 5.0-3.0) | quasi / in-sample | quasi / in-sample | quasi / in-sample |
| Udinese v AC Milan | evening target | 1 | 19:54–19:54 | 9' 0-1 | 0-2 (corners 5.0-4.0) | out-of-sample | out-of-sample | out-of-sample |
| Barcelona v Real Madrid | evening target | 0 | – | no logged in-play state | 1-2 (corners 6.0-6.0) | – (no states) | – (no states) | – (no states) |

## Evening log – all

| metric | v4 | v4.1 | v4.2 |
|---|---|---|---|
| Alerts | 0 | 1 | 0 |
| Overs / Unders | 0 / 0 | 0 / 1 | 0 / 0 |
| Graded / open | 0 / 0 | 1 / 0 | 0 / 0 |
| Matches with alerts | 0 | 1 | 0 |
| Staked (graded) | 0 | 15 | 0 |
| Flat 1u P&L / ROI | +0.00 u / – | +0.98 u / +98.0% | +0.00 u / – |
| Quarter-Kelly P&L / ROI | +0.00 / – | +14.70 / +98.0% | +0.00 / – |
| Record | – | win 1 | – |
| Brier model (n) | – (0) | 0.2308 (1) | – (0) |
| Brier prior-only | – | 0.3445 | – |
| BSS = 1 − Brier/Brier_prior | – | +0.330 | – |
| Log-loss model | – | 0.6547 | – |
| Log-loss prior-only | – | 0.8842 | – |
| LL skill = 1 − LL/LL_prior | – | +0.260 | – |
| Max stake on one match | 0 | 15 (lecce:juventus) | 0 |
| Avg fair odds of chosen lines (all / graded) | – / – | 1.92 / 1.92 | – / – |

## Evening targets only (Udinese v Milan, Barcelona v Real Madrid; out-of-sample for v4/v4.1/v4.2)

| metric | v4 | v4.1 | v4.2 |
|---|---|---|---|
| Alerts | 0 | 0 | 0 |
| Overs / Unders | 0 / 0 | 0 / 0 | 0 / 0 |
| Graded / open | 0 / 0 | 0 / 0 | 0 / 0 |
| Matches with alerts | 0 | 0 | 0 |
| Staked (graded) | 0 | 0 | 0 |
| Flat 1u P&L / ROI | +0.00 u / – | +0.00 u / – | +0.00 u / – |
| Quarter-Kelly P&L / ROI | +0.00 / – | +0.00 / – | +0.00 / – |
| Record | – | – | – |
| Brier model (n) | – (0) | – (0) | – (0) |
| Brier prior-only | – | – | – |
| BSS = 1 − Brier/Brier_prior | – | – | – |
| Log-loss model | – | – | – |
| Log-loss prior-only | – | – | – |
| LL skill = 1 − LL/LL_prior | – | – | – |
| Max stake on one match | 0 | 0 | 0 |
| Avg fair odds of chosen lines (all / graded) | – / – | – / – | – / – |

## Evening continuation matches (quasi-out-of-sample)

| metric | v4 | v4.1 | v4.2 |
|---|---|---|---|
| Alerts | 0 | 1 | 0 |
| Overs / Unders | 0 / 0 | 0 / 1 | 0 / 0 |
| Graded / open | 0 / 0 | 1 / 0 | 0 / 0 |
| Matches with alerts | 0 | 1 | 0 |
| Staked (graded) | 0 | 15 | 0 |
| Flat 1u P&L / ROI | +0.00 u / – | +0.98 u / +98.0% | +0.00 u / – |
| Quarter-Kelly P&L / ROI | +0.00 / – | +14.70 / +98.0% | +0.00 / – |
| Record | – | win 1 | – |
| Brier model (n) | – (0) | 0.2308 (1) | – (0) |
| Brier prior-only | – | 0.3445 | – |
| BSS = 1 − Brier/Brier_prior | – | +0.330 | – |
| Log-loss model | – | 0.6547 | – |
| Log-loss prior-only | – | 0.8842 | – |
| LL skill = 1 − LL/LL_prior | – | +0.260 | – |
| Max stake on one match | 0 | 15 (lecce:juventus) | 0 |
| Avg fair odds of chosen lines (all / graded) | – / – | 1.92 / 1.92 | – / – |

## v4.2 by direction (evening)

| selection | alerts | graded | record | staked | QK P&L / ROI | flat ROI | BSS | LL skill |
|---|---|---|---|---|---|---|---|---|
| v4.1 under | 1 | 1 | win 1 | 15 | +14.70 / +98.0% | +98.0% | +0.330 | +0.260 |

## Alerts removed by v4.2 (first blocking rule; all sessions)

**v4 → v4.2**

| rule | alerts |
|---|---|
| lift: net multiplier <= 1.05 (all Overs of family suppressed) | 15 |
| kept (same line) | 2 |
| lift: Over line lift < 6% vs prior | 2 |
| single line per family (fair closest to 1.95) | 1 |

**v4.1 → v4.2**

| rule | alerts |
|---|---|
| v4.2: Unders disabled | 15 |
| kept (same line) | 5 |

## v4.2 alerts (evening)

| section | match | min | score | corners | line | prior fair | fair | lift % | net mult | λ model / prior | min odds | stake | FT | result | P&L u | P&L |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

## v4.1 alerts and their v4.2 fate (evening)

| section | match | min | score | line | fair | prior 1/p | result | v4.2 |
|---|---|---|---|---|---|---|---|---|
| evening | Lecce v Juventus | 69 | 0-2 | Under 2.5 Goals | 1.92 | 2.42 | win | v4.2: Unders disabled |

Notes: min-odds fill with first-tier quarter-Kelly stake; prior-only benchmark = same state, time-decayed pre-match lambdas, no multipliers/shrinkage (v4.2: with the calibrated corner half profile); live paper table rows: afternoon 96, evening 0 (run with v3 rules).
