"""Telegram notifier (Bot API ``sendMessage`` over aiohttp)."""

from __future__ import annotations

import asyncio
import html
import logging

import aiohttp

from .models import Signal, Urgency

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "AUD": "A$", "CAD": "C$"}


def money(amount: float, currency: str) -> str:
    """``15, "USD"`` -> ``"$15"``; unknown codes -> ``"15 SEK"``."""
    sym = CURRENCY_SYMBOLS.get(currency.upper())
    v = f"{amount:,.0f}" if float(amount).is_integer() else f"{amount:,.2f}"
    return f"{sym}{v}" if sym else f"{v} {currency}"


def format_tier_line(tiers: list[tuple[float, float]] | list[list[float]], currency: str, max_items: int = 6) -> str:
    """``If Bet365 offers 2.05 -> Bet $15 | If 2.15 -> Bet $25``."""
    parts = []
    for i, (odds, stake) in enumerate(list(tiers)[:max_items]):
        head = "If Bet365 offers" if i == 0 else "If"
        parts.append(f"{head} {odds:.2f} -> Bet {money(stake, currency)}")
    return " | ".join(parts)


def settlement_note(sp: list[float] | tuple[float, ...]) -> str:
    """Human note for Asian outcomes from ``[win, half_win, push, half_loss, loss]``."""
    try:
        _, hw, push, hl, _ = (float(v) for v in sp)
    except (TypeError, ValueError):
        return ""
    if push > 1e-4:
        return f"whole line: stake refunded {push:.0%}"
    if hw > 1e-4:
        return f"quarter line: half-win {hw:.0%}"
    if hl > 1e-4:
        return f"quarter line: half-loss {hl:.0%}"
    return "half line"


def _fmt_line(line: float) -> str:
    """``10.0`` / ``10.25`` / ``10.5`` / ``10.75`` (same as quantbet.syndicate.fmt_line)."""
    t = f"{float(line):.2f}"
    if t.endswith("00"):
        return t[:-1]
    return t[:-1] if t.endswith("0") else t


def asian_alternatives_lines(alts: list[dict], currency: str) -> list[str]:  # type: ignore[type-arg]
    """Lines patch: the compact ``[ASIAN ALTERNATIVES]`` block shown under the stake ladder.

    ``🔀 [ASIAN ALTERNATIVES] (if Asian tab is open): Over 10.25 ≥ 1.88 (€25) | Over 10.0 ≥ 1.76 (€30) | ...``
    plus one short line saying they replace the main bet and how quarter / whole lines settle.
    Empty list when there are no alternatives.
    """
    if not alts:
        return []
    parts = [f"{str(a['selection']).capitalize()} {_fmt_line(a['line'])} ≥ {float(a['min_odds']):.2f} "
             f"({money(float(a['first_tier_stake']), currency)})" for a in alts]
    notes = "; ".join(f"{_fmt_line(a['line'])}: {a['settlement_note']}" for a in alts if a.get("settlement_note"))
    return ["🔀 [ASIAN ALTERNATIVES] (if Asian tab is open): " + html.escape(" | ".join(parts)),
            "↳ bet ONE of these <b>instead of</b> the .5 line above, never in addition (same family, same match cap)"
            + (f" | {html.escape(notes)}" if notes else "")]


def _tier_line(lines: list[str], s: Signal) -> None:
    """v4.3-global: ``[LIQUIDITY: TIER 3 - SMALL LEAGUE]`` (and tier 1/2) under the header, then
    ``[PROBATION: n/150]`` when ``extra["probation"]`` is set; no-op otherwise."""
    tier = (s.extra or {}).get("liquidity_tier")
    if tier:
        from .global_layer import liquidity_tag

        lines.append(f"<b>{html.escape(liquidity_tag(str(tier)))}</b>")
    prob = (s.extra or {}).get("probation")
    if prob:  # league probation (quantbet.probation): newly added tier without enough OOS evidence yet
        lines.append(f"<b>{html.escape(str(prob))}</b>")


