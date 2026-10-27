# config/

Runtime configuration is environment-driven (`.env`, see `../.env.example`,
loaded by `src/quantbet/config.py`). This folder holds reference data:

* `leagues.example.json` – the high-liquidity league whitelist used by
  `risk.py` for the **High Limit Risk** flag and an example sport-key list.
  Copy values into `QB_HIGH_LIQUIDITY_LEAGUES` / `QB_THE_ODDS_API_SPORTS`.
* `engine_b_params.example.json` – the Engine B (live model) defaults. These
  are **uncalibrated priors**; fit them on your own historical in-play data.

## Free-mode files (v0.2)

* `engine_b_params.json` – Engine B parameters written by `scripts/calibrate.py`
  (shipped: EPL 2025/26 fit → `corner_dispersion` ≈ 150, `goal_correlation` 0;
  other fields are priors). Loaded via `QB_ENGINE_B_PARAMS_PATH`.
* `team_ratings.json` – Football-Data team attack/defence ratings for goals and
  corners (shrunk towards 1). Loaded via `QB_FOOTBALL_DATA_RATINGS_PATH`.
* `calibration_report.json` – league averages, dispersion fits and Bet365
  pre-closing vs Pinnacle-close EV.
