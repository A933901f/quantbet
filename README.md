# QuantBet – read-only quant betting signal engine ($0/month)

QuantBet watches live football matches through **free** public statistics:
**ESPN's public site API** (Tier 1, primary), optionally enriched by the
**API-Football free tier** (Tier 2, budgeted) and the **FotMob match page**
(Tier 3, opt-in). It prices every goal and corner line,
including Asian quarter lines, with an in-play Poisson / Negative-Binomial
model, and sends a Telegram alert with the **minimum Bet365 odds** you need to
get +2.5 % EV. Then you open the Bet365 app yourself and bet only if the price
is at or above that minimum.

**It never logs into any bookmaker, never automates a browser, never places a
bet and never sends a request to a bet365 domain** (blocked in code). Run it
on a server or VPS, not your home network.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the design and diagram, and
[API_RECOMMENDATIONS.md](API_RECOMMENDATIONS.md) for the free sources,
endpoints and what was verified.

## $0 setup

```bash
python3.11+ -m venv .venv && source .venv/bin/activate   # tested on Python 3.13
pip install -r requirements.txt        # or: pip install -e ".[dev]"
cp .env.example .env
```

1. **Telegram (free):** create a bot with @BotFather, send it a message and
   put `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`. While
   `QB_DRY_RUN=true`, alerts are only logged.
2. **User-Agent:** set `QB_USER_AGENT` to something that identifies you,
   including a contact address. QuantBet sends it unchanged and never
   impersonates a browser.
3. **The-Odds-API (free, optional but recommended):** sign up for the free
   key (500 credits/month) and set `THE_ODDS_API_KEY`. Without it, pre-match
   goal ratings come from ESPN's pickcenter lines (DraftKings close, de-vigged)
   and then Football-Data team ratings.
4. **API-Football (free, optional):** set `API_FOOTBALL_KEY` to enable Tier 2.
   It is only called for flagged matches in minute 55–85, at most 90 calls per
   UTC day (`QB_API_FOOTBALL_USE_LIMIT`), tracked in `data/api_football.sqlite3`.
   Without a key it stays off.
5. **FotMob page (optional):** `QB_ENABLE_FOTMOB_PAGE=true` adds xG, shots
   inside the box and FotMob's momentum graph for flagged matches, every 5 min
   at most.
6. **Calibrate from Football-Data.co.uk (free):**
   ```bash
   python scripts/calibrate.py --leagues E0 D1 I1 SP1 --seasons 2024 2025 --download
   ```
   This writes `config/team_ratings.json`, `config/engine_b_params.json` and
   `config/calibration_report.json`. The shipped files come from EPL 2025/26
   only (380 matches). Teams promoted since then are missing, so re-run it.
7. Run:
   ```bash
   PYTHONPATH=src python -m quantbet.main     # or `quantbet` after pip install -e .
   python scripts/live_dry_run.py --minutes 3 # real ESPN data, prints would-be alerts, sends nothing
   nohup python scripts/paper_dry_run.py --until 2026-10-25T18:45 --kickoff-cutoff 2026-10-25T16:35 &
                                              # paper trading: dryrun/alerts_*.md, paper_trades.sqlite3, report_*.md
   pytest -q                                  # 156 tests, no network
   ```

### Data tiers

| Tier | Source | What it gives | Default |
|---|---|---|---|
| 1 | ESPN `site.api.espn.com/.../soccer/all/scoreboard`: **one request every 60 s + 2–5 s jitter**, filtered in memory by league (event `uid` `l:<id>`). Per-league scoreboards, staggered ≥ 15 s, are used only as a fallback. `.../{league}/summary?event={id}` is fetched **once per match** (first sighting or pre-kick-off, 1 per cycle) and cached in SQLite. Since the forward-run plumbing, listed leagues also get a pre-kick-off pass (≤ 45 min before KO, 4 per cycle, plus one refresh in the last 10 min); see the forward-run operating guide. | score, clock incl. added time, HT/FT, corners, shots, shots on target, possession, red cards (scoreboard), DraftKings pre-match lines (summary) | **on** |
| – | `DeltaTracker` | 10-min velocity, `activity_10m` and the bounded momentum multiplier (see below) | on |
| 2 | API-Football free `fixtures?live=all` + `fixtures/statistics` | xG (when provided), shots inside box, red cards | on only with `API_FOOTBALL_KEY` |
| 3 | FotMob match page `__NEXT_DATA__` | xG, shots inside box, used to **confirm** an alert | on, gated: minute 50–85 and EV at the neutral reference ≥ +1.0 %, cached 5 min per match |
| legacy | SofaScore / FotMob JSON API | – | off (SofaScore 403s an honest client; never impersonated) |

**Momentum (single path into Engine B).** `shots_off = max(0, totalShots − shotsOnTarget)`;
`activity_10m = 1.5·ΔCorners + 1.2·ΔSOT + 0.5·ΔShotsOff`. This is computed per
team and combined, over the last 10 match minutes, and scaled to a 10-minute
rate when the span differs. `momentum_mult = clip(1 + 0.08·(activity_10m/3.5 − 1), 0.85, 1.25)`.
The team baseline is 1.75. Engine B scales each team's goal and corner rates
by its team multiplier only. No SOT ratio or momentum index is added on top,
so nothing is counted twice. The signed dominance index
`(act_home − act_away)/max(act_total, 3.5)` is used only as a trigger
(`momentum_home/away` at ≥ 0.30). `activity_high` fires when the combined
multiplier is ≥ 1.04.

**Alert gates added in v0.3.** EV at reference = the EV of the full model at the
fair odds of a *neutral* model. The neutral model has the same score, clock,
red cards and chasing logic, but its live-activity multipliers are set to 1.
That is the price a market that ignores live pressure would show.
* EV at reference < 0 means live activity argues against the line, so the
  alert is dropped.
