"""v4.5-sharp lines patch: only Bet365-available lines alert.

1. ``[PAPER ONLY]`` signals are stored but not sent to Telegram unless ``QB_SEND_PAPER_ALERTS=true``.
2. Total Cards: TIER_1 majors only (``SharpParams.cards_leagues``), nets 1.18 / 0.85, a single ``.5`` line
   with 1.80 <= fair <= 2.10.
3. Goals / Corners primaries are always ``.5`` lines (1.55 <= fair <= 2.50, closest to 1.95); up to three
   ``.0 / .25 / .75`` neighbours are attached as ``[ASIAN ALTERNATIVES]`` (substitutes, never extra bets).
4. The re-freeze: a new v4.5-sharp hash, the v4.2 / v4.3 / v4.4 hashes unchanged.
Offline, no network, no Telegram.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from pathlib import Path

import pytest

from quantbet.engine import PreMatchRatings, SignalEngine, rules_preset
from quantbet.frozen import read_frozen_set
from quantbet.global_layer import GlobalRatingsResolver, LeagueInfo, LeagueRegistry
from quantbet.models import LiveMatchState, MarketType, Selection, Signal, Urgency
from quantbet.notifier import asian_alternatives_lines, format_min_odds_signal
from quantbet.risk import RiskConfig, RiskManager
from quantbet.syndicate import (BET365_HALF_LINE_MARKET, SharpParams, SyndicateParams, SyndicateSignalEngine,
                                asian_settlement_note, combo_leg_selection, fmt_line, is_half_line, load_priors)

ROOT = Path(__file__).resolve().parents[1]
V42_HASH = "40e806b910db93de34f9b26d9ebf361168077f1f38a8fe8d7dbc4e7cb6729aec"
V43_HASH = "971ab88662be2f23c0dd987484c7dcac1f36ed479bc4e650f99e60b0e1e9f77a"
V44_HASH = "c3a6d97a385bd07e614e4040b4df2c6aff437d445061fec35bbe30f10dcb3ced"
V45_HASH = "3e1ccaa0a84e1b067884bd84fb3e6d0faa0e840cbea65d3d7c6ab7d160ee5368"  # lines patch re-freeze
V45_PREV_HASH = "20e12b5951ed5e8dafa2076a243d5721aca24a221e56caf3266cecb846eb6fdb"  # final patch / plumbing
SH = SharpParams()
PRIORS = load_priors(ROOT / "config/syndicate_priors.json")
R = PreMatchRatings(0.6, 2.9, 1.0, 10.0, 0.55, 0.2)
TIER_KW = dict(tier_kelly_mult=(("TIER_1", 1.0), ("TIER_2", 1.0), ("TIER_3", 0.5)), tier_max_units=(("TIER_3", 15.0),))
# a synthetic match that produces Goals / Corners Overs once the activity / flurry gates are relaxed
RELAXED = dataclasses.replace(SH, require_activity_confirm=False, max_momentum_index=99.0, sustained_over_index=1.0)


class _Ratings:
    def ratings(self, h, a, league=None):  # noqa: ANN001, ANN201
        return R


def _engine(params=None, rules="v4.5-sharp") -> SyndicateSignalEngine:  # noqa: ANN001
    reg = LeagueRegistry({"eng.1": LeagueInfo("eng.1", "Premier League", tier="TIER_1"),
                          "usa.1": LeagueInfo("usa.1", "MLS", tier="TIER_1"),
                          "eng.2": LeagueInfo("eng.2", "Championship", tier="TIER_2")})
    pricing, live = rules_preset(rules, None, bankroll=2000.0)
    rm = RiskManager(RiskConfig(bankroll=2000, unit_size=10, **TIER_KW))
    res = GlobalRatingsResolver(reg, None, _Ratings(), team_table_slugs=("eng.1", "usa.1", "eng.2"))
    return SyndicateSignalEngine(reg, rm, res, pricing, SignalEngine(live_params=live),
                                 sizer=lambda f, o: rm.stake_from_kelly(f), params=params or SharpParams(),
                                 priors=PRIORS)


def _state(key, m, rate=1.0, mh=1.0, slug="eng.1", league="Premier League", t=0.0, cards=False):  # noqa: ANN001, ANN202
    s = LiveMatchState(event_key=key, minute=m, home_goals=0, away_goals=0, home_corners=int(m * 0.11 * rate),
                       away_corners=int(m * 0.09 * rate), home_red_cards=0, away_red_cards=0,
                       period=2 if m > 45 else 1, updated_at=t, home="Home FC", away="Away FC", league=league,
                       status="2nd half" if m > 45 else "1st half", stats_available=True, attack_momentum=0.5,
                       home_shots=int(m * 0.28 * rate), away_shots=int(m * 0.22 * rate),
                       home_shots_on_target=int(m * 0.10 * rate), away_shots_on_target=int(m * 0.08 * rate),
                       league_slug=slug, home_fouls=int(m * 0.42) if cards else None,
                       away_fouls=int(m * 0.40) if cards else None, home_yellow_cards=2 if cards else None,
                       away_yellow_cards=2 if cards else None)
    s.momentum_mult_home = s.momentum_mult_away = s.momentum_mult_total = mh
    return s


def _drive(eng, key, minutes, **kw):  # noqa: ANN001, ANN202
    out = []
    for i, m in enumerate(minutes):
        s = _state(key, float(m), t=1_000_000.0 + i * 60, **kw)
        out += eng.on_match_state(s, s.updated_at)
    return out


def _signal(**extra) -> Signal:  # noqa: ANN003
    x = {"min_odds": 1.95, "abort_floor": 1.90, "tiers": [[1.95, 20.0], [2.05, 30.0]],
         "syndicate": {"tier": "TIER_1", "hurdle": 0.035, "abort_edge": 0.021, "assumed_margin": 0.04,
                       "lift_required": 1.0, "achievability_gate": False}}
    x.update(extra)
    return Signal(urgency=Urgency.LIVE_MODEL, engine="B", event_key="k", home="A", away="B", league="L",
                  market=MarketType.TOTAL_CORNERS, line=10.5, selection=Selection.OVER, bookmaker="bet365",
                  bookmaker_odds=1.95, p_true=0.53, fair_odds=1.88, min_acceptable_odds=1.95, ev=0.04,
                  kelly_fraction=0.02, is_live=True, minute=60.0, score="1-0", method="live:negbin",
                  extra=x)


# -- 1. paper alerts --------------------------------------------------------------------------------------
class _DB:
    def __init__(self) -> None:
        self.rows: list[Signal] = []

    async def insert_signal(self, sig: Signal) -> None:
        self.rows.append(sig)


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[Signal] = []

    async def send_signal(self, sig: Signal) -> None:
        self.sent.append(sig)


class _Risk:
    def size_all(self, signals):  # noqa: ANN001, ANN201
        return list(signals)


def test_paper_signal_inserted_but_not_sent_by_default():
    from quantbet.main import emit
    db, nt = _DB(), _Notifier()
    paper, real = _signal(paper_only=True), _signal()
    n = asyncio.run(emit([paper, real], _Risk(), db, nt))  # type: ignore[arg-type]
    assert n == 1 and db.rows == [paper, real] and nt.sent == [real]


def test_send_paper_alerts_true_sends_paper():
    from quantbet.main import emit, notify_signal
    db, nt = _DB(), _Notifier()
    paper = _signal(paper_only=True)
    assert asyncio.run(emit([paper], _Risk(), db, nt, send_paper=True)) == 1  # type: ignore[arg-type]
    assert db.rows == [paper] and nt.sent == [paper]
    nt2 = _Notifier()
    assert asyncio.run(notify_signal(nt2, paper)) is False and nt2.sent == []  # type: ignore[arg-type]
    assert asyncio.run(notify_signal(nt2, paper, True)) is True and nt2.sent == [paper]  # type: ignore[arg-type]


def test_send_paper_alerts_setting_env_workflow_and_example(monkeypatch):  # noqa: ANN001
    from quantbet.config import Settings
    assert Settings(_env_file=None).send_paper_alerts is False  # type: ignore[call-arg]
    monkeypatch.setenv("QB_SEND_PAPER_ALERTS", "true")
    assert Settings(_env_file=None).send_paper_alerts is True  # type: ignore[call-arg]
    wf = (ROOT / ".github/workflows/live_quantbet.yml").read_text()
    assert 'QB_SEND_PAPER_ALERTS: "false"' in wf
    assert "QB_SEND_PAPER_ALERTS=false" in (ROOT / ".env.example").read_text()


def test_every_send_path_goes_through_the_paper_gate():
    src = (ROOT / "src/quantbet/main.py").read_text()
    body = src.split("async def notify_signal", 1)[1]
    # the only direct notifier.send_signal call left in main.py is inside notify_signal
    assert src.count(".send_signal(") == 1 and body.split("\n\n\n", 1)[0].count(".send_signal(") == 1
    assert "send_paper=s.send_paper_alerts" in src or "s.send_paper_alerts)" in src


# -- 2. cards ---------------------------------------------------------------------------------------------
def test_cards_params():
    assert SH.allow_tier2_cards is False and SH.allow_tier3_cards is False
    assert SH.cards_leagues == ("eng.1", "esp.1", "ger.1", "ita.1", "fra.1", "uefa.champions", "uefa.europa")
    assert (SH.cards_net_over, SH.cards_net_under) == (1.18, 0.85)
    assert SH.cards_fair_odds_range == (1.80, 2.10) and SH.primary_fair_window("cards") == (1.80, 2.10)
    assert (SyndicateParams().cards_net_over, SyndicateParams().cards_net_under) == (1.10, 0.90)  # v4.4 kept


def test_cards_blocked_outside_tier1_list():
    for slug in SH.cards_leagues:
        assert SH.tier_allows("TIER_1", "cards", slug)
    assert not SH.tier_allows("TIER_1", "cards", "usa.1") and not SH.tier_allows("TIER_1", "cards", "bra.1")
    assert not SH.tier_allows("TIER_1", "cards", None)
    assert not SH.tier_allows("TIER_2", "cards", "eng.2") and not SH.tier_allows("TIER_3", "cards", "eng.3")
    assert SH.tier_allows("TIER_1", "goals", "usa.1") and SH.tier_allows("TIER_2", "corners", "eng.2")
    assert SyndicateParams().tier_allows("TIER_2", "cards", "eng.2")  # v4.4: every family everywhere
    on = dataclasses.replace(SH, allow_tier2_cards=True)
    assert on.tier_allows("TIER_2", "cards", "eng.2")
    assert combo_leg_selection(SH, "cards", 1.30, "TIER_1", "usa.1") is None


def test_cards_engine_trace_outside_list():
    for slug, league in (("usa.1", "MLS"), ("eng.2", "Championship")):
        eng = _engine()
        out = _drive(eng, "c" + slug, range(45, 66), slug=slug, league=league, cards=True)
        assert not [s for s in out if s.market is MarketType.TOTAL_CARDS]
        cards = {k: v for k, v in eng.last_trace["c" + slug].items() if k[0] is MarketType.TOTAL_CARDS}
        assert cards and set(cards.values()) == {"cards_league_blocked (Bet365 live Total Cards: TIER_1 majors only)"}


def test_card_line_is_half_and_inside_180_210():
    eng = _engine()
    out = _drive(eng, "ce", range(45, 66), cards=True)
    cards_tr = {k: v for k, v in eng.last_trace["ce"].items() if k[0] is MarketType.TOTAL_CARDS}
    assert cards_tr and not any(v.startswith("cards_league_blocked") for v in cards_tr.values())
    for (mk, line, _sel), v in cards_tr.items():
        if not is_half_line(line):
            assert v in ("cards: .5 lines only",) or not v.startswith(("not_best", "primary_fair_window"))
    for s in [x for x in out if x.market is MarketType.TOTAL_CARDS]:
        assert is_half_line(s.line) and 1.80 <= s.fair_odds <= 2.10 and "asian_alternatives" not in s.extra
    cards = [x for x in out if x.market is MarketType.TOTAL_CARDS]
    assert cards and len({(s.line, s.selection) for s in cards}) == 1  # one .5 card line (eng.1 is a listed major)
    assert cards[0].extra["paper_only"] is True


# -- 3. .5 primaries and Asian alternatives --------------------------------------------------------------
def test_line_params_and_v44_hooks():
    assert SH.prefer_half_lines is True and SH.half_line_fair_range == (1.55, 2.50) and SH.asian_alternatives_n == 3
    assert SH.prefer_half() and SH.primary_fair_window("goals") == (1.55, 2.50) and SH.asian_alternatives_max() == 3
    v44 = SyndicateParams()
    assert not v44.prefer_half() and v44.primary_fair_window("corners") is None
    assert v44.asian_alternatives_max() == 0
    assert BET365_HALF_LINE_MARKET == {"goals": "Match Goals", "corners": "2-Way Corners"}


def test_fmt_line_and_settlement_notes():
    assert [fmt_line(x) for x in (10.0, 10.25, 10.5, 10.75, 2.0)] == ["10.0", "10.25", "10.5", "10.75", "2.0"]
    assert asian_settlement_note(10.0, Selection.OVER, "corners") == "whole line: exactly 10 corners -> stake refunded"
    n = asian_settlement_note(10.25, Selection.OVER, "corners")
    assert "10.0 + 10.5" in n and "exactly 10 corners" in n and "half stake lost" in n and "half refunded" in n
    n = asian_settlement_note(10.75, Selection.OVER, "corners")
    assert "10.5 + 11.0" in n and "exactly 11 corners" in n and "half stake won" in n and "half refunded" in n
    n = asian_settlement_note(2.25, Selection.UNDER, "goals")
    assert "exactly 2 goals" in n and "half stake won" in n
    assert asian_settlement_note(10.5, Selection.OVER, "corners") == "half line: no push"


@pytest.fixture(scope="module")
def relaxed_signals():  # noqa: ANN201
    eng = _engine(params=RELAXED)
    out = []
    for i, (mh, rate) in enumerate(((1.1, 1.0), (1.2, 1.0), (1.3, 1.0), (1.3, 1.5))):
        out += _drive(eng, f"r{i}", range(20, 41), mh=mh, rate=rate)
    return eng, out


def test_primary_is_always_half_line(relaxed_signals):  # noqa: ANN001
    eng, out = relaxed_signals
    gc = [s for s in out if s.market in (MarketType.TOTAL_GOALS, MarketType.TOTAL_CORNERS)]
    assert gc, "the relaxed synthetic match must produce Goals / Corners alerts"
    for s in gc:
        assert is_half_line(s.line) and 1.55 <= s.fair_odds <= 2.50
        fam = "goals" if s.market is MarketType.TOTAL_GOALS else "corners"
        assert s.extra["bet365_market"] == BET365_HALF_LINE_MARKET[fam]
        for c in s.extra.get("combos") or []:
            assert c["legs"][0]["label"] == f"{s.selection.value.capitalize()} {s.line:g} {fam.capitalize()}"
            for leg in c["legs"]:  # every combo leg (the alert leg = the .5 primary) is a half line
                assert is_half_line(float(leg["label"].split()[1]))
        bt = s.extra.get("boost_target") or {}
        assert not bt or bt["min_odds"] == s.extra["min_odds"]  # the boost references the .5 primary
    traces = [v for k in eng.last_trace for v in eng.last_trace[k].values()]
    assert any(v == "asian_line: not a .5 primary (alternative only)" for v in traces)


def test_asian_alternatives_structure(relaxed_signals):  # noqa: ANN001
    _, out = relaxed_signals
    with_alts = [s for s in out if s.extra.get("asian_alternatives")]
    assert with_alts
    for s in with_alts:
        alts = s.extra["asian_alternatives"]
        assert 1 <= len(alts) <= 3
        for a in alts:
            assert set(a) == {"line", "selection", "settlement_note", "fair_odds", "min_odds", "abort_floor",
                              "first_tier_stake"}
            assert not is_half_line(a["line"]) and abs(a["line"] - s.line) in (0.25, 0.5)
            assert a["selection"] == s.selection.value
            assert a["min_odds"] > a["fair_odds"] >= 1.50 and a["abort_floor"] <= a["min_odds"]
            assert a["first_tier_stake"] > 0
        assert len({a["line"] for a in alts}) == len(alts)
        assert sum(1 for a in alts if float(a["line"]).is_integer()) <= 1  # one whole line at most
        txt = format_min_odds_signal(s)
        assert "🔀 [ASIAN ALTERNATIVES] (if Asian tab is open): " in txt and "instead of" in txt


def test_no_alternatives_when_prefer_half_off(relaxed_signals):  # noqa: ANN001
    eng = _engine(params=dataclasses.replace(RELAXED, prefer_half_lines=False))
    out = _drive(eng, "off", range(20, 41), mh=1.3, rate=1.0)
    assert all("asian_alternatives" not in s.extra and "bet365_market" not in s.extra for s in out)


def test_alternatives_line_format_exact():
    alts = [{"line": 10.25, "selection": "over", "min_odds": 1.88, "first_tier_stake": 25.0,
             "settlement_note": "q", "fair_odds": 1.80, "abort_floor": 1.84},
            {"line": 10.0, "selection": "over", "min_odds": 1.76, "first_tier_stake": 30.0,
             "settlement_note": "w", "fair_odds": 1.69, "abort_floor": 1.72},
            {"line": 10.75, "selection": "over", "min_odds": 2.12, "first_tier_stake": 20.0,
             "settlement_note": "q2", "fair_odds": 2.03, "abort_floor": 2.07}]
    lines = asian_alternatives_lines(alts, "EUR")
    assert lines[0] == ("🔀 [ASIAN ALTERNATIVES] (if Asian tab is open): Over 10.25 ≥ 1.88 (€25) | "
                        "Over 10.0 ≥ 1.76 (€30) | Over 10.75 ≥ 2.12 (€20)")
    assert "<b>instead of</b>" in lines[1] and "never in addition" in lines[1]
    assert asian_alternatives_lines([], "EUR") == []
    sig = _signal(asian_alternatives=alts, bet365_market="2-Way Corners")
    sig.currency = "EUR"
    txt = format_min_odds_signal(sig)
    ladder = txt.index("🧮 If Bet365 offers")
    assert txt.index(lines[0]) > ladder and "Over 10.5 Corners - Bet365 2-Way Corners" in txt
    assert "ASIAN ALTERNATIVES" not in format_min_odds_signal(_signal())
    assert "ASIAN ALTERNATIVES" not in format_min_odds_signal(_signal(paper_only=True, asian_alternatives=alts))


# -- 4. freeze ---------------------------------------------------------------------------------------------
def test_refreeze_new_hash_old_hashes_intact():
    from quantbet.config import Settings
    from quantbet.main import build_rules
    path = ROOT / "config" / "engine_b_params.json"
    fs = read_frozen_set(path, "v4.5-sharp")
    assert fs["sha256"] == V45_HASH != V45_PREV_HASH
    assert [x["sha256"] for x in fs["superseded"]][-1] == V45_PREV_HASH
    sp = fs["global"]["syndicate"]["params"]
    assert sp["allow_tier2_cards"] is False and sp["prefer_half_lines"] is True
    assert sp["cards_net_over"] == 1.18 and sp["cards_net_under"] == 0.85 and sp["asian_alternatives_n"] == 3
    assert list(sp["half_line_fair_range"]) == [1.55, 2.50] and list(sp["cards_fair_odds_range"]) == [1.80, 2.10]
    for rules, h in (("v4.2", V42_HASH), ("v4.3-global", V43_HASH), ("v4.4-syndicate", V44_HASH),
                     ("v4.5-sharp", V45_HASH)):
        assert build_rules(Settings(_env_file=None, alert_rules=rules))[2] == h  # type: ignore[call-arg]
    blob = json.dumps(json.loads(path.read_text()))
    assert "send_paper_alerts" not in blob  # runtime switch, not a model parameter
