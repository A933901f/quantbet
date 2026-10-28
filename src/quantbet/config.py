"""Runtime configuration loaded from environment variables / ``.env``."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All tunables of the signal engine. Every field can be overridden via env."""

    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="QB_", extra="ignore", env_nested_delimiter="__"
    )

    # --- API credentials (read from env; never hard-code) -------------------
    the_odds_api_key: str | None = Field(default=None, validation_alias="THE_ODDS_API_KEY")
    odds_api_io_key: str | None = Field(default=None, validation_alias="ODDS_API_IO_KEY")
    betsapi_token: str | None = Field(default=None, validation_alias="BETSAPI_TOKEN")
    api_football_key: str | None = Field(default=None, validation_alias="API_FOOTBALL_KEY")
    sportmonks_token: str | None = Field(default=None, validation_alias="SPORTMONKS_TOKEN")
    telegram_bot_token: str | None = Field(default=None, validation_alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, validation_alias="TELEGRAM_CHAT_ID")

    # --- Mode ----------------------------------------------------------------
    # "free" ($0/month): ESPN stats (+ optional API-Football / FotMob page) + The-Odds-API free tier +
    # Football-Data.co.uk. "paid": the original adapters (code kept).
    mode: str = "free"

    # --- Free stack ------------------------------------------------------------
    # Honest, identifiable User-Agent (put a contact address in it).
    user_agent: str = "quantbet-signal-engine/0.2 (personal, read-only)"  # ESPN 403s the placeholder "; contact: you@example.com" form
    http_backend: str = "aiohttp"  # aiohttp | curl_cffi (plain client, never impersonate)
    polite_min_interval_s: float = 15.0  # per URL (=> per match per source)
    polite_global_concurrency: int = 2
    polite_global_gap_s: float = 1.0
    polite_jitter_s: float = 3.0
    polite_backoff_base_s: float = 60.0
    polite_backoff_max_s: float = 3600.0
    http_cache_path: Path = Path("data/http_cache.sqlite3")
    # Tier 1: ESPN public site API (primary; verified 2026-10-25)
    enable_espn: bool = True
    espn_base: str = "https://site.api.espn.com/apis/site/v2/sports/soccer"
    espn_leagues: list[str] = Field(
        default_factory=lambda: ["eng.1", "esp.1", "ger.1", "ita.1", "fra.1", "uefa.champions", "uefa.europa"]
    )
    espn_use_all: bool = True  # one soccer/all/scoreboard per cycle, filtered in memory
    espn_all_poll_s: float = 60.0  # + U(2, 5) s jitter
    espn_summary_once: bool = True  # /summary once per match (DraftKings prior), never in play
    espn_summary_db_path: Path = Path("data/espn_summary.sqlite3")
    espn_scoreboard_poll_s: float = 60.0  # per league while live/about to start (30-60 recommended)
    espn_idle_poll_s: float = 600.0  # per league when nothing is live within 15 min
    espn_summary_poll_s: float = 60.0  # per in-play match
    # plumbing (forward run, not model params): pre-kick-off /summary priority for listed leagues
    espn_prekick_window_min: float = 45.0  # scheduled matches within 45 min of kick-off get their own budget
    espn_prekick_summaries_per_cycle: int = 4  # extra /summary requests per ~60 s cycle for those matches
    espn_prekick_refresh_min: float = 10.0  # one late pre-match refresh inside the last 10 min (0 = off)
    # plumbing: rebuild the syndicate dedup / match caps from the alerts DB on start-up (3 h back)
    dedup_hydrate_lookback_s: float = 10800.0
    # lines patch: [PAPER ONLY] signals (v4.5 Total Cards) are always written to the alerts DB for offline
    # grading but are NOT sent to Telegram unless this is true (QB_SEND_PAPER_ALERTS=true)
    send_paper_alerts: bool = False
    espn_use_summary: bool = True
    espn_use_pickcenter_prior: bool = True  # DraftKings pre-match lines on ESPN as a ratings prior
    synthetic_momentum: bool = True  # DeltaTracker momentum when no graph momentum exists
    # Tier 2: API-Football free (needs API_FOOTBALL_KEY; off without it)
    enable_api_football_free: bool = True
    api_football_daily_limit: int = 100
    api_football_use_limit: int = 90
    api_football_db_path: Path = Path("data/api_football.sqlite3")
    # Tier 3: FotMob match page (__NEXT_DATA__) enricher - opt-in
    enable_fotmob_page: bool = True  # gated: only EV-at-reference >= fotmob_ev_gate, minute 50-85
    fotmob_page_interval_s: float = 300.0
    fotmob_page_only_flagged: bool = True
    fotmob_ev_gate: float = 0.010  # EV of the full model at the neutral reference price
    fotmob_gate_minutes: tuple[float, float] = (50.0, 85.0)
    fotmob_drop_contradicted: bool = True
    drop_negative_reference_ev: bool = True  # live activity must not argue against the bet
    # Legacy fallbacks (disabled by default; plain client, never impersonated)
    enable_sofascore: bool = False
    enable_fotmob: bool = False
    enable_sofascore_odds: bool = False  # Layer 2, opt-in
    sofascore_odds_provider_id: int = 1
    sofascore_odds_label: str = "sofascore_provider"  # set "bet365" only if verified
    sofascore_base: str = "https://api.sofascore.com/api/v1"
    fotmob_base: str = "https://www.fotmob.com/api/data"
    free_max_live_events: int = 6
    free_league_filter: list[str] = Field(default_factory=list)
    stats_poll_s: float = 30.0
    list_poll_s: float = 60.0
    # The-Odds-API free tier (Layer 1)
    odds_monthly_budget: int = 500
    odds_budget_reserve: int = 20
    odds_priority_window_h: float = 3.0
    odds_min_refresh_s: float = 3 * 3600.0
    odds_cache_path: Path = Path("data/odds_cache.sqlite3")
    the_odds_api_eu_bookmakers: list[str] = Field(
        default_factory=lambda: ["pinnacle", "betfair_ex_eu", "williamhill", "unibet_eu", "marathonbet", "onexbet"]
    )
    football_data_ratings_path: Path = Path("config/team_ratings.json")
    engine_b_params_path: Path = Path("config/engine_b_params.json")
    # Pricing (model-only MIN-ODDS alerts)
    target_edge: float = 0.025
    abort_edge: float = 0.015
    min_alert_minute: float = 10.0
    max_alert_minute: float = 86.0
    realert_cooldown_s: float = 600.0
    # v3 alert rules (de-correlation, minute windows, shrinkage, game-state dampener,
    # chasing-corner penalty). "legacy" restores the pre-25-Oct-2026 policy.
    alert_rules: str = "v4.5-sharp"  # legacy | v3 | v4 | v4.1 | v4.2 | v4.3-global | v4.4-syndicate | v4.5-sharp (v4.x checked against config/engine_b_params.json)
    leagues_config_path: Path = Path("config/leagues.json")  # v4.3-global league registry (tiers, baselines)
    # v4.4-syndicate / v4.5-sharp priors (rho table for Bet Builder legs, card baselines, cards NegBin r);
    # the used part is hashed into the v4.4 / v4.5 freezes.
    syndicate_priors_path: Path = Path("config/syndicate_priors.json")
    # League probation counts (quantbet.probation; update with scripts/update_probation.py). Card tag only.
    probation_db_path: Path = Path("data/league_probation.sqlite3")
    max_match_stake_frac: float = 0.05
    family_cooldown_min: float = 15.0

    # --- Paid providers (code kept, disabled by default) ------------------------
    enable_the_odds_api: bool = True  # free tier only (500 credits/month)
    enable_odds_api_io: bool = False
    enable_odds_api_io_ws: bool = False
    enable_betsapi: bool = False
    enable_api_football: bool = False
    enable_sportmonks: bool = False

    the_odds_api_sports: list[str] = Field(
        default_factory=lambda: [
            "soccer_epl",
            "soccer_spain_la_liga",
            "soccer_germany_bundesliga",
            "soccer_italy_serie_a",
            "soccer_france_ligue_one",
            "soccer_uefa_champs_league",
        ]
    )
    # NOTE: The-Odds-API does not list a European "bet365" key (only bet365_au,
    # AFL/NRL). It is used here as the sharp feed (Pinnacle + Betfair Exchange).
    the_odds_api_bookmakers: list[str] = Field(
        default_factory=lambda: ["pinnacle", "betfair_ex_eu"]
    )  # paid-mode adapter only
    odds_api_io_bookmakers: list[str] = Field(default_factory=lambda: ["Bet365", "Betfair Exchange"])
    odds_api_io_markets: list[str] = Field(
        default_factory=lambda: ["ML", "Spread", "Totals", "Corners Totals"]
    )
    target_bookmaker: str = "bet365"
    sharp_bookmakers: list[str] = Field(
        default_factory=lambda: ["pinnacle", "betfair_ex_eu", "betfair exchange", "betfair_ex_uk"]
    )

    # Poll intervals (seconds) and rate limits (requests/second, burst).
    poll_interval_prematch: float = 60.0
    poll_interval_live: float = 5.0
    poll_interval_stats: float = 15.0
    rate_the_odds_api: float = 0.2
    rate_odds_api_io: float = 1.3  # 5,000 req/h ~ 1.39 req/s
    rate_betsapi: float = 0.9  # 3,600 req/h = 1 req/s
    rate_api_football: float = 0.5
    rate_sportmonks: float = 0.5
    http_timeout: float = 4.0

    # --- Engine --------------------------------------------------------------
    ev_threshold: float = 0.025
    min_edge_for_floor: float = 0.015
    devig_method: str = "power"  # multiplicative | additive | power | shin
    max_sharp_age_s: float = 30.0
    max_target_age_s: float = 10.0
    steam_window_s: float = 60.0
    steam_mode: str = "either"  # relative | absolute | either
    steam_rel_threshold: float = 0.05
    steam_abs_threshold_pp: float = 0.03
    steam_cooldown_s: float = 120.0
    engine_b_mode: str = "fallback"  # fallback | always | off
    max_odds: float = 15.0

    # --- Risk ----------------------------------------------------------------
    bankroll: float = 2000.0
    unit_size: float = 10.0
    currency: str = "EUR"
    kelly_multiplier: float = 0.25
    max_stake_fraction: float = 0.03
    round_small_step: float = 5.0
    round_large_step: float = 10.0
    round_switch_at: float = 100.0
    high_liquidity_leagues: list[str] = Field(
        default_factory=lambda: [
            "premier league",
            "la liga",
            "bundesliga",
            "serie a",
            "ligue 1",
            "champions league",
            "europa league",
            "conference league",
            "eredivisie",
            "primeira liga",
            "championship",
        ]
    )

    # --- Storage / runtime ---------------------------------------------------
    db_path: Path = Path("data/quantbet.sqlite3")
    log_level: str = "INFO"
    log_json: bool = True
    dry_run: bool = False  # if True, notifications are logged, not sent


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