def _syndicate_lines(lines: list[str], s: Signal, x: dict, min_odds: float) -> None:  # type: ignore[type-arg]
    """v4.4-syndicate / v4.5-sharp blocks (hurdle, momentum, cards model, BOOST TARGET, BET BUILDER).
    No-op for older rules."""
    e = html.escape
    sy = x.get("syndicate")
    if sy and sy.get("achievability_gate") is False:  # v4.5-sharp
        cap = f" | lift cap {sy['lift_cap'] * 100:.0f}%" if sy.get("lift_cap") is not None else ""
        lines.append(f"🎚️ Tier hurdle: EV ≥ {sy['hurdle'] * 100:.1f}% at the minimum | abort floor at "
                     f"{sy['abort_edge'] * 100:.1f}% EV{cap} | achievability gate off")
    elif sy:
        lines.append(f"🎚️ Tier hurdle: EV ≥ {sy['hurdle'] * 100:.1f}% at the minimum | abort floor at "
                     f"{sy['abort_edge'] * 100:.1f}% EV | lift needed x{sy['lift_required']:.3f} "
                     f"(assumed Bet365 margin {sy['assumed_margin'] * 100:.2f}%/side)")
    mo = x.get("momentum")
    if mo:
        w = f" [{', '.join(mo.get('weights') or [])}]" if mo.get("weights") else ""
        lines.append(f"🌊 Momentum {mo['span']:g}' window: index {mo['index']:.2f} -> {mo['adj_index']:.2f}{e(w)} "
                     f"over {mo['polls']} polls ({e('/'.join(mo.get('inputs') or []))})")
    cm = x.get("cards_model")
    if cm:
        if cm.get("rule") == "bet365":
            rule = ("Bet365 Total Cards: yellow=1, red=2, 2nd-yellow red=3 - NOT the 10/25 booking-points market")
        else:
            rule = "yellow=1, red=1"
        dist = f"NegBin r={cm['nb_r']:g}" if cm.get("nb_r") else "Poisson"
        lines.append(f"🟨 Cards model ({dist}): λ remaining {cm['lam_remaining']:.2f} vs prior {cm['lam_prior']:.2f} "
                     f"(net x{cm['net']:.2f}; fouls x{cm['foul_factor']:.2f}, bookings x{cm['booking_factor']:.2f}, "
                     f"urgency x{cm['urgency']:.2f}) | {cm['current']} cards so far ({rule})")
        if cm.get("cards90") is not None:
            lines.append(f"📚 Cards baseline: {cm['cards90']:.2f} cards / {cm['fouls90']:.1f} fouls per 90 "
                         f"({e(str(cm.get('baseline_source', '')))})")
    bt = None if x.get("paper_only") else x.get("boost_target")
    if bt:
        txt = f"🚀 [BOOST TARGET] Bet only if the (boosted) Bet365 price is ≥ <b>{min_odds:.2f}</b>"
        if bt.get("typical_unboosted"):
            need = bt.get("boost_needed_pct") or 0.0
            txt += (f". Typical unboosted ≈ {bt['typical_unboosted']:.2f} (ASSUMED: prior fair less margin) -> "
                    + ("no boost needed" if need <= 0 else f"needs a ≥{need * 100:.0f}% winnings boost"))
        if bt.get("pre_boost_odds"):
            txt += (f". With a {bt['boost_pct'] * 100:.0f}% winnings boost a pre-boost price ≥ {bt['pre_boost_odds']:.2f} "
                    "qualifies (only if Bet365 actually offers that boost on this market)")
        lines.append(txt)
    combos = x.get("combos") or []
    if combos:
        lines.append("🧩 <b>[BET BUILDER / 3-WAY COMBO]</b> (informational, same match; rho = Football-Data PRIORS)")
        for c in combos:
            legs = " + ".join(str(g["label"]) for g in c["legs"])
            row = (f"• {e(legs)}: fair {c['fair_odds']:.2f} | naive independent {c['naive_odds']:.2f} | "
                   f"<b>Minimum Bet365 Combo Odds {c['min_odds']:.2f}</b>")
            if c.get("boost_required_odds"):
                row += f" | {c['boost_pct'] * 100:.0f}% boost: pre-boost ≥ {c['boost_required_odds']:.2f}"
            row += f" | max stake {c.get('max_stake', 0):g} {e(s.currency)} (inside the 5% match cap) [{e(c['method'])}]"
            lines.append(row)


