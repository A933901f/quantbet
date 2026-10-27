"""Entry point: wires poller -> engine -> risk -> database -> notifier.

Run with ``python -m quantbet.main`` (after ``pip install -e .`` or with
``PYTHONPATH=src``). Stop with Ctrl+C / SIGTERM for a graceful shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import signal
import sys
import time
from datetime import datetime, timezone

import aiohttp

from .config import Settings, get_settings
from .database import AlertDatabase
from .engine import EngineConfig, FreeSignalEngine, LiveModelParams, PricingConfig, RatingsResolver, SignalEngine
from .models import MarketKey, MarketType, Selection
from .notifier import TelegramNotifier
from .poller import (
    APIFootballAdapter,
    BaseAdapter,
    BetsAPIAdapter,
    EventMatcher,
    OddsAPIAdapter,
    OddsApiIoAdapter,
    OddsApiIoWebSocket,
    Poller,
    PollerMessage,
    SportmonksAdapter,
)
from .risk import RiskConfig, RiskManager

log = logging.getLogger("quantbet")


class JsonFormatter(logging.Formatter):
    """One JSON object per line, including any ``extra=`` fields."""

    _std = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update({k: v for k, v in record.__dict__.items() if k not in self._std})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO", as_json: bool = True) -> None:
    """Configure root logging (JSON lines by default)."""
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter() if as_json else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=level, handlers=[h], force=True)


def build_adapters(s: Settings, session: aiohttp.ClientSession, matcher: EventMatcher) -> tuple[list[BaseAdapter], OddsApiIoAdapter | None]:
    """Instantiate adapters for which credentials are present."""
    adapters: list[BaseAdapter] = []
    odds_io: OddsApiIoAdapter | None = None
    t = s.http_timeout
    if s.enable_the_odds_api and s.the_odds_api_key:
        adapters.append(OddsAPIAdapter(session, s.the_odds_api_key, s.the_odds_api_sports, s.the_odds_api_bookmakers, rate_per_s=s.rate_the_odds_api, interval_s=s.poll_interval_prematch, timeout_s=t))
    if s.enable_odds_api_io and s.odds_api_io_key:
        odds_io = OddsApiIoAdapter(session, s.odds_api_io_key, s.odds_api_io_bookmakers, s.odds_api_io_markets, rate_per_s=s.rate_odds_api_io, interval_s=s.poll_interval_live, timeout_s=t)
        adapters.append(odds_io)
    if s.enable_betsapi and s.betsapi_token:
        adapters.append(BetsAPIAdapter(session, s.betsapi_token, rate_per_s=s.rate_betsapi, interval_s=s.poll_interval_live, timeout_s=t))
    if s.enable_api_football and s.api_football_key:
        adapters.append(APIFootballAdapter(session, s.api_football_key, matcher, rate_per_s=s.rate_api_football, interval_s=s.poll_interval_stats, timeout_s=t))
    if s.enable_sportmonks and s.sportmonks_token:
        adapters.append(SportmonksAdapter(session, s.sportmonks_token, matcher, rate_per_s=s.rate_sportmonks, interval_s=s.poll_interval_stats, timeout_s=t))
    return adapters, odds_io


async def consume(queue: asyncio.Queue[PollerMessage], engine: SignalEngine, risk: RiskManager, db: AlertDatabase, notifier: TelegramNotifier, stop: asyncio.Event) -> None:
    """Engine loop: evaluate every queued batch, size, log and notify."""
    while not stop.is_set():
        try:
            msg = await asyncio.wait_for(queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        t0 = time.perf_counter()
        if msg.kind == "stats":
            signals = []
            for st in msg.states:
                engine.update_match_state(st)
                signals.extend(engine.evaluate_live_event(st.event_key))
        else:
            signals = engine.process(msg.odds)
        for sig in risk.size_all(signals):
            await db.insert_signal(sig)
            await notifier.send_signal(sig)
            log.info("signal", extra={"id": sig.signal_id, "urgency": sig.urgency.value, "market": sig.describe_market(), "odds": sig.bookmaker_odds, "ev": round(sig.ev, 4), "stake": sig.stake_currency})
        log.debug("batch evaluated", extra={"provider": msg.provider, "ms": round((time.perf_counter() - t0) * 1000, 2)})
        queue.task_done()


async def closing_line_job(engine: SignalEngine, db: AlertDatabase, stop: asyncio.Event, every_s: float = 30.0) -> None:
    """At kick-off, store the sharp no-vig price as the closing line (CLV)."""
    while not stop.is_set():
        try:
            for a in await db.alerts_needing_close(datetime.now(timezone.utc).isoformat()):
                mk = MarketKey(a["event_key"], MarketType(a["market"]), a["line"])
                sel = Selection(a["selection"])
                # The last sharp no-vig quote seen before kick-off is the closing line.
                close = engine.closing_fair_odds(mk, sel)
                if close:
                    clv = await db.update_closing_line(a["id"], close, engine.current_target_odds(mk, sel))
                    log.info("closing line stored", extra={"id": a["id"], "close": close, "clv": round(clv, 4)})
        except Exception:  # noqa: BLE001
            log.exception("closing line job failed")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every_s)


def load_live_params(path: object) -> LiveModelParams | None:
    """LiveModelParams from a calibrate.py JSON file (flat) or a frozen file (``live_params``); unknown keys ignored."""
    from pathlib import Path

    p = Path(str(path))
    if not p.exists():
        return None
    raw = json.loads(p.read_text())
    if isinstance(raw, dict) and isinstance(raw.get("live_params"), dict):
        raw = raw["live_params"]
    names = {f.name for f in dataclasses.fields(LiveModelParams)}
    vals = {k: (tuple(v) if isinstance(v, list) else v) for k, v in raw.items() if k in names}
    return LiveModelParams(**vals)


def is_global(rules: str) -> bool:
    """True for the v4.3-global layer (league registry, telemetry gate, tier sizing)."""
    return rules.lower().startswith("v4.3")


def build_global(s: Settings):  # type: ignore[no-untyped-def]
    """(LeagueRegistry, GlobalParams) for v4.3-global from ``s.leagues_config_path``."""
    from .global_layer import GlobalParams, load_registry

    reg = load_registry(s.leagues_config_path)
    return reg, GlobalParams(auto_discovery=reg.auto_discovery)


def global_params_dict(s: Settings) -> dict:  # type: ignore[type-arg]
    """The ``global`` block hashed into the v4.3-global freeze (GlobalParams + registry tiers/baselines)."""
    reg, gp = build_global(s)
    return gp.to_dict(reg)


def build_rules(s: Settings, unfreeze: bool = False) -> tuple[PricingConfig, LiveModelParams, str]:
    """Pricing + model params for ``s.alert_rules`` (legacy / v3 / v4); v4 is checked against the freeze file.

    Returns ``(pricing, live_params, params_sha256)``. Raises FrozenParamsError on a v4 mismatch.
    """
    from .engine import rules_preset
    from .frozen import enforce_frozen, param_set, params_hash

    pricing_kw = dict(target_edge=s.target_edge, abort_edge=s.abort_edge, kelly_multiplier=s.kelly_multiplier,
                      min_minute=s.min_alert_minute, max_minute=s.max_alert_minute,
                      realert_cooldown_s=s.realert_cooldown_s, bankroll=s.bankroll,
                      max_match_stake_frac=s.max_match_stake_frac, family_cooldown_min=s.family_cooldown_min)
    rules = s.alert_rules.lower()
    base = load_live_params(s.engine_b_params_path)
    pricing, live = rules_preset(rules, base, **pricing_kw)
    gp = global_params_dict(s) if is_global(rules) else None
    if rules.startswith("v4"):
        h, diffs = enforce_frozen(s.engine_b_params_path, rules, pricing, live, unfreeze, global_params=gp)
        if diffs:
            log.warning("frozen-params check notes", extra={"rules": rules, "diffs": diffs, "params_sha256": h})
    else:
        h = params_hash(param_set(rules, pricing, live))
    log.info("engine params", extra={"rules": rules, "params_sha256": h})
    return pricing, live, h


def build_engine_config(s: Settings) -> EngineConfig:
    """EngineConfig from settings."""
    return EngineConfig(
        ev_threshold=s.ev_threshold, min_edge_for_floor=s.min_edge_for_floor, devig_method=s.devig_method,
        target_bookmaker=s.target_bookmaker, sharp_bookmakers=tuple(s.sharp_bookmakers),
        max_sharp_age_s=s.max_sharp_age_s, max_target_age_s=s.max_target_age_s,
        kelly_multiplier=s.kelly_multiplier, engine_b_mode=s.engine_b_mode, max_odds=s.max_odds,
        steam_window_s=s.steam_window_s, steam_mode=s.steam_mode, steam_rel_threshold=s.steam_rel_threshold,
        steam_abs_threshold_pp=s.steam_abs_threshold_pp, steam_cooldown_s=s.steam_cooldown_s,
    )


def build_risk(s: Settings) -> RiskManager:
    """RiskManager from settings (v4.3-global adds the liquidity-tier multipliers / unit caps)."""
    tiers: dict[str, object] = {}
    if is_global(s.alert_rules):
        _, gp = build_global(s)
        tiers = dict(tier_kelly_mult=gp.tier_kelly_mult, tier_max_units=gp.tier_max_units)
    return RiskManager(RiskConfig(
        bankroll=s.bankroll, unit_size=s.unit_size, currency=s.currency, kelly_multiplier=s.kelly_multiplier,
        max_stake_fraction=s.max_stake_fraction, round_small_step=s.round_small_step,
        round_large_step=s.round_large_step, round_switch_at=s.round_switch_at,
        high_liquidity_leagues=tuple(x.lower() for x in s.high_liquidity_leagues), **tiers,  # type: ignore[arg-type]
    ))


async def emit(signals: list, risk: RiskManager, db: AlertDatabase, notifier: TelegramNotifier) -> int:  # type: ignore[type-arg]
    """Size, persist and notify; returns the number sent."""
    n = 0
    for sig in risk.size_all(signals):
        await db.insert_signal(sig)
        await notifier.send_signal(sig)
        n += 1
        log.info("signal", extra={"id": sig.signal_id, "urgency": sig.urgency.value, "market": sig.describe_market(),
                                  "odds": sig.bookmaker_odds, "ev": round(sig.ev, 4), "stake": sig.stake_currency})
    return n


async def odds_quota_loop(quota: object, stop: asyncio.Event, sports: list[str], every_s: float = 900.0) -> None:
    """Layer 1: refresh Pinnacle/EU snapshots only when the budget + priority rules allow."""
    while not stop.is_set():
        for sport in sports:
            try:
                await quota.fetch_odds(sport)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                log.exception("odds quota fetch failed", extra={"sport": sport})
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every_s)


async def heartbeat_loop(monitor: object, poller: object, stop: asyncio.Event, every_s: float = 15.0) -> None:
    """Tick the HeartbeatMonitor; on a suspend/resume gap force an immediate scoreboard refresh."""
    while not stop.is_set():
        try:
            gap = monitor.tick(poller.live_snapshot())  # type: ignore[attr-defined]
            if gap is not None:
                note = getattr(poller, "note_gap", None)
                if note is not None:
                    note(gap)  # post-gap delta reset: next snapshots are fresh baselines
                poller.request_refresh()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            log.exception("heartbeat tick failed")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every_s)


async def run_free(s: Settings, stop: asyncio.Event, max_runtime_s: float | None = None, hooks: object | None = None,
                   heartbeat: object | None = None, unfreeze: bool = False) -> None:
    """$0/month pipeline: ESPN (Tier 1) -> DeltaTracker momentum -> optional Tier 2/3 enrichment
    -> Engine B model-only MIN-ODDS alerts (+ optional Layer-2 odds)."""
    from .aggregator_odds import SofaScoreOddsSource
    from .api_football_free import ApiFootballConfig, ApiFootballFree, interest_flags
    from .espn import EspnEndpoints
    from .football_data import load_ratings
    from .fotmob_page import FotmobPageConfig, FotmobPageEnricher, fotmob_dominance, xg_confirm
    from .free_stats_poller import FreePollerConfig, FreeSourceEndpoints, FreeStatsPoller
    from .odds_quota import OddsApiQuotaManager, QuotaConfig
    from .polite_http import PoliteFetcher, ResponseCache

    matcher = EventMatcher()
    quota = None
    if s.enable_the_odds_api and s.the_odds_api_key:
        quota = OddsApiQuotaManager(
            s.the_odds_api_key, s.odds_cache_path,
            QuotaConfig(monthly_budget=s.odds_monthly_budget, reserve=s.odds_budget_reserve,
                        bookmakers=tuple(s.the_odds_api_eu_bookmakers), sports=tuple(s.the_odds_api_sports),
                        priority_window_h=s.odds_priority_window_h, min_refresh_s=s.odds_min_refresh_s),
            user_agent=s.user_agent,
        )
    table = load_ratings(s.football_data_ratings_path)
    holder: dict[str, FreeStatsPoller] = {}

    def prior_lookup(event_key: str):  # type: ignore[no-untyped-def]
        p = holder.get("poller")
        return p.prior_for(event_key) if (p and s.espn_use_pickcenter_prior) else None

    glob = is_global(s.alert_rules)
    registry = gparams = None
    if glob:
        from .espn import LEAGUE_NAMES, register_league_id
        from .global_layer import GlobalRatingsResolver, GlobalSignalEngine

        registry, gparams = build_global(s)
        for slug, info in registry.leagues.items():
            LEAGUE_NAMES.setdefault(slug, info.name)
            if info.espn_id and info.supported:
                register_league_id(info.espn_id, slug)  # verified ids: no learn request needed

        def slug_lookup(event_key: str):  # type: ignore[no-untyped-def]
            p = holder.get("poller")
            return (p.scheduled.get(event_key) or {}).get("league") if p else None

        resolver = GlobalRatingsResolver(registry, quota.sharp_prices if quota else None, table,
                                         devig_method=s.devig_method, prior_lookup=prior_lookup,
                                         slug_lookup=slug_lookup, team_table_slugs=gparams.team_table_slugs)
    else:
        resolver = RatingsResolver(quota.sharp_prices if quota else None, table, devig_method=s.devig_method,
                                   prior_lookup=prior_lookup)
    pricing, live_params, params_sha = build_rules(s, unfreeze)
    risk = build_risk(s)
    if glob:
        free = GlobalSignalEngine(registry, risk, resolver, pricing, SignalEngine(build_engine_config(s), live_params),
                                  sizer=lambda f, o: risk.stake_from_kelly(f))
        try:
            from .probation import ProbationBook

            free.probation = ProbationBook(s.probation_db_path)  # "[PROBATION: n/150]" on new-tier cards
        except Exception:  # noqa: BLE001 - presentation only
            log.exception("league probation book unavailable; cards will carry no probation tag")
    else:
        free = FreeSignalEngine(resolver, pricing, SignalEngine(build_engine_config(s), live_params),
                                sizer=lambda f, o: risk.stake_from_kelly(f))
    free.auto_commit = False  # dedup records an alert only after the FotMob confirmation step
    fetcher = PoliteFetcher(
        s.user_agent, ResponseCache(s.http_cache_path), min_interval_s=s.polite_min_interval_s,
        global_concurrency=s.polite_global_concurrency, global_min_gap_s=s.polite_global_gap_s,
        jitter_s=s.polite_jitter_s, backoff_base_s=s.polite_backoff_base_s, backoff_max_s=s.polite_backoff_max_s,
        backend=s.http_backend,
    )
    odds_src = SofaScoreOddsSource(fetcher, s.sofascore_base, s.sofascore_odds_provider_id, s.sofascore_odds_label,
                                   enabled=s.enable_sofascore_odds)

    def ratings_of(st: object):  # type: ignore[no-untyped-def]
        return resolver.resolve(st.event_key, st.home, st.away, st.league, st.commence_time)[0]  # type: ignore[attr-defined]

    enrichers = []
    apif = None
    if s.enable_api_football_free and s.api_football_key:
        apif = ApiFootballFree(s.api_football_key, s.api_football_db_path,
                               ApiFootballConfig(daily_limit=s.api_football_daily_limit, use_limit=s.api_football_use_limit),
                               user_agent=s.user_agent)
        enrichers.append(lambda st: apif.enrich(st, ratings_of(st)))
    fm_page = None
    if s.enable_fotmob_page:
        # Tier 3 is a *confirmation* step only (see confirm_with_fotmob), not a per-state enricher.
        fm_page = FotmobPageEnricher(fetcher, FotmobPageConfig(min_interval_s=s.fotmob_page_interval_s))

    async def confirm_with_fotmob(sigs: list, st: object) -> list:  # type: ignore[type-arg]
        """Gate: minute 50-85 and EV at the neutral reference >= fotmob_ev_gate -> FotMob xG check."""
        if not sigs:
            return sigs
        lo, hi = s.fotmob_gate_minutes
        out, enriched = [], False
        for sig in sigs:
            x = sig.extra or {}
            if not x.get("model_only"):
                out.append(sig)
                continue
            evr = x.get("ev_at_reference")
            if evr is not None and evr < 0 and s.drop_negative_reference_ev:
                log.info("alert dropped: live activity argues against the line",
                         extra={"event": sig.event_key, "line": sig.line, "ev_at_reference": evr})
                continue
            if x.get("fotmob_required") == "dominance":
                # v3 rule 4: corner Over after 75' needs xG + shots-in-box dominance; no data -> suppress.
                if fm_page is not None and not enriched:
                    await fm_page.enrich(st)  # type: ignore[arg-type]
                    enriched = True
                fm_ok = fm_page is not None and "fotmob_page" in getattr(st, "enrichments", ())
                hx, ax = getattr(st, "home_xg", None), getattr(st, "away_xg", None)
                hs_, as_ = getattr(st, "home_shots_inside_box", None), getattr(st, "away_shots_inside_box", None)
                dom = fotmob_dominance(x.get("dominant_side", "home"), hx, ax, hs_, as_) if fm_ok else "unavailable"
                x["fotmob"] = f"dominance_{dom}"
                x["fotmob_xg"], x["fotmob_sib"] = (hx, ax), (hs_, as_)
                log.info("fotmob confirmation", extra={"event": sig.event_key, "line": sig.line, "verdict": x["fotmob"],
                                                       "xg": x["fotmob_xg"], "sib": x["fotmob_sib"]})
                if dom != "confirmed":
                    continue
                out.append(sig)
                continue
            if fm_page is None or not (lo <= (sig.minute or 0) <= hi) or evr is None or evr < s.fotmob_ev_gate:
                x["fotmob"] = "not_checked"
                out.append(sig)
                continue
            if not enriched:
                await fm_page.enrich(st)  # type: ignore[arg-type]  # cached per match for 5 min
                enriched = True
            fm_ok = "fotmob_page" in getattr(st, "enrichments", ())
            verdict = xg_confirm(sig.selection.value, sig.minute or 0.0, getattr(st, "home_xg", None),
                                 getattr(st, "away_xg", None), getattr(st, "home_shots_inside_box", None),
                                 getattr(st, "away_shots_inside_box", None), x.get("lambda_pre_total")) if fm_ok else "unavailable"
            x["fotmob"] = verdict
            x["fotmob_xg"] = (getattr(st, "home_xg", None), getattr(st, "away_xg", None))
            x["fotmob_sib"] = (getattr(st, "home_shots_inside_box", None), getattr(st, "away_shots_inside_box", None))
            log.info("fotmob confirmation", extra={"event": sig.event_key, "line": sig.line, "verdict": verdict,
                                                   "xg": x["fotmob_xg"], "sib": x["fotmob_sib"]})
            if verdict == "contradicted" and s.fotmob_drop_contradicted:
                continue
            out.append(sig)
        return out

    async with AlertDatabase(s.db_path) as db, \
            TelegramNotifier(s.telegram_bot_token, s.telegram_chat_id, dry_run=s.dry_run) as notifier:

        async def on_state(st: object) -> None:
            sigs = free.on_match_state(st)  # type: ignore[arg-type]
            sigs = await confirm_with_fotmob(sigs, st)
            for sg in sigs:
                free.commit(sg)
            log.info("live state", extra={
                "event": st.event_key, "src": st.source, "minute": round(st.minute, 1), "score": st.score,  # type: ignore[attr-defined]
                "corners": (st.home_corners, st.away_corners), "sot": (st.home_shots_on_target, st.away_shots_on_target),  # type: ignore[attr-defined]
                "momentum": st.attack_momentum, "momentum_src": st.momentum_source,  # type: ignore[attr-defined]
                "mult": (st.momentum_mult_home, st.momentum_mult_away, st.momentum_mult_total),  # type: ignore[attr-defined]
                "activity": st.activity_10m, "status": st.status,  # type: ignore[attr-defined]
                "ratings_src": resolver._cache.get(st.event_key, (None, "?"))[1], "alerts": len(sigs),  # type: ignore[attr-defined]
                **({"slug": getattr(st, "league_slug", ""), "tier": registry.tier_of(getattr(st, "league_slug", ""))}
                   if registry is not None else {})})
            if odds_src.enabled and getattr(st, "source", "") == "sofascore":
                snaps = await odds_src.fetch_event_odds(st.source_event_id, st.event_key, st.home, st.away,  # type: ignore[attr-defined]
                                                        st.league, st.commence_time, True)  # type: ignore[attr-defined]
                sigs += free.on_odds(snaps)
            await emit(sigs, risk, db, notifier)
            if hooks is not None and sigs and hasattr(hooks, "on_alerts"):
                await hooks.on_alerts(sigs, st)  # type: ignore[attr-defined]

        g_kw: dict[str, object] = {}
        espn_leagues = list(s.espn_leagues)
        if glob:
            espn_leagues = registry.supported_slugs() + [x for x in s.espn_leagues if x not in registry.leagues]  # type: ignore[union-attr]
            g_kw = dict(espn_all_limit=gparams.espn_all_limit, auto_discover=gparams.auto_discovery,  # type: ignore[union-attr]
                        telemetry_gate_minute=gparams.telemetry_gate_minute, fallback_listed_only=True,  # type: ignore[union-attr]
                        espn_summaries_per_cycle=gparams.summaries_per_cycle)  # type: ignore[union-attr]
        poller = FreeStatsPoller(
            fetcher, on_state, matcher, FreeSourceEndpoints(sofascore_base=s.sofascore_base, fotmob_base=s.fotmob_base),
            FreePollerConfig(live_list_interval_s=s.list_poll_s, stats_interval_s=max(s.stats_poll_s, s.polite_min_interval_s),
                             max_events=(gparams.max_live_events if glob else s.free_max_live_events),  # type: ignore[union-attr]
                             league_filter=tuple(s.free_league_filter),
                             use_espn=s.enable_espn, use_sofascore=s.enable_sofascore, use_fotmob=s.enable_fotmob,
                             espn_leagues=tuple(espn_leagues),
                             espn_scoreboard_interval_s=max(s.espn_scoreboard_poll_s, s.polite_min_interval_s),
                             espn_idle_interval_s=s.espn_idle_poll_s,
                             espn_summary_interval_s=max(s.espn_summary_poll_s, s.polite_min_interval_s),
                             use_espn_summary=s.espn_use_summary, synthetic_momentum=s.synthetic_momentum,
                             espn_use_all=s.espn_use_all, espn_all_interval_s=max(s.espn_all_poll_s, s.polite_min_interval_s),
                             espn_summary_once=s.espn_summary_once,
                             summary_db_path=str(s.espn_summary_db_path) if s.espn_summary_db_path else None,
                             **g_kw),  # type: ignore[arg-type]
            espn_endpoints=EspnEndpoints(base=s.espn_base), ratings_hint=ratings_of, enrichers=enrichers,
        )
        if glob:
            poller.league_priority = registry.priority  # type: ignore[union-attr]
            poller.on_league_meta = registry.note_discovered  # type: ignore[union-attr]
        holder["poller"] = poller
        tasks = [asyncio.create_task(poller.run(stop), name="free-stats")]
        if heartbeat is not None:
            g = heartbeat.start()  # type: ignore[attr-defined]
            if g is not None:
                log.warning("restart gap since previous heartbeat", extra={"duration_s": round(g.duration_s)})
            tasks.append(asyncio.create_task(heartbeat_loop(heartbeat, poller, stop), name="heartbeat"))
        if hooks is not None and hasattr(hooks, "tick"):
            tasks.append(asyncio.create_task(hooks.tick(poller, fetcher, stop), name="hooks"))  # type: ignore[attr-defined]
        if quota:
            tasks.append(asyncio.create_task(odds_quota_loop(quota, stop, list(s.the_odds_api_sports)), name="odds-quota"))
        log.info("quantbet started (free mode)", extra={
            "espn": s.enable_espn, "espn_leagues": espn_leagues, "api_football": bool(apif),
            "rules": s.alert_rules, "params_sha256": params_sha,
            "auto_discovery": bool(g_kw.get("auto_discover")), "telemetry_gate_minute": g_kw.get("telemetry_gate_minute"),
            "fotmob_page": s.enable_fotmob_page, "sofascore": s.enable_sofascore, "fotmob_api": s.enable_fotmob,
            "the_odds_api": bool(quota), "sofascore_odds": s.enable_sofascore_odds, "team_ratings": table is not None,
            "dry_run": notifier.dry_run, "user_agent": s.user_agent})
        if max_runtime_s:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=max_runtime_s)
            stop.set()
        else:
            await stop.wait()
        log.info("shutting down", extra={"http_requests": fetcher.n_requests, "refusals": fetcher.refusals})
        if hooks is not None and hasattr(hooks, "final"):
            with contextlib.suppress(Exception):
                await hooks.final(poller, fetcher)  # type: ignore[attr-defined]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await fetcher.close()
        if quota:
            await quota.close()
        if apif:
            await apif.close()
        rep = await db.report(s.bankroll)
        log.info("session report", extra=dataclasses.asdict(rep))


async def run(settings: Settings | None = None) -> None:
    """Start the full pipeline and block until a shutdown signal."""
    s = settings or get_settings()
    setup_logging(s.log_level, s.log_json)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    if s.mode == "free":
        await run_free(s, stop)
        return

    engine = SignalEngine(
        EngineConfig(
            ev_threshold=s.ev_threshold, min_edge_for_floor=s.min_edge_for_floor, devig_method=s.devig_method,
            target_bookmaker=s.target_bookmaker, sharp_bookmakers=tuple(s.sharp_bookmakers),
            max_sharp_age_s=s.max_sharp_age_s, max_target_age_s=s.max_target_age_s,
            kelly_multiplier=s.kelly_multiplier, engine_b_mode=s.engine_b_mode, max_odds=s.max_odds,
            steam_window_s=s.steam_window_s, steam_mode=s.steam_mode, steam_rel_threshold=s.steam_rel_threshold,
            steam_abs_threshold_pp=s.steam_abs_threshold_pp, steam_cooldown_s=s.steam_cooldown_s,
        )
    )
    risk = RiskManager(RiskConfig(
        bankroll=s.bankroll, unit_size=s.unit_size, currency=s.currency, kelly_multiplier=s.kelly_multiplier,
        max_stake_fraction=s.max_stake_fraction, round_small_step=s.round_small_step,
        round_large_step=s.round_large_step, round_switch_at=s.round_switch_at,
        high_liquidity_leagues=tuple(x.lower() for x in s.high_liquidity_leagues),
    ))
    matcher = EventMatcher()
    queue: asyncio.Queue[PollerMessage] = asyncio.Queue(maxsize=1000)

    async with aiohttp.ClientSession(headers={"User-Agent": "quantbet-signal-engine/0.1"}) as session, \
            AlertDatabase(s.db_path) as db, \
            TelegramNotifier(s.telegram_bot_token, s.telegram_chat_id, dry_run=s.dry_run) as notifier:
        adapters, odds_io = build_adapters(s, session, matcher)
        if not adapters:
            log.error("no providers configured: set API keys in .env (see .env.example)")
            return
        tasks = [
            asyncio.create_task(Poller(adapters, matcher, queue, stop).run(), name="poller"),
            asyncio.create_task(consume(queue, engine, risk, db, notifier, stop), name="engine"),
            asyncio.create_task(closing_line_job(engine, db, stop), name="clv"),
        ]
        if s.enable_odds_api_io_ws and odds_io and s.odds_api_io_key:
            ws = OddsApiIoWebSocket(s.odds_api_io_key, s.odds_api_io_markets, odds_io, queue, matcher, stop)
            tasks.append(asyncio.create_task(ws.run(), name="ws"))
        log.info("quantbet started", extra={"providers": [a.name for a in adapters]})
        await stop.wait()
        log.info("shutting down")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        rep = await db.report(s.bankroll)
        log.info("session report", extra=dataclasses.asdict(rep))


def parse_args(argv: list[str] | None = None) -> "argparse.Namespace":
    import argparse

    ap = argparse.ArgumentParser(prog="quantbet", description="Read-only quantitative signal & notification engine.")
    ap.add_argument("--live", action="store_true",
                    help="free mode, Telegram ENABLED (needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env)")
    ap.add_argument("--dry-run", action="store_true", help="log notifications instead of sending them")
    ap.add_argument("--rules", choices=("legacy", "v3", "v4", "v4.1", "v4.2", "v4.3-global"), default=None,
                    help="alert rules (default QB_ALERT_RULES; v4.1 with --live)")
    ap.add_argument("--unfreeze", action="store_true", help="allow v4 params that differ from config/engine_b_params.json")
    ap.add_argument("--leagues", default=None, help="comma-separated ESPN slugs (default QB_ESPN_LEAGUES)")
    ap.add_argument("--cups", action="store_true", help="add UCL/UEL/UECL and domestic cups to the league list")
    ap.add_argument("--heartbeat-db", default="data/heartbeat.sqlite3")
    ap.add_argument("--no-heartbeat", action="store_true")
    ap.add_argument("--no-keepalive", action="store_true")
    return ap.parse_args(argv)


def settings_from_args(a: "argparse.Namespace", base: Settings | None = None) -> Settings:
    from .espn import CUP_LEAGUES

    s = base or get_settings()
    upd: dict[str, object] = {}
    if a.live:
        if not (s.telegram_bot_token and s.telegram_chat_id):
            raise SystemExit("--live needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (set them in .env)")
        upd.update(mode="free", dry_run=False)
    if a.dry_run:
        upd["dry_run"] = True
    if a.rules:
        upd["alert_rules"] = a.rules
    elif a.live:
        upd["alert_rules"] = "v4.2"
    leagues = [x.strip() for x in a.leagues.split(",") if x.strip()] if a.leagues else list(s.espn_leagues)
    if a.cups:
        leagues += [x for x in CUP_LEAGUES if x not in leagues]
    upd["espn_leagues"] = leagues
    return s.model_copy(update=upd)


async def run_cli(s: Settings, a: "argparse.Namespace") -> None:
    from .heartbeat import HeartbeatMonitor, start_keepalive

    setup_logging(s.log_level, s.log_json)
    ka, handle = ("disabled", None) if a.no_keepalive else start_keepalive()
    log.info("keep-alive", extra={"keepalive": ka})
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        if s.mode != "free":
            await run(s)
            return
        hb = None
        if not a.no_heartbeat:
            from pathlib import Path

            Path(a.heartbeat_db).parent.mkdir(parents=True, exist_ok=True)
            hb = HeartbeatMonitor(a.heartbeat_db, run_id=f"live-{s.alert_rules}")
        log.info("live run", extra={"rules": s.alert_rules, "telegram": not s.dry_run, "leagues": s.espn_leagues})
        await run_free(s, stop, heartbeat=hb, unfreeze=a.unfreeze)
    finally:
        if handle is not None:
            with contextlib.suppress(Exception):
                handle.terminate()


def main(argv: list[str] | None = None) -> None:
    """CLI entry point (``quantbet --live --rules v4 --cups``)."""
    from .frozen import FrozenParamsError

    a = parse_args(argv)
    s = settings_from_args(a)
    try:
        asyncio.run(run_cli(s, a))
    except FrozenParamsError as exc:
        raise SystemExit(f"refusing to start: {exc}") from None


if __name__ == "__main__":
    main()