* EV at reference ≥ +1.0 % at minute 50–85 triggers the FotMob page check.
  * Over is **confirmed** if xG pace (xG / minute × 90) is ≥ 0.9 × the
    pre-match goal expectation, or shots-in-box pace is ≥ 0.9 × 15 per 90.
    It is **contradicted** if every available pace is below that.
  * Under is the mirror image, at ≥ 1.1 ×.
  * Contradicted alerts are dropped. If FotMob is unavailable, the alert fires
    with the tag `unavailable`.

Leagues: `QB_ESPN_LEAGUES=["eng.1","esp.1","ger.1","ita.1","fra.1","uefa.champions","uefa.europa"]`.

List settings must be JSON arrays, e.g.
`QB_FREE_LEAGUE_FILTER=["Premier League","Serie A"]`.

## What an alert looks like

Produced by the end-to-end test from the real ESPN capture of Inter v
Fiorentina (43', 0-0, Inter 3 corners / 4 SOT vs 0 / 0), $2,000 bankroll:

```
[LIVE MODEL · MIN ODDS]
⚽ Internazionale vs Fiorentina  |  43'  0-0
🏆 Italian Serie A
🎯 Over 8.5 Corners  (half line)
⚖️ Fair Odds: 1.95  (p=0.514)
✅ MINIMUM BET365 ODDS TO BET: 2.00
🛑 Abort floor: 1.98 (do not bet below)
🧮 If Bet365 offers 2.00 -> Bet $15 | If 2.05 -> Bet $25 | If 2.10 -> Bet $35 | If 2.15 -> Bet $45 | ...
📡 Triggers: momentum_home
📊 Ratings: espn_draftkings | Stats: espn
```

A model-only alert is sent only when the match is between 10' and 86', the
stats are fresh and complete, the ratings are not just league defaults, and
there is a trigger: strong attack momentum (graph or synthetic), the favourite level or behind
after 60', or a red card. Each market is limited to one alert per 10 minutes,
and each match to 4.

## Quota notes (The-Odds-API free tier)

* `/events` is free. `/odds` costs markets × regions, where up to 10
  bookmakers count as one region. The default `h2h,totals` call costs
  **2 credits**.
* With a reserve of 20, 500 credits give about **240 calls per month**, or 8
  a day. The quota manager spreads the remaining credits over the days left
  in the month. It only calls a sport when a fixture kicks off within 3 h and
  the last snapshot is more than 3 h old. Everything is cached in
  `data/odds_cache.sqlite3`, and `x-requests-remaining/used/last` are logged.
* Live in-play odds are **not** bought with free credits. At 2 credits per
  call they would last minutes. Pinnacle snapshots are used only as
  pre-match priors.

## Tipster audit

Tipster audit: export picks to CSV (`load_picks_csv`) and closing odds from
football-data.co.uk (`load_football_data_csv`), then
`TipsterAuditor(closing).audit(picks)` → WHITELIST (n ≥ 500, mean CLV ≥ 1.02,
p < 0.05), FADE (n ≥ 1000, negative yield, p < 0.05) or NEUTRAL. Live tips
from whitelisted tipsters are only re-alerted when the current Bet365 price is
still ≥ 2.5 % EV. An optional Telethon listener can read Telegram tip channels
you are a member of; scraping Reddit/Blogabet/OLBG is **not** implemented.

`football_data.to_closing_rows(load_csv(path))` produces the same closing rows
from any downloaded Football-Data CSV.

## Alert rules v3

One line per family (goals/corners) per poll, chosen by expected log-growth;
15-minute family cooldown; ≤ 2 signals and ≤ 5 % bankroll per match; alerts
only in 30'–43' and 55'–85' (red cards too); shrinkage of observed pace toward
the prior; comfortable-lead dampener; stricter favourite-chasing; FotMob
dominance required for corner Overs after 75'. Details in ARCHITECTURE.md.
Compare against the old rules: `python scripts/replay_rules.py`.

## v4.5-sharp (DEFAULT: `QB_ALERT_RULES=v4.5-sharp` / `--rules v4.5-sharp`)

v4.5 is built on v4.4-syndicate and uses the same code (`src/quantbet/syndicate.py`, `SharpParams`). It has its own
freeze under `frozen_sets["v4.5-sharp"]` (`python scripts/freeze_params.py --rules v4.5-sharp`). The v4.2,
v4.3-global and v4.4-syndicate hashes are untouched, and v4.4 is still selectable (`--rules v4.4-syndicate`,
workflow choice).

| Spec name | Code (field / where) | v4.5 | v4.4 |
|---|---|---|---|
| `achievability_gate` | `SharpParams.achievability_gate` | False | True (lift ≥ (1+h)/(1−m)) |
| `max_lift_goals` / `max_lift_corners` | `max_lift_goals` / `max_lift_corners` (`lift_cap()`) | 0.28 / 0.35 | `max_lift` 0.45 |
| `over_min_net` Goals / Corners | `over_min_net_goals` / `over_min_net_corners` (`over_net_min()`) | 1.06 / 1.08 | `over_min_net` 1.10 |
| `over_suppress_net` | `over_suppress_net` | 1.05 | 1.05 |
| tier hurdles | `tier_target_edge` | .035 / .045 / .060 | .040 / .055 / .070 |
| `abort_ratio` | `abort_ratio` | 0.60 | 0.60 |
| unmapped `espn.*` block | `block_unmapped_leagues`, `unmapped_prefix` (`hygiene_block()`) | on | off |
| youth filter | `youth_tokens`, `youth_suffix_tokens`, `youth_name_exceptions` | u21, u23, u19, u20, u18, u17, reserves, academy, jong; " b", " ii", " iii" (`quantbet.youth`) | - |
| TIER_3 = Goals O/U only | `allow_tier3_corners` / `allow_tier3_cards` (`tier_allows()`) | False / False | all markets |
| `minute_windows` | `PricingConfig.minute_windows` (engine.py preset) | (30,43), (55,85) | (30,43), (55,85) |
| `w_fav_trailing` | `w_fav_trailing` | 1.0 | 1.15 |
| `sustained_over_index` over 3 polls | `sustained_over_index`, `momentum_polls` | 1.35, 3 | 1.70, 3 |
| `max_momentum_index` | `max_momentum_index` (`flurry_cap()`) | 2.90 | - |
| Over activity confirmation | `require_activity_confirm` (`activity_confirm_overs()`); `PricingConfig.activity_trigger_mult` | `momentum_mult_total` ≥ 1.04 or `fav_trailing`, else `no_activity_confirm` | - |
| Under suppression by activity (spec "bullish") | `under_suppress_attack_momentum` / `under_suppress_mult_total` (`under_activity_suppress()`, `MomentumView.activity_bullish`) | abs(`attack_momentum`) ≥ 0.30 or `momentum_mult_total` ≥ 1.02 | - |
| `bus_parking` | `bus_from_minute`, `bus_leader_share_max`, `bus_index_max` | 65', ≤ 0.25, ≤ 0.75 | 55', ≤ 0.35, ≤ 1.00 |
| cross-family direction lock | `match_direction_lock` (`direction_lock()`), `SyndicateDeduplicator.check` | on: `match_direction_conflict` | off (only same-family `family_conflict`) |
| `corner_prior_minutes` / `corner_dispersion` | `LiveModelParams` (engine.py preset) | 200 / 65.0 (Football-Data MLE 65.34, MoM 65.04) | 180 / 149.56 |
| second-half `tempo_stall` | `stall2_*` (`second_half_stall()`) | OFF (`stall2_enabled=False`; mechanics kept: 55'–75', index ≤ 0.65 over 12', abs(gd) ≤ 1) | first half only |
| card counting | `cards_count` (`card_rule()`) | "bet365": Y 1, R 2, 2nd yellow 3 | "y1_r1": Y 1, R 1 |
| cards distribution | `cards_distribution` | NegBin, pooled r | Poisson |
| `cards_paper_only` | `cards_paper_only` (`cards_paper()`) | True | False |
| combo gating | `combo_require_gated_families` (`combo_gated()`) | True | False |

* **League hygiene** - only slugs in `config/leagues.json` may alert. Any `espn.*` / unlisted slug gets the trace
  `unmapped_league_blocked` and NO alerts at all (base-engine signals included). Youth or reserve sides are blocked
  with `youth_reserve_blocked (<token>)`. Tokens are whole words, `U-21` / `U 21` / `Under 21` also match, and
  " b" / " ii" / " iii" must be a separate trailing word, so `Club Brugge`, `Bayern` and `Benfica` do not match.
  `Jong Ajax` / `Jong PSV` match `jong`; `Jonge Kracht` / `Jongbloed` do not. `Juan Pablo II` (a senior Peruvian
  club) is an explicit exception.
* **Youth guard in name matching** (all rule sets) - `poller.team_similarity` returns 0 unless both names are youth /
  reserve sides or both are senior (`quantbet.youth.youth_compatible`), and `TeamRatingsTable._team` skips
  incompatible keys. Before this, rapidfuzz `token_set_ratio("Arsenal U21", "Arsenal")` was 100, so a U21 side could
  inherit the senior club's ratings (the base v4.2 resolver uses the team table in every league) or be matched to
  the senior event. The 27-Oct Fleetwood Town v Arsenal alert was the senior Arsenal (Carabao Cup R4, ESPN
  competitor 359, priors from the DraftKings pickcenter), so it was not affected; the guard closes the latent path.
  It is code, not a frozen parameter, so no hash changes.
* **Momentum** - Overs need a sustained index ≥ 1.35 on every poll (≥ 3 polls). The same Overs are blocked when
  the latest reading is above 2.90 (`momentum_flurry_cap`: a flurry, not sustained pressure). Goals/Corners Overs
  also need `momentum_mult_total` ≥ 1.04 (the v4.3 `activity_trigger_mult`) or a trailing favourite
  (`no_activity_confirm`). The favourite-trailing
  weight is 1.0 because the live model already applies `fav_trailing_goal_mult` / `corner_mult`.
* **Unders** - first-half `tempo_stall` as v4.4. `bus_parking` (Goals Under) needs 65'+, abs(gd) == 1, the leader's
  pressure share ≤ 0.25 and the adjusted index ≤ 0.75 on every poll of the persistence window. Any Under is
  suppressed (`bullish`) when abs(`attack_momentum`) ≥ 0.30 or `momentum_mult_total` ≥ 1.02. The second-half
  `tempo_stall` is off (`stall2_enabled=False`; set it True in a new freeze to bring it back).
* **Direction lock** - once a match has alerted one direction in any family, the opposite direction in any family is
  dropped (`match_direction_conflict`), including alerts pending in the same poll. Staked alerts are checked against
  staked alerts only, so a paper card never blocks a real bet; paper cards are checked against staked and paper
  alerts.
* **Total Cards (Bet365)** - yellow = 1, straight red = 2, a second-yellow dismissal = 3 (the first yellow plus the
  red; maximum 3 per player). This is the Bet365 **Total Cards** market. It is NOT the 10/25 **booking-points**
  market, which is a different product priced on a different scale.
  * ESPN: the boxscore `yellowCards` stat EXCLUDES the first yellow of a second-yellow dismissal. Scoreboard
    `details` and summary `keyEvents` keep both entries, so `quantbet.espn.card_points*` counts per player from
    them. `Y + 2R` from the stats is only the fallback.
  * Priors: Football-Data `HY + AY + 2 × (HR + AR)` per 90 (`cards_bet365` in `config/syndicate_priors.json`:
    pooled cards90, cards_var, per-division baselines). Football-Data `notes.txt`: English and Scottish divisions
    exclude the first yellow of a second-yellow dismissal from HY/AY, so those priors undercount by ≤ reds/90
    (`sy_bias_max` per division). European divisions include it, so the count is exact there.
  * Pricing: Negative Binomial with the pooled within-division shape r (common-r MLE over division means). The
    remaining-time mean is scaled. Gamma frailty means any sub-interval of a match with mean μ is NB(r, μ).
    r → ∞ gives the Poisson.
  * **PAPER ONLY - UNCALIBRATED**: cards signals are logged to SQLite and graded by `scripts/audit_run.py` as
    Bet365 Total Cards (regular time, per player). They are tagged `[PAPER ONLY - UNCALIBRATED]` and show no stake
    ladder. They never use the 3-alert / 5% match cap and are never combo legs. TIER_3 cards are blocked. Keep
    this until 50 live games with foul telemetry have been graded.
* **Combos** - `[BET BUILDER / 3-WAY COMBO]` gets a leg from another family only when that family independently
  passes its directional net gate: Over net ≥ its `over_net_min`, or Under net ≤ `under_max_net`.
* **Corner dispersion** - r = 65.0 with a 200-minute prior, from the Football-Data full-time within-division fit
  (MLE 65.34, MoM 65.04; `corners_dispersion_check` in the priors file). The frozen v4.2 fit is 149.56.
* Replay: `python scripts/replay_syndicate.py --rules v4.5-sharp ...` (output keys `replay`, `all_replay`,
  `by_market_replay`, `by_tier_replay`, `decision_counts`). The 27-Oct replay is **in-sample** because the
  thresholds were picked after seeing that run. Treat it as an implementation check, not validation.

## Forward-run operating guide (v4.5-sharp, 2-week forward run)

The v4.5-sharp parameters stay frozen (`20e12b59…6fdb`). The four changes below are runtime plumbing only and
leave every model threshold and frozen value alone. The 27-Oct replay is unchanged: 14 alerts, flat +1.055u,
+42.82 on 295 staked, Brier 0.2615.

### 1. Paper-only cards are never staked

* `[PAPER ONLY - UNCALIBRATED]` signals (v4.5 Total Cards) are zeroed before they reach the alerts DB:
  `stake_units`, `stake_currency`, `kelly_fraction` and `tiers` are all 0 or empty.
* The paper minimum odds stay in `extra.paper_stake` and `extra.paper_tiers` for display and grading.
* `AlertDatabase.report()` (the `session report` log line at shutdown) counts staked alerts only.
* `scripts/audit_run.py` still grades paper cards separately, at 1u flat.

### 2. Dedup / match-cap memory survives restarts

* Every 6-hour GitHub Actions run used to start with an empty `SyndicateDeduplicator`. A match that already had
  3 alerts or 5% exposure in the previous run could therefore alert again.
* On start-up, `main.run_free` now calls `SyndicateDeduplicator.load_recent_alerts(db_path, lookback_s)`. It
  replays the last `QB_DEDUP_HYDRATE_LOOKBACK_S` seconds (default 10800 = 3 h) of syndicate alerts from the DB.
  This rebuilds:
  * the per-match count and stake;
  * the family cooldowns;
  * the direction locks.
* Paper rows rebuild only the paper state.
* A missing DB, an old schema, and v4.3/v4.4 rows without `extra.dedup` are skipped. So is malformed JSON.
* Cooldowns are kept in **match minutes**, so nothing has to be rebuilt from wall-clock time. The stored alert
  minute is compared with the next observed match minute, exactly as in-process.
* Look for `dedup hydrated from alerts DB` in the log, with the number of rows used.

**Workflow cache:**

* `data/*.sqlite3` does persist between runs. `actions/cache/restore` takes the newest `quantbet-state-*` entry
  for the branch, and `save` writes a new key per run.
* The alerts DB runs in WAL mode, and the `*.sqlite3` glob skips `-wal` files. So the last alerts of a run could
  be lost. A new `Checkpoint SQLite (WAL -> main file)` step (`if: always()`) now folds the WAL into the main
  file before the cache save and the artifact upload.
* Caches unused for 7 days are evicted. After a pause of more than a week, the run starts empty and hydration
  finds nothing, which is harmless.

### 3. Pre-kickoff ESPN summaries (true pre-match priors)

* **The problem:** on 27 Oct, 16 of the 39 alerts were priced from a summary that was first fetched while the
  match was already in play. The ESPN DraftKings odds in that summary are then in-play odds, not a pre-match
  line.
* **What it does now:** listed (non-`espn.*`) leagues get their summary within `QB_ESPN_PREKICK_WINDOW_MIN`
  (45) minutes of kick-off. Soonest kick-off goes first, on a separate budget of
  `QB_ESPN_PREKICK_SUMMARIES_PER_CYCLE` (4) per cycle. The frozen `summaries_per_cycle` (3) is not touched.
* **Never overwritten:** a summary fetched pre-match is never replaced by an in-play one. The DB upsert refuses
  it.
* **Optional late refresh:** one refresh in the last `QB_ESPN_PREKICK_REFRESH_MIN` (10) minutes, if the first
  pre-match fetch was older than that. Set it to 0 to switch the refresh off. The refresh goes into
  `latest_*` columns. It replaces the model prior only while the match is still `pre`.
* **Data change:** the market prior is now the **latest pre-match** DraftKings line (≤ 10 min before KO when
  refreshed) instead of whatever was fetched first.
* **Provenance columns:** `espn_summary_once` gains `fetched_state`, `fetched_minute`, `kickoff`,
  `latest_body`, `latest_fetched_at`, `latest_state` and `latest_minute`. The migration runs automatically and
  backfills from the stored bodies.
* **What the alerts carry:** `extra.global.fetch` holds `fetched_state`, `fetched_minute`, `fetched_at`,
  `kickoff`, `snapshot` and `true_pre_match`, plus `dk_first` / `dk_latest`.
* **Request budget:** up to 4 extra summary GETs per ~62 s cycle, and at most 2 per listed match (first +
  refresh). They all go through `polite_http`: 15 s per URL, 1 s global gap, concurrency 2, honest UA.
  * At peak that is 1 scoreboard + 4 pre-kickoff + 3 normal ≈ 8 req/min.
  * 27 Oct (82 listed matches, peak 21 kick-offs in 45 min, 13 at 19:45): about 6 cycles to fetch the first
    lines and 4 cycles for the 19:45 refreshes. That is ≤ 164 summary GETs a day on this path. Before, it was
    about 82, so the net new traffic is mainly the ≤ 82 late refreshes.

### 4. Recording what you actually did on Bet365 (`scripts/record_fill.py`)

Every card ends with an `ID: <code>abc123def456</code>` line (the signal id, 12 hex characters).

**Local clone:**

```bash
python scripts/record_fill.py abc123def456 --odds 2.10            # stake defaults to the alert's 1st-tier stake
python scripts/record_fill.py abc123def456 --odds 2.05 --stake 15
python scripts/record_fill.py abc123def456 --skipped --reason line_moved
python scripts/record_fill.py --list --hours 72                   # staked alerts with no fill yet
git add data/fills.csv && git commit -m "fills" && git push
```

* Validation:
  * the odds must be > 1.01;
  * the id must exist in the alerts DB (a unique prefix of ≥ 6 characters is accepted);
  * a second fill of the same id is refused unless you pass `--force`;
  * a paper-only card cannot be filled unless you pass `--force`;
  * taken odds below the card's minimum Bet365 odds print a warning.
* Each fill goes to **`data/fills.csv`** (committed, append-only). It also goes to the alerts DB columns
  (`taken_odds`, `taken_stake`, `fill_status`, `fill_reason`, `filled_at`) when that DB is present locally.

**GitHub Actions user (no local DB):**

The alerts DB lives in the Actions cache and the run artifact, so it can't be written from your side. The
durable record is the committed `data/fills.csv`. Either:

* open `data/fills.csv` in the GitHub web editor (pencil icon) and append one row per alert, then commit to
  the branch the workflow runs on:
  ```
  signal_id,fill_status,taken_odds,taken_stake,fill_reason,filled_at
  abc123def456,filled,2.10,10,,2026-10-29T19:52:00+00:00
  0f9e8d7c6b5a,skipped,,,line_moved,2026-10-29T20:05:00+00:00
  ```
  `fill_status` is `filled` or `skipped`. A skip has no odds. `filled_at` is ISO UTC, and the latest row per id
  wins. Or:
* download the run artifact `quantbet-<run_id>` (Actions → run → Artifacts), unzip it into `data/`, and run
  `record_fill.py` locally. `--list` then shows the unfilled alerts. Commit `data/fills.csv` afterwards.
  Without the artifact, `--no-db` writes the CSV row without the id check.

**Audit:**

```bash
python scripts/audit_run.py --log logs/live_<run_id>.log --alerts-db data/quantbet.sqlite3 \
    --http-db data/http_cache.sqlite3 --fills data/fills.csv --summary-db data/espn_summary.sqlite3
```

* **Fills line:**
  * fill rate, over all staked alerts and over the recorded ones;
  * skip reasons;
  * average slippage versus the card's minimum odds (in odds and %), and how many fills were below the
    minimum;
  * **realized** P&L at the taken odds and stakes, against the **theoretical** P&L at the minimum odds, both
    staked and 1u flat.
* **Provenance line:** true pre-match vs in-play-fetched priors, plus a pre-match reference EV. This is the
  EV difference between the latest pre-match DK line and the prior that was actually used, for Goals only.
  It is **not** a Bet365 in-play CLV: Bet365 prices are never read.

### Forward-run checklist

1. Keep `QB_ALERT_RULES=v4.5-sharp`. The pre-flight pytest checks the frozen hash on every run.
2. Check that each run log shows `dedup hydrated from alerts DB` (0 rows on the very first run is normal) and
   `checkpointed data/quantbet.sqlite3`.
3. Record every staked card, filled or skipped, within the day. Leave paper cards alone.
4. Once a week, download the latest artifact and run `audit_run.py --fills --summary-db`. Compare realized
   with theoretical and watch the fill rate. Do not change thresholds during the 2 weeks.

## v4.4-syndicate (opt-in since v4.5: `QB_ALERT_RULES=v4.4-syndicate` / `--rules v4.4-syndicate`)

Built on v4.3-global (registry, tiers, Live Stats Integrity Gate, tier sizing, `[LIQUIDITY: TIER 3 - SMALL LEAGUE]`,
`[PROBATION: n/150]`). The frozen v4.2 and v4.3-global blocks and hashes are untouched; v4.4 has its own freeze
under `frozen_sets["v4.4-syndicate"]` (`scripts/freeze_params.py --rules v4.4-syndicate`). That freeze hashes
`SyndicateParams` and the used part of `config/syndicate_priors.json`. Code: `src/quantbet/syndicate.py`.

* **Selectivity** - EV hurdle at the MINIMUM BET365 ODDS: TIER_1 4.0%, TIER_2 5.5%, TIER_3 7.0%. The abort floor
  is 0.6 × the hurdle. The model must also beat the prior by (1+h)/(1−m), where m is an ASSUMED per-side Bet365
  margin by tier. Overs need a net λ multiplier ≥ 1.10 and a lift ≤ 45%, plus **sustained momentum**: a shots,
  SOT and corners index ≥ 1.70× league pace on every poll of the last 4' (12' window). This replaces
  `activity_high`. State weights: favourite trailing ×1.15, level knockout tie after 55' ×1.10, 2+ goal lead ×0.80.
* **Unders** (half lines only, inside the windows) - lower tail of the same Poisson/NegBin. Triggered by
  `tempo_stall` (first-half dead pace since 25') or `bus_parking` (≥55', one-goal lead, trailing side has ≥65%
  of the pressure, goals only). Bullish states suppress Unders; bearish states suppress Overs.
* **Cards** - ESPN fouls, yellows and reds (yellow = 1, red = 1). If any of them is missing, there is no alert.
  The remaining λ is Poisson from Football-Data cards/fouls baselines, adjusted by foul and booking factors. An
  urgency multiplier of ×1.11 applies at 60'–85' when |gd| ≤ 1 (from the ESPN 27-Oct finals). O/U half lines use
  the same Kelly ladder and count toward the 5% match cap.
* **Dedup** - 15' cooldown per match × family. A re-alert needs a ≥0.5 line move or a ≥4pp p shift. Max 1
  goals + 1 corners + 1 cards signal per match.
* **Cards text** - every alert carries `[BOOST TARGET]` (pre-boost price for a 25% winnings boost:
  1 + (O_min − 1)/1.25). It also carries `[BET BUILDER / 3-WAY COMBO]`: pairwise Fréchet-clipped joint, a
  3-leg seeded Gaussian-copula Monte Carlo, ρ priors from Football-Data, and fair / naive / Minimum Bet365 Combo
  Odds. Combos are informational, and any stake stays inside the 5% cap.
* Replay of the 27-Oct run: `python scripts/replay_syndicate.py --help`. The audit is in `AUDIT.md` of the upgrade bundle.

## v4.3-global layer (opt-in: `QB_ALERT_RULES=v4.3-global` / `--rules v4.3-global`)

The v4.2 maths is **unchanged** (Overs only, +6 % lift gate, bearish dampeners suppress Overs only, minute gates
30'-43' / 55'-85', GD >= 3 blocks Overs, 5 % match cap, max 1 goals + 1 corners alert per match, best line per family,
quarter-Kelly, EV >= +2.5 %, abort +1.5 %). v4.3-global adds a *coverage and sizing* layer around it, frozen separately
under `frozen_sets["v4.3-global"]` in `config/engine_b_params.json` (sha256 `971ab88662be2f23…`, 2026-10-25T23:49:44Z).
The top-level v4.2 entry (`40e806b9…`) is untouched; v4 / v4.1 / v4.2 still run.

* **League registry** - `config/leagues.json` (copy: `config/leagues.example.json`): 45 ESPN slugs with tier, ESPN id,
  corner baseline, goals baseline and Football-Data code. 44 verified on ESPN; `kor.1` returns HTTP 400 and is marked
  `supported: false`.
* **Liquidity tiers** - TIER_1 (top 5, UCL, UEL, MLS, Brasileirão), TIER_2 (second divisions of the big five, Eredivisie,
  Liga MX, Liga Profesional, Nordics, A-League, J1, Portugal, Belgium, Turkey, Scotland, Austria, Switzerland, Greece, UECL,
  Libertadores, domestic cups), TIER_3 (Série B, Colombia, Chile, Sudamericana, CSL, AFC CL, League One and every
  **auto-discovered** league). Tiering is a conservative judgement about market depth, not a measurement.
* **Auto-discovery** - live events in `soccer/all/scoreboard?limit=500` from leagues not in the registry are ingested as
  `espn.<leagueId>`, TIER_3, default baselines (corners 9.8, goals 2.70). (Without `?limit=` ESPN truncates at 100 events.)
* **Live Stats Integrity Gate** - from minute 30 a match needs the stats keys (`wonCorners`, `totalShots`) **and**
  total shots > 0 or total corners > 0, otherwise it is dropped with the log line `skipped: no_live_telemetry`
  (`why` = `missing_keys` | `all_zero`, once per match and reason). This catches feeds that publish fake zeros.
* **Priors** - ESPN pickcenter (DraftKings) total + 1X2, de-vigged -> `lambda_goals_prior` and `fav_prob`.
  No pickcenter -> league goals/corner baseline, marked `confidence: low`; low-confidence priors never alert
  (the v4.2 ratings-confidence gate blocks `league_default`). The Football-Data team table is used only for `eng.1`
  (a Uruguayan "Liverpool" must never pick up the EPL ratings).
* **Tier sizing** - TIER_3 stake = 0.5 x quarter-Kelly (effectively 1/8 Kelly), capped at 15 units (150 at unit 10),
  rounded to 5/10, and still inside the 5 % match cap. At a 2,000 bankroll the 3 % per-alert cap (60) is lower than 15
  units, so the 15-unit cap only binds for bankrolls above ~5,000.
* **Card tag** - every alert shows `[LIQUIDITY: TIER 1 - MAJOR LEAGUE]`, `[LIQUIDITY: TIER 2 - MID LIQUIDITY]` or
  `[LIQUIDITY: TIER 3 - SMALL LEAGUE]`.
* **Corner baselines** - user values kept: aus.1 11.2, eng.2 10.4, bra.1 10.5, usa.1 9.8, mex.1 9.7, arg.1 9.6,
  jpn.1 9.6, default 9.8. Checked against Football-Data (`scripts/fd_league_report.py`, report in
  `dryrun/football_data_leagues_report.md`): eng.2 (FD E1 2025/26 10.35) agrees; the others cannot be checked because the
  Football-Data "extra" files (ARG/BRA/USA/MEX/JPN) have no corner columns and there is no A-League file.
  Flagged, not changed: the v4.2 Serie A default (9.8) vs FD I1 2025/26 8.82.
* **Freeze** - `scripts/freeze_params.py --rules v4.3-global` writes the frozen set; any change to the registry
  tiers/baselines or GlobalParams fails `enforce_frozen` unless `--unfreeze`.

**Not validated out of sample.** The v4.2 calibration was done on European top-flight data; nothing has been
back-tested for the Americas/Asia/second divisions. Treat v4.3-global alerts as paper trades until you have a sample.

### GitHub Actions (`.github/workflows/live_quantbet.yml`)

Cron every 6 h + `workflow_dispatch`, `timeout-minutes: 350`, `concurrency` with `cancel-in-progress: false`,
secrets `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` (missing -> `--dry-run`), SQLite state via `actions/cache`,
logs + SQLite via `upload-artifact`. Caveats: a hosted job is killed at 6 h, so there are gaps between runs; private
repos get 2,000 free minutes/month, so 24/7 (~43,800 min) is **not** $0; public repos are free but expose code
and artifacts; scheduled workflows are disabled after 60 days of repo inactivity; ESPN may 403 datacenter IPs.
A $0 24/7 alternative is an Oracle Cloud Always Free VM running `scripts/run_live.sh` (not set up here).

## v4.2 rules (default, frozen 2026-10-25T19:31:39Z)

`QB_ALERT_RULES=v4.2` = v4.1 **Overs only** (`allow_unders=false`; Under code kept, disabled) with the same
positive-lift gate (net ≥ 1.06 and fair ≤ prior_fair/1.06; net ≤ 1.05 suppresses the family). Dampeners only
shield Overs. Corners: piecewise half profile, H2/H1 per-minute ratio 1.064 (StatsBomb open data, 354 matches),
used by model and prior (normalised, total prior unchanged); Gamma-Poisson shrinkage measures the observed pace
against the half-adjusted expected corners so far and, before 55', may only raise λ. Lead dampeners refitted
(goals 1.033 → capped 1.00, i.e. none; corners 0.952). Calibration: `python scripts/statsbomb_corners.py
--per-league 80` + `python scripts/calibrate_v42.py` (also `scripts/calibrate.py --v42`) → `config/calibration_report.json` `v4_2`.

Replay / grading (all rule sets in one command):

    python scripts/replay_all.py                                       # Old/v4/v4.1/v4.2 → dryrun/report_<date>_v4_2.md
    python scripts/replay_all.py --versions v4 v4.1 v4.2 --session evening   # → dryrun/report_<date>_evening_grading.md

## v4.1 rules (superseded)

`QB_ALERT_RULES=v4.1` = v4 plus a positive-lift gate (reverse-pricing mode, i.e. no bookmaker odds for the line):
an Over fires only if `fair_model <= prior_fair / 1.06` for that exact line **and** the family's
`net_multiplier = λ_model_remaining / λ_prior_remaining >= 1.06` (prior = time-decayed pre-match rate from the
current state, no in-play multipliers, no shrinkage; corners net includes Gamma-Poisson shrinkage and the chasing
cap); net ≤ 1.05 suppresses every Over of that family. Unders (1.75–2.20, closest to 1.95) may fire only when
`λ_model < λ_prior` and `fair_under <= prior_fair_under / 1.06`; they share the 1 goals + 1 corners per match cap,
5 % stake cap and minute windows, and are **allowed** in blowouts (|diff| ≥ 3 blocks Overs only). The lift gate
replaces v4's trigger/direction gate. Asian quarter lines settle half/half; pushes refund. Replay:
`python scripts/replay_v41.py` → `dryrun/report_<date>_v4_1.md`. `--rules v4` still runs while its hash matches the
`superseded` entry in the params file.

## v4 rules and LIVE mode

`QB_ALERT_RULES=v4` (v4.1 / v3 / legacy stay selectable). v4: reverse-pricing picks the single line per family whose fair
odds are closest to 1.95 within 1.75–2.20 (none in range → no alert); with real bookmaker odds attached it picks by
expected log-growth at those odds. Goals: time-decayed prior × bounded multipliers only (momentum clipped to
0.85–1.25; no observed goal pace). Corners: Gamma-Poisson shrinkage `w = elapsed/(elapsed+180)`. Hard cap 1 goals +
1 corners alert per match (no state-change bypass), 5 % match stake cap. |diff| ≥ 3 → no Overs for the rest of the
match; |diff| = 2 → goals ×0.75, corners ×0.80 (both teams). Favourite trailing by 1 after 60' → chasing corner boost
capped at 1.03× unless FotMob shots-inside-box confirms dominance.

Params are frozen in `config/engine_b_params.json` (`rules_version`, `frozen_at`, `sha256`); the runner logs the
hash at startup and refuses to run v4.2 (or v4.1 / v4) with different params unless `--unfreeze`. Re-freeze: `python
scripts/freeze_params.py --rules v4.2` (moves the previous hash to `superseded`). Replay old/v3/v4: `python scripts/replay_v4.py` → `dryrun/report_<date>_v4.md`.

LIVE (Telegram on; needs `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` in `.env`; keep-alive + heartbeat):

    mkdir -p logs && nohup scripts/live.sh >> logs/live.log 2>&1 &       # Linux (systemd-inhibit if permitted)
    mkdir -p logs && nohup scripts/live.sh >> logs/live.log 2>&1 &       # macOS (caffeinate -i via the same wrapper)
    python scripts/live.py --live --rules v4.2 --cups         # direct; keep-alive started in-process

## Keep-alive, heartbeat and gap detection

Start long runs through the wrapper:

    QB_ALERT_RULES=v3 nohup scripts/run_live.sh --until 2026-10-25T22:45 \
        --kickoff-from 2026-10-25T19:00 --kickoff-cutoff 2026-10-25T20:15 --tag evening \
        >> dryrun/run_2026-10-25_evening.log 2>&1 &

* Linux: `systemd-inhibit --what=idle:sleep --why='quantbet live poller'` (probed first; if logind/D-Bus is
  unavailable or not permitted, e.g. in a container, it runs without an inhibitor and says so).
* macOS: `caffeinate -i`.
* Windows: `quantbet.heartbeat.prevent_sleep_windows()` calls `SetThreadExecutionState`
  (`ES_CONTINUOUS | ES_SYSTEM_REQUIRED`) via ctypes; guarded by platform, no-op elsewhere.
* **None of these can prevent a VM / container / host-level pause** (like the 14:35–16:45 UTC pause of
  the box on 2026-10-25). For that, the runner keeps a heartbeat: one row `(ts, ticks, live matches)` per
  60 s in `dryrun/heartbeat.sqlite3`; every 15 s it compares wall-clock and monotonic time with the previous
  tick, and a gap > 180 s is written to the `gaps` table (start, end, duration, live matches and their last
  known minute) and triggers an immediate scoreboard refresh (still via PoliteFetcher pacing/backoff).
  Gaps across restarts are detected from the last heartbeat. The paper report lists all gaps.

## Limits & caveats

* **Undocumented endpoints.** ESPN's site API, FotMob and SofaScore have no
  public, documented API for this use. Their endpoints and page structure
  **can change or be withdrawn at any time**, and automated access **may be
  against those sites' terms**. Check their terms before you run this. All
  URLs are configurable (`QB_ESPN_BASE`, `QB_SOFASCORE_BASE`, `QB_FOTMOB_BASE`,
  `FotmobPageConfig`), and each source can be switched off.
* **ESPN can refuse too.** On 25 Oct 2026 every request whose User-Agent
  contained the placeholder `; contact: you@example.com` got
  `403 Access Denied`. The plain identifiable UA
  `quantbet-signal-engine/0.2 (personal, read-only)` got 200. That UA is now
  the default. If you add contact details, test them with one request. Never
  switch to a browser UA. QuantBet backs off on 403 (60 s → 120 s → … 1 h)
  and sends no alerts while refused.
* **ESPN has no xG, dangerous attacks or shots inside the box.** Momentum
  then comes from the `DeltaTracker` (weights SOT 3, shots 1, corners 1.5,
  normalised by pre-match expected rates). These weights are **uncalibrated
  heuristics**. Early in a match (under 3 minutes of history) it uses
  whole-match averages. Log states and tune before trusting it.
* **ESPN pickcenter odds are DraftKings (US) lines,** used only as a pre-match
  goals prior when no Pinnacle snapshot exists. They are never shown as a
  Bet365 price.
* **API-Football free plan** is 100 calls/day and 10/min. Its free plan
  restricts the seasons you can access, and since 2026 it is reported to
  exclude current-season fixtures. If the API returns a plan, season or limit
  error, the adapter turns itself off until the next UTC day, and ESPN keeps
  working.
* **Blocks are respected, not evaded.** If a source answers 403, 429 or 503,
  or serves a challenge page, QuantBet backs off exponentially (60 s up to
  1 h), logs a warning and fails over to the next source. There is no
  TLS-fingerprint impersonation, no captcha solving and no proxy or
  User-Agent rotation. `curl_cffi` is only ever a plain client, never with
  `impersonate`. SofaScore returned 403 to an honest client and is off by
  default. If every source refuses, you get no live alerts. That is the
  expected, safe outcome.
* **Low request rate.** Each URL (per match, per source) is fetched at most
  once every 15 s, with at most 2 requests in flight, jitter on top, and
  caching. Don't lower these.
* **No live Bet365 price.** Free mode cannot see Bet365. Every alert is a
  *conditional* instruction: bet only if the app shows at least the minimum
  odds. Layer 2 (the SofaScore odds widget) is off by default, and SofaScore
  does not say which bookmaker those odds come from.
* **Leagues without a team table** (only EPL ships calibrated) rely on the
  ESPN/Pinnacle prior for goals and on league-default corner rates.
* **Stale priors.** Pinnacle prices from the free tier can be hours old.
  Football-Data ratings are season averages with shrinkage.
* **Calibration.** Engine B's in-play multipliers (momentum, chasing, red
  cards) are priors, not fitted values. Only corner dispersion and goal
  correlation are fitted. On EPL 2025/26, total corners came out almost
  Poisson (r ≈ 150). Paper-trade and track CLV before risking money.
* **Bet365 is hard to beat.** In EPL 2025/26, Bet365 pre-closing 1X2/O2.5
  prices averaged about −5 % EV against Pinnacle's no-vig close. Real edges
  are rare and short-lived, and winning accounts get limited.
* **Paid adapters** (Odds-API.io, BetsAPI, API-Football, Sportmonks) are still
  in the code but disabled. Set `QB_MODE=paid` and the keys to use them.
* **No profit guarantee.** Positive EV is a model estimate. Variance is large.

## Responsible gambling

Only bet money you can afford to lose, set deposit limits with your
bookmaker, and stop if betting stops being fun. Help: BeGambleAware
(UK, begambleaware.org), 1-800-GAMBLER (US), or your local support service.