def format_min_odds_signal(s: Signal) -> str:
    """Model-only (no live Bet365 price available) alert: the user checks the app.

    Shows fair odds, the MINIMUM Bet365 odds that give the target edge, the
    abort floor and a stake ladder.
    """
    e = html.escape
    x = s.extra or {}
    min_odds = float(x.get("min_odds", s.bookmaker_odds))
    floor = float(x.get("abort_floor", s.min_acceptable_odds))
    paper = bool(x.get("paper_only"))
    lines = [f"<b>[{e(s.urgency.value)}]</b>" + ("  ⚠️ <b>High Limit Risk</b>" if s.high_limit_risk else "")]
    if paper:  # v4.5-sharp cards: logged + graded, never staked, outside the match caps
        lines.append("📝 <b>[PAPER ONLY - UNCALIBRATED]</b> logged for grading - do NOT bet")
    _tier_line(lines, s)
    match = f"{e(s.home)} vs {e(s.away)}"
    if s.minute is not None:
        match += f"  |  {int(s.minute)}'  {e(s.score or '')}"
    lines.append(f"⚽ {match}")
    lines.append(f"🏆 {e(s.league)}")
    label = s.describe_market()
    if (x.get("cards_model") or {}).get("rule") == "bet365" and label.endswith(" Cards"):
        label = label[: -len(" Cards")] + " Total Cards"  # v4.5: Bet365 "Total Cards" (Y=1, R=2)
    if x.get("bet365_market"):  # lines patch: the always-open Bet365 .5 market ("Match Goals" / "2-Way Corners")
        label += f" - Bet365 {x['bet365_market']}"
    lines.append(f"🎯 <b>{e(label)}</b>" + (f"  ({e(settlement_note(x['settlement']))})" if x.get("settlement") else ""))
    lines.append(f"⚖️ Fair Odds: <b>{s.fair_odds:.2f}</b>  (p={s.p_true:.3f})")
    if paper:
        lines.append(f"📝 Paper minimum odds (graded, not a bet): <b>{min_odds:.2f}</b> | paper abort floor {floor:.2f}")
        lines.append("🧮 Stake: none (paper only; does not use the 3-alert / 5% match cap)")
    else:
        lines.append(f"✅ MINIMUM BET365 ODDS TO BET: <b>{min_odds:.2f}</b>")
        lines.append(f"🛑 Abort floor: <b>{floor:.2f}</b> (do not bet below)")
        tiers = x.get("tiers") or []
        if tiers:
            lines.append("🧮 " + e(format_tier_line(tiers, s.currency)))
        lines.extend(asian_alternatives_lines(x.get("asian_alternatives") or [], s.currency))
    lift = x.get("lift")
    if lift and lift.get("prior_fair"):
        lines.append(f"📈 Prior fair {lift['prior_fair']:.2f} -> model {s.fair_odds:.2f} "
                     f"(lift {lift['lift'] * 100:+.1f}%, net λ x{lift['net_multiplier']:.2f})")
    trig = x.get("triggers")
    if trig:
        lines.append(f"📡 Triggers: {e(', '.join(map(str, trig)))}")
    src = [str(v) for v in (x.get("ratings_source"), x.get("source")) if v]
    if src:
        lines.append(f"📊 Ratings: {e(src[0])}" + (f" | Stats: {e(src[1])}" if len(src) > 1 else ""))
    _syndicate_lines(lines, s, x, min_odds)
    if paper:
        lines.append("ℹ️ Paper only: graded offline (Bet365 Total Cards rules) until 50 live games are calibrated.")
    else:
        lines.append("ℹ️ Model-only: no live Bet365 price was read. Check the app; bet only at or above the minimum.")
    for note in s.risk_notes:
        lines.append(f"• {e(note)}")
    # plumbing: the id the user passes to scripts/record_fill.py (or writes into data/fills.csv)
    lines.append(f"ID: <code>{e(s.signal_id)}</code>")
    return "\n".join(lines)


