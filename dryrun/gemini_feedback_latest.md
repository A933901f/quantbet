# Gemini feedback

> Advisory, untrusted model output - review before acting on anything below.

- timestamp_utc: 2026-10-26T00:04:29Z
- model: gemini-flash-latest
- auth_mode: x-goog-api-key
- http_status: 200
- latency_s: 44.86
- reports: dryrun/report_2026-10-25_v4_2.md
- prompt: Handshake test: confirm you can read v4.2 report and give next instructions for global leagues expansion

---
Handshake confirmed. Report `v4_2` read and audited.

### Quantitative Audit Summary
1. **BSS & Statistical Significance**: v4.2’s $\text{BSS} = +0.137$ is entirely an artifact of $n=6$ in-sample afternoon alerts. Out-of-sample validation is currently $n=0$. Suppressing Unders in v4.2 masked the negative skill ($\text{BSS} = -0.070$ on v4.1 Unders) rather than fixing tail calibration.
2. **Poisson vs. Negative Binomial**: Corners exhibit empirical variance-to-mean ratios of $1.3\text{--}1.8$. Poisson understates tails (mispricing extreme lines and deep Unders). Gamma-Poisson shrinks $\lambda$, but pricing lines requires a Negative Binomial distribution with dispersion parameter $r$ tuned per league.
3. **Kelly Covariance**: Alerts on the same match across markets (e.g., Angers v Lorient min 31 Corners + min 80 Goals) treat outcomes as independent. Game-state shocks (red cards, pitch conditions, match tempo) create non-zero covariance. 
4. **Pipeline Health Critical Failure**: Logged gaps (e.g., 14:35–16:46 UTC) mean the engine went dark for 131 consecutive minutes during peak match volume.

---

### Engineering Action Plan: Global Leagues Expansion

Execute the following sequential sprints:

#### Sprint 1: Ingestion Resilience & Data Pipeline Watchdog
- **Fix Data Gaps**: Bet365 in-play scraper/websocket client must run under a supervised `systemd` daemon with exponential backoff reconnect and automated zero-byte log heartbeat alerts (warn after 60s silence, hard restart after 180s).
- **Match State Re-sync**: On reconnection, state transitions must query an API snapshot before updating $\lambda$ to prevent ghost pace spikes caused by elapsed-time jumps during downtime.

#### Sprint 2: Global Prior Ingestion ($0/month Stack)
- Expand beyond Top-5 European leagues to include Tier-2/3 and global leagues (e.g., Brasileirão, J-League, MLS, Scandinavian leagues, Championship).
- **Automated Pre-match Ingestion**:
  1. Pull pre-match Asian handicap, closing totals, and team ratings from `football-data.co.uk` extended CSVs (`B1`, `D2`, `E1`, `N1`, `SP2`, `SC0`, etc.).
  2. Implement an automated fallback pre-match scraper (e.g., OddsPortal / Sofascore public APIs) to fetch pre-match lines ($O/U$, spreads) for leagues not covered by Football-Data.
- **League Clustering**: Cluster leagues into 4 prior tiers based on average goal expectancy ($\mu_G$) and corner expectancy ($\mu_C$) (e.g., Low-tempo/Under-biased vs. High-event leagues) rather than forcing Top-5 European profiles globally.

#### Sprint 3: Distribution & In-Play Time Recalibration
- **Retire 2015/16 StatsBomb Baselines**: Modern stoppage time (post-2023 directives) routinely adds 7–12 minutes. An H2 corner ratio fitted on 2015/16 severely under-weights late-game Poisson volume. Fit H2/H1 ratios dynamically on recent seasons (2023–2026 data).
- **Negative-Binomial Parameterization**:
  $$P(X = k) = \frac{\Gamma(k + r)}{k!\,\Gamma(r)} (1 - p)^r p^k, \quad \text{where } p = \frac{\lambda}{\lambda + r}$$
  Calibrate league-level dispersion parameter $r$ ($r \approx 4\text{--}6$ for corners). Re-evaluate Unders pricing under NegBin before re-enabling Unders.

#### Sprint 4: Portfolio Kelly & Match-Level Risk Limits
- **Covariance Penalty**: Replace independent single-bet Quarter-Kelly stakes with match-level exposure caps:
  - Max simultaneous risk per single match across all families (Goals + Corners) capped at $0.05 \times \text{Bankroll}$ (or equivalent fractional unit ceiling).
  - If a Goals line is already active, discount the edge ($\text{EV}_{\text{adj}} = \text{EV} \times 0.75$) for subsequent corner bets on the same match to penalize unmodeled correlated game flow.

**Immediate Next Task**: Deploy Sprint 1 watchdog and provide the schema for the multi-league prior database. Confirm readiness to proceed.
