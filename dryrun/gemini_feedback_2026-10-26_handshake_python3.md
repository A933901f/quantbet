# Gemini feedback

> Advisory, untrusted model output - review before acting on anything below.

- timestamp_utc: 2026-10-26T00:03:33Z
- model: gemini-flash-latest
- auth_mode: x-goog-api-key
- http_status: 200
- latency_s: 110.53
- reports: dryrun/report_2026-10-25_v4_2.md
- prompt: Handshake test: confirm you can read v4.2 report and give next instructions for global leagues expansion

---
### 1. Audit & Quantitative Assessment (v4.2 Report)

* **BSS & Sample Validity:** 
  * v4.2 claims BSS $+0.137$ and Log-Loss skill $+0.107$, but sample size is **$n=6$** across 4 matches. This is pure in-sample survivor bias created by eliminating Unders (which was $-22.1\%$ ROI in v4.1) and raising the net multiplier gate to $>1.05$.
  * Evening target matches (*Udinese v Milan*, *El Clásico*) yielded **0 logged states** due to scraper/cutoff issues; true out-of-sample skill remains unverified.
* **Model Calibration (Poisson / Negative-Binomial):**
  * Goals Poisson assumption holds moderately well with dampener $\approx 1.00$, but corners exhibit significant empirical overdispersion ($\text{Var}/\text{Mean} \approx 1.3 - 1.8$). Using a Poisson rate multiplier without modeling the dispersion parameter ($\alpha$) under-prices tail risk on alternate corner lines.
  * The StatsBomb 2015/16 baseline ($n=354$) for corner half-decay ($H2/H1 = 1.064$) is stale and biased toward Big 5 European leagues. It cannot be applied globally.
  * Gamma-Poisson shrinkage parameter (`eff / (eff + 180)`) is arbitrary and overly aggressive before minute 55.
* **Kelly Staking Independence:**
  * Quarter-Kelly currently evaluates bets independently. In *Internazionale v Fiorentina* and *Angers v Lorient*, simultaneous or sequential bets were triggered on both Goals and Corners. Correlation between goal state transitions and corner rates violates univariate Kelly assumptions and risks compounding drawdown.
* **Pipeline Health:**
  * Box pauses (4 outages totaling $>3.5$ hours) indicate infrastructure fragility. Zero states recorded for scheduled evening matches points to broken fixture discovery or scheduling crons.

---

### 2. Next Engineering Instructions: Global Leagues Expansion

Execute the following sequential tasks in `quantbet`:

```
quantbet/
├── config/leagues.json             <-- Add dynamic league definitions & tiers
├── engine/models/
│   ├── corners_nb.py               <-- Negative Binomial corner engine
│   └── shrinkage.py                <-- Empirical Bayes conjugate updates
├── risk/portfolio_kelly.py         <-- Match-level & portfolio-level Kelly constraints
└── ingestion/pipeline_monitor.py   <-- Health check & heartbeat alerting
```

#### Task 1: Negative Binomial Engine & Hierarchical League Calibration
1. Deprecate single-parameter Poisson for corners in favor of Negative Binomial ($\lambda, r$), where variance is $\sigma^2 = \lambda + \frac{\lambda^2}{r}$.
2. Create `config/leagues.json` mapping leagues into tiers (Tier 1: Big 5 + UCL; Tier 2: Eredivisie, Championship, Primeira Liga, Brasileirão, etc.; Tier 3: Rest of World).
3. Do not apply top-5 default parameters globally. For leagues lacking half-time corner history, initialize priors using:
   $$\text{Prior } \lambda_{\text{corners}} = \text{de-vigged closing total}$$
   $$r_{\text{league}} = \text{fitted dispersion parameter (default: } 4.5 \text{)}$$
   $$\text{H2/H1 corner split} = 1.05 \text{ (Tier 1/2)}, 1.00 \text{ (Tier 3 unverified)}$$

#### Task 2: Portfolio Kelly & Correlated Exposure Guardrails
1. Implement a **Match Risk Budget**:
   * Hard cap maximum match exposure to **$0.025$ of bankroll** (2.5 units), regardless of individual quarter-Kelly outputs.
   * If an active position exists on a match (e.g., Over 2.25 Goals), scale down any subsequent corner bet size on the same fixture by $50\%$ to account for latent pace covariance.
2. Implement **Concurrent Bet Scaler**:
   $$f_{\text{allocated}, i} = f_i^* \times \min\left(1.0, \frac{\text{MaxGlobalBankrollRisk}}{\sum_{j=1}^K f_j^*}\right)$$
   Set `MaxGlobalBankrollRisk = 0.10` (10% max simultaneous open risk).

#### Task 3: Ingestion Pipeline Reliability & Fixture Discovery
1. Replace static match targets with automated calendar discovery via Bet365 event loop.
2. Implement a watchdog heartbeat in `ingestion/pipeline_monitor.py`:
   * If state stream drops for $>60$ seconds during active match windows, trigger an automatic headless browser/session restart.
   * Add automated match-state graduation: transition from `pre_match` to `in_play` automatically at $T=0$.

#### Task 4: Automated Backtest & Metric Gating
1. Refactor `report.py` to calculate:
   * **Tier-stratified BSS** (do not aggregate Tier 1 and RoW).
   * **Calibration curves** (reliability diagrams across deciles $[0.1, 0.2, \dots, 0.9]$).
2. Mandate minimum sample size gate before enabling live execution on new leagues:
   * Require $n \ge 150$ graded out-of-sample paper bets with $\text{BSS} > 0$ and empirical $\text{Brier} < 0.245$ before enabling live execution for any expanded league tier.