def format_signal(s: Signal) -> str:
    """Render a signal as compact Telegram HTML for fast manual execution."""
    if s.urgency is Urgency.MIN_ODDS:
        return format_min_odds_signal(s)
    e = html.escape
    lines = [f"<b>[{e(s.urgency.value)}]</b>" + ("  ⚠️ <b>High Limit Risk</b>" if s.high_limit_risk else "")]
    _tier_line(lines, s)
    match = f"{e(s.home)} vs {e(s.away)}"
    if s.is_live and s.minute is not None:
        match += f"  |  {int(s.minute)}'  {e(s.score or '')}"
    lines.append(f"⚽ {match}")
    lines.append(f"🏆 {e(s.league)}")
    lines.append(f"🎯 <b>{e(s.describe_market())}</b>")
    lines.append(f"💰 Bet365: <b>{s.bookmaker_odds:.2f}</b>  |  Fair: {s.fair_odds:.3f}")
    lines.append(f"🛑 Min odds: <b>{s.min_acceptable_odds:.2f}</b> (abort below)")
    lines.append(f"📈 Edge: <b>+{s.ev * 100:.1f}%</b>  (p={s.p_true:.3f}, {e(s.method)})")
    lines.append(
        f"🧮 Stake: <b>{s.stake_units:g}u = {s.stake_currency:,.0f} {e(s.currency)}</b>"
        f"  (¼-Kelly {s.kelly_fraction * 100:.2f}%)"
    )
    if s.sharp_source:
        lines.append(f"📊 Sharp ref: {e(s.sharp_source)}")
    for note in s.risk_notes:
        lines.append(f"• {e(note)}")
    lines.append(f"ID: <code>{e(s.signal_id)}</code>")
    return "\n".join(lines)


class TelegramNotifier:
    """Sends messages to one chat. Handles 429 ``retry_after`` and transient errors."""

    def __init__(
        self,
        token: str | None,
        chat_id: str | None,
        session: aiohttp.ClientSession | None = None,
        dry_run: bool = False,
        max_retries: int = 3,
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        self._session = session
        self._own_session = session is None
        self.dry_run = dry_run or not (token and chat_id)
        self.max_retries = max_retries

    async def __aenter__(self) -> TelegramNotifier:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the HTTP session if we own it."""
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def send_text(self, text: str) -> bool:
        """Send an HTML message; returns True on success (or in dry-run)."""
        if self.dry_run:
            log.info("notification (dry-run)", extra={"text": text})
            return True
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
        url = TELEGRAM_API.format(token=self.token)
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        for attempt in range(self.max_retries + 1):
            try:
                async with self._session.post(url, json=payload) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status == 200 and data.get("ok"):
                        return True
                    if resp.status == 429:
                        wait = float(data.get("parameters", {}).get("retry_after", 1))
                        await asyncio.sleep(wait)
                        continue
                    log.error("telegram error", extra={"status": resp.status, "body": data})
                    if 400 <= resp.status < 500:
                        return False
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                log.warning("telegram transient error", extra={"error": str(exc), "attempt": attempt})
            await asyncio.sleep(min(2**attempt * 0.5, 5))
        return False

    async def send_signal(self, signal: Signal) -> bool:
        """Format and send a signal."""
        return await self.send_text(format_signal(signal))
