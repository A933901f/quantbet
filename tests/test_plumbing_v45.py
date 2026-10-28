"""Forward-run plumbing on top of v4.5-sharp (no model change): paper-only alerts carry no stake, the syndicate
dedup is hydrated from the alerts DB on start-up, pre-kick-off /summary priority with provenance, manual Bet365
fills (scripts/record_fill.py + data/fills.csv) and their audit, the WAL checkpoint before the Actions cache save.
Offline, no network."""

from __future__ import annotations

import asyncio
import copy
import csv
import importlib.util
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from quantbet.database import FILL_COLUMNS, AlertDatabase, migrate_fill_columns
from quantbet.fills import Fill, append_fill, merge_fills, read_fills, validate_fill
from quantbet.free_stats_poller import FreePollerConfig, FreeStatsPoller, dk_lines, migrate_summary_db
from quantbet.models import MarketType, Selection, Signal, Urgency
from quantbet.notifier import format_min_odds_signal, format_signal
from quantbet.polite_http import FetchResult
from quantbet.risk import RiskConfig, RiskManager
from quantbet.syndicate import SharpParams, SyndicateDeduplicator, mark_paper_only

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
SH = SharpParams()
# the plumbing patch left 20e12b59... untouched; the later lines patch re-froze v4.5-sharp (test_lines_v45.py)
V45_HASH = "3e1ccaa0a84e1b067884bd84fb3e6d0faa0e840cbea65d3d7c6ab7d160ee5368"


def run(coro):  # noqa: ANN001, ANN201
    return asyncio.run(coro)


def _script(name: str):  # noqa: ANN202
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _sig(key="m1", market=MarketType.TOTAL_GOALS, sel=Selection.OVER, line=2.5, minute=60.0, paper=False,
         stake=20.0, created=None, family="goals", p=0.55) -> Signal:
    x = {"model_only": True, "min_odds": 2.05, "abort_floor": 2.0, "tiers": [[2.05, stake], [2.2, stake + 10]],
         "dedup": {"syndicate": True, "family": family, "selection": sel.value, "line": line, "p": p,
                   "stake": stake, "minute": minute, "reason": "first"}}
    s = Signal(urgency=Urgency.MIN_ODDS, engine="B", event_key=key, home="Home FC", away="Away FC",
               league="Premier League", market=market, line=line, selection=sel, bookmaker="bet365 (model-only)",
               bookmaker_odds=2.05, p_true=p, fair_odds=1 / p, min_acceptable_odds=2.0, ev=0.04,
               kelly_fraction=0.02, is_live=True, minute=minute, score="1-0", method="live:poisson", extra=x)
    if created is not None:
        s.created_at = created
    if paper:
        s.extra["dedup"]["paper"] = True
        mark_paper_only(s)
    return s


# ---------------------------------------------------------------------------
# 1. paper-only alerts: zero stake before sizing / DB insertion, excluded from the report
# ---------------------------------------------------------------------------
def test_mark_paper_only_zeroes_stake_and_keeps_theoretical():
    s = _sig(market=MarketType.TOTAL_CARDS, family="cards", line=4.5, paper=True)
    assert s.kelly_fraction == 0.0 and s.stake_units == 0.0 and s.stake_currency == 0.0
    assert s.extra["paper_only"] is True and s.extra["tiers"] == []
    assert s.extra["paper_stake"] == 20.0 and s.extra["paper_tiers"] == [[2.05, 20.0], [2.2, 30.0]]


def test_risk_size_all_keeps_paper_unsized_and_staked_sized():
    rm = RiskManager(RiskConfig(bankroll=2000.0))
    paper = _sig(market=MarketType.TOTAL_CARDS, family="cards", paper=True)
    paper.kelly_fraction = 0.05  # even if something upstream set a fraction, sizing must not stake it
    staked = _sig()
    out = rm.size_all([paper, staked])
    assert paper in out and staked in out
    assert paper.stake_currency == 0.0 and paper.stake_units == 0.0 and paper.kelly_fraction == 0.0
    assert paper.extra["tiers"] == [] and not any("stake" in n for n in paper.risk_notes)
    assert staked.stake_currency > 0


def test_engine_paper_card_has_no_stake_and_no_match_cap_use():
    from test_sharp_v45 import _paper_signal

    eng, cards = _paper_signal()
    if not cards:
        pytest.skip("no cards signal produced by the synthetic match (model-dependent)")
    sg = cards[0]
    assert sg.kelly_fraction == 0.0 and sg.stake_currency == 0.0 and sg.stake_units == 0.0
    assert sg.extra["tiers"] == [] and sg.extra["paper_stake"] > 0 and sg.extra["paper_tiers"]
    rm = RiskManager(RiskConfig(bankroll=2000.0))
    assert rm.size_all([sg]) == [sg] and sg.stake_currency == 0.0
    before = dict(eng.sdedup.match.get("pc", {"stake": 0.0, "n": 0}))
    eng.commit(sg)
    assert eng.sdedup.match.get("pc", {"stake": 0.0, "n": 0}) == before


def test_db_report_excludes_paper_and_old_paper_rows(tmp_path):
    async def go():
        async with AlertDatabase(tmp_path / "q.sqlite3") as db:
            st = _sig()
            st.stake_currency, st.stake_units = 20.0, 2.0
            await db.insert_signal(st)
            await db.insert_signal(_sig(key="m2", market=MarketType.TOTAL_CARDS, family="cards", paper=True))
            old = _sig(key="m3", market=MarketType.TOTAL_CARDS, family="cards")  # pre-fix row: paper WITH stake
            old.extra["paper_only"] = True
            old.stake_currency, old.stake_units = 15.0, 1.5
            await db.insert_signal(old)
            for sid in [st.signal_id]:
                await db.settle(sid, "win")
            await db.settle(old.signal_id, "loss")
            await db.db.execute("UPDATE alerts SET extra='not json' WHERE id='nope'")
            rep = await db.report(2000.0)
            cur = await db.db.execute("SELECT stake, stake_units, kelly_fraction FROM alerts WHERE event_key='m2'")
            row = await cur.fetchone()
            return rep, tuple(row)
    rep, paper_row = run(go())
    assert paper_row == (0.0, 0.0, 0.0)
    assert rep.n_alerts == 1 and rep.turnover == 20.0 and rep.profit == pytest.approx(21.0)


def test_db_report_survives_malformed_extra(tmp_path):
    async def go():
        async with AlertDatabase(tmp_path / "q.sqlite3") as db:
            s = _sig()
            s.stake_currency = 10.0
            await db.insert_signal(s)
            await db.db.execute("UPDATE alerts SET extra='{broken'")
            await db.db.commit()
            return await db.report(2000.0)
    assert run(go()).n_alerts == 1


# ---------------------------------------------------------------------------
# 2. dedup hydration from quantbet.sqlite3
# ---------------------------------------------------------------------------
def _db_with(tmp_path, sigs):  # noqa: ANN001, ANN202
    path = tmp_path / "quantbet.sqlite3"

    async def go():
        async with AlertDatabase(path) as db:
            for s in sigs:
                await db.insert_signal(s)
    run(go())
    return path


def test_load_recent_alerts_restores_cooldown_caps_and_direction(tmp_path):
    now = time.time()
    sigs = [_sig(key="a", minute=55.0, stake=40.0, created=now - 600),
            _sig(key="a", market=MarketType.TOTAL_CORNERS, family="corners", line=9.5, minute=58.0, stake=40.0,
                 created=now - 400),
            _sig(key="b", market=MarketType.TOTAL_CARDS, family="cards", sel=Selection.UNDER, line=4.5, minute=61.0,
                 paper=True, created=now - 300),
            _sig(key="old", minute=30.0, created=now - 4 * 3600)]  # outside the 3 h lookback
    path = _db_with(tmp_path, sigs)
    d = SyndicateDeduplicator(SH, 2000.0)
    assert d.load_recent_alerts(path, now=now) == 3
    assert d.match["a"] == {"stake": 80.0, "n": 2} and "b" not in d.match and "old" not in d.match
    assert d.dirs["a"] == {"over"} and d.paper_dirs["b"] == {"under"} and "b" not in d.dirs
    fam = d.last("a", "goals")
    assert fam["minute"] == 55.0 and fam["count"] == 1 and fam["line"] == 2.5 and fam["p"] == 0.55
    # cooldown is in MATCH minutes: the next observed state's minute is compared with the stored minute
    assert d.check("a", "goals", "over", 3.5, 10.0, 55.0 + SH.cooldown_min - 1, 0.9) == "cooldown"
    assert d.check("a", "goals", "under", 3.5, 10.0, 80.0, 0.9) in ("match_direction_conflict", "family_conflict")
    # 5% match cap (100 at 2000) already holds 80
    assert d.check("a", "cards", "over", 4.5, 30.0, 80.0, None) == "stake_cap"
    assert d.remaining_stake("a") == pytest.approx(20.0)
    # an identical fresh engine that replayed the same records live would be in the same state
    live = SyndicateDeduplicator(SH, 2000.0)
    for s in sigs[:3]:
        x = s.extra["dedup"]
        live.record(s.event_key, x["family"], x["selection"], x["line"], x["p"], x["stake"], x["minute"],
                    paper=bool(x.get("paper")))
    assert (live.family, live.match, live.dirs, live.paper_dirs) == (d.family, d.match, d.dirs, d.paper_dirs)


def test_load_recent_alerts_missing_db_old_schema_and_malformed(tmp_path):
    d = SyndicateDeduplicator(SH, 2000.0)
    assert d.load_recent_alerts(tmp_path / "missing.sqlite3") == 0
    # a DB without an alerts table
    p = tmp_path / "empty.sqlite3"
    sqlite3.connect(p).execute("CREATE TABLE other (x)").connection.commit()
    assert d.load_recent_alerts(p) == 0
    # old schema: alerts table without created_at
    p2 = tmp_path / "old.sqlite3"
    c = sqlite3.connect(p2)
    c.execute("CREATE TABLE alerts (id TEXT, event_key TEXT, extra TEXT)")
    c.commit()
    c.close()
    assert d.load_recent_alerts(p2) == 0
    # v4.3 rows (no dedup.syndicate), malformed JSON, missing fields -> skipped, the good row is kept
    now = time.time()
    good = _sig(key="g", created=now - 60)
    v43 = _sig(key="v43", created=now - 50)
    v43.extra["dedup"] = {"family": "goals", "line": 2.5, "stake": 15.0, "minute": 55.0, "sig": [2, 2, 0, 0]}
    bad = _sig(key="bad", created=now - 40)
    half = _sig(key="half", created=now - 30)
    del half.extra["dedup"]["line"]
    path = _db_with(tmp_path, [good, v43, bad, half])
    c = sqlite3.connect(path)
    c.execute("UPDATE alerts SET extra='{not json' WHERE event_key='bad'")
    c.commit()
    c.close()
    d2 = SyndicateDeduplicator(SH, 2000.0)
    assert d2.load_recent_alerts(path, now=now) == 1 and set(d2.match) == {"g"}


def test_run_free_wires_hydration_and_workflow_keeps_state():
    src = (ROOT / "src/quantbet/main.py").read_text()
    assert "free.sdedup.load_recent_alerts(s.db_path, lookback_s=s.dedup_hydrate_lookback_s)" in src
    from quantbet.config import Settings

    assert Settings().dedup_hydrate_lookback_s == 10800.0
    wf = (ROOT / ".github/workflows/live_quantbet.yml").read_text()
    assert wf.count("path: data/*.sqlite3") == 2 and "restore-keys: quantbet-state-" in wf
    i_ck, i_save = wf.index("PRAGMA wal_checkpoint(TRUNCATE)"), wf.index("uses: actions/cache/save@v4")
    assert i_ck < i_save  # WAL folded into the main file before the cache save


# ---------------------------------------------------------------------------
# 3. pre-kick-off /summary priority, provenance, no in-play overwrite
# ---------------------------------------------------------------------------
class _Fetch:
    def __init__(self, bodies):  # noqa: ANN001
        self.bodies = list(bodies)
        self.calls: list[tuple[str, float]] = []

    async def get_json(self, url, cache_ttl_s=15.0, headers=None):  # noqa: ANN001, ANN201
        self.calls.append((url, cache_ttl_s))
        return FetchResult(url, 200, self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0])


def _body(state="pre", total=None):  # noqa: ANN001, ANN202
    b = json.loads((FIX / "espn_summary_live.json").read_text())
    b["header"]["competitions"][0]["status"]["type"]["state"] = state
    if total is not None:
        pc = b["pickcenter"][0]
        pc["overUnder"] = total
        pc["total"]["over"]["close"]["line"] = f"o{total}"
        pc["total"]["under"]["close"]["line"] = f"u{total}"
    return b


def _ev(i="401", state="pre", ko_in_min=30.0, league="eng.1"):  # noqa: ANN001, ANN202
    start = datetime.fromtimestamp(time.time() + ko_in_min * 60, timezone.utc)
    return SimpleNamespace(id=i, league=league, league_name="Premier League", home="H" + i, away="A" + i, state=state,
                           status={"type": {"state": state}, "displayClock": "0'", "clock": 0.0}, start=start,
                           live=state == "in")


def _poller(tmp_path, bodies, **kw):  # noqa: ANN001, ANN202
    cfg = FreePollerConfig(espn_leagues=("eng.1",), summary_db_path=str(tmp_path / "s.sqlite3"),
                           prekick_window_min=45.0, prekick_summaries_per_cycle=4, prekick_refresh_min=10.0, **kw)
    return FreeStatsPoller(_Fetch(bodies), lambda s: None, cfg=cfg)


def test_summary_db_migration_backfills_state_from_body(tmp_path):
    p = tmp_path / "s.sqlite3"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE espn_summary_once (event_id TEXT PRIMARY KEY, league TEXT, fetched_at REAL, body TEXT)")
    c.execute("INSERT INTO espn_summary_once VALUES ('1','eng.1',1.0,?)", (json.dumps(_body("pre")),))
    c.execute("INSERT INTO espn_summary_once VALUES ('2','eng.1',2.0,?)", (json.dumps(_body("in")),))
    c.execute("INSERT INTO espn_summary_once VALUES ('3','eng.1',3.0,'{broken')")
    c.commit()
    added = migrate_summary_db(c)
    assert "fetched_state" in added and "latest_body" in added
    rows = dict(c.execute("SELECT event_id, fetched_state FROM espn_summary_once").fetchall())
    assert rows == {"1": "pre", "2": "in", "3": "unknown"}
    assert migrate_summary_db(c) == []  # idempotent
    c.commit()
    c.close()
    # the poller opens a migrated (or old) DB fine
    FreeStatsPoller(_Fetch([_body()]), lambda s: None, cfg=FreePollerConfig(summary_db_path=str(p)))


def test_prekick_fetch_first_then_late_refresh_keeps_both(tmp_path):
    p = _poller(tmp_path, [_body("pre", 3.5), _body("pre", 2.5)])
    ev = _ev(ko_in_min=40.0)
    run(p._prekick_summaries([("k1", ev)], time.time()))
    assert len(p.f.calls) == 1 and p.prior_meta["k1"]["fetched_state"] == "pre"
    assert p.prior_meta["k1"]["snapshot"] == "first" and p.prior_meta["k1"]["kickoff"] == ev.start.isoformat()
    first_prior = p.priors["k1"][0]
    # still > 10 min to kick-off: no refresh
    run(p._prekick_summaries([("k1", ev)], time.time()))
    assert len(p.f.calls) == 1
    # a first fetch made inside the last 10 min already is a late line -> no refresh
    (tmp_path / "late").mkdir()
    late = _poller(tmp_path / "late", [_body("pre")])
    ev_late = _ev("402", ko_in_min=5.0)
    run(late._prekick_summaries([("kl", ev_late)], time.time()))
    run(late._prekick_summaries([("kl", ev_late)], time.time()))
    assert len(late.f.calls) == 1 and "kl" in late._prekick_refreshed
    # inside the last 10 min with a first line fetched 40 min out: one refresh, bypassing the 24 h response cache
    p.prior_meta["k1"]["first"]["fetched_at"] -= 35 * 60
    ev.start = datetime.fromtimestamp(time.time() + 5 * 60, timezone.utc)
    run(p._prekick_summaries([("k1", ev)], time.time()))
    run(p._prekick_summaries([("k1", ev)], time.time()))
    assert len(p.f.calls) == 2 and p.f.calls[0][1] == 24 * 3600 and p.f.calls[1][1] == 60.0
    meta = p.prior_meta["k1"]
    assert meta["snapshot"] == "latest_pre" and meta["first"]["dk_total_line"] != meta["latest"]["dk_total_line"]
    assert p.priors["k1"][0] is not first_prior and p.priors["k1"][0].total_line == meta["latest"]["dk_total_line"]
    row = sqlite3.connect(tmp_path / "s.sqlite3").execute(
        "SELECT fetched_state, latest_state, kickoff FROM espn_summary_once WHERE event_id='401'").fetchone()
    assert row[:2] == ("pre", "pre") and row[2]  # kickoff stored with the first fetch
    # a restart restores both snapshots from SQLite without a request
    p2 = _poller(tmp_path, [_body("in")])
    run(p2._prekick_summaries([("k1", ev)], time.time()))
    assert p2.f.calls == [] and p2.prior_meta["k1"]["snapshot"] == "latest_pre"
    assert p2.priors["k1"][0].total_line == meta["latest"]["dk_total_line"]


def test_stored_pre_summary_never_overwritten_by_in_play(tmp_path):
    p = _poller(tmp_path, [_body("pre", 3.5)])
    ev = _ev(ko_in_min=20.0)
    run(p._prekick_summaries([("k", ev)], time.time()))
    # simulate an in-play first-fetch path for the same event id (e.g. another key) -> row must stay 'pre'
    p._store_summary("401", "eng.1", _body("in", 1.5), "in", 50.0, ev.start.isoformat())
    st = sqlite3.connect(tmp_path / "s.sqlite3").execute(
        "SELECT fetched_state, fetched_minute FROM espn_summary_once WHERE event_id='401'").fetchone()
    assert st == ("pre", None)
    # in-play: no refresh is ever made and the in-memory prior is not replaced
    before = p.priors["k"]
    ev.state, ev.live = "in", True
    run(p._summaries_once([("k", ev)]))
    assert p.priors["k"] is before and len(p.f.calls) == 1
    # the late refresh only stores PRE snapshots
    p._store_latest("401", _body("in"), "in", 60.0)
    assert sqlite3.connect(tmp_path / "s.sqlite3").execute(
        "SELECT latest_state FROM espn_summary_once WHERE event_id='401'").fetchone() == (None,)


def test_prekick_candidates_listed_leagues_only_and_own_budget(tmp_path):
    p = _poller(tmp_path, [_body("pre")], espn_summaries_per_cycle=0)
    evs = [_ev(str(400 + i), ko_in_min=10 + i) for i in range(6)] + [_ev("999", league="espn.123", ko_in_min=5)]
    for e in evs:
        e.home_stats, e.away_stats, e.home_score, e.away_score, e.status_name = {}, {}, 0, 0, "STATUS_SCHEDULED"
        e.home_reds = e.away_reds = 0
        e.home_id = e.away_id = "1"
        e.home_yellows = e.away_yellows = e.home_card_points = e.away_card_points = None
    far = _ev("500", ko_in_min=120.0)  # beyond the 45-min window: left to the (frozen) normal budget
    for k, v in vars(evs[0]).items():
        if not hasattr(far, k):
            setattr(far, k, v)
    run(p._process_events(list(reversed(evs)) + [far], time.time(), []))
    assert len(p.f.calls) == 4  # own budget of 4 per cycle, soonest kick-off first, normal budget 0
    assert [c[0].split("event=")[-1] for c in p.f.calls] == ["400", "401", "402", "403"]
    assert all(m["fetched_state"] == "pre" for m in p.prior_meta.values()) and len(p.prior_meta) == 4
    run(p._process_events(evs + [far], time.time(), []))
    ids = [c[0].split("event=")[-1] for c in p.f.calls]
    assert ids[4:] == ["404", "405"]  # next cycle: the remaining listed matches; the auto-discovered one never
    assert "999" not in ids and "500" not in ids


def test_resolver_attaches_prior_provenance():
    from quantbet.engine import SharpPrices
    from quantbet.global_layer import GlobalRatingsResolver, LeagueInfo, LeagueRegistry

    reg = LeagueRegistry({"eng.1": LeagueInfo("eng.1", "Premier League", "TIER_1", espn_id="700", supported=True)})
    pr = SharpPrices(1.9, 3.6, 4.2, 2.5, 1.95, 1.9)
    meta = {"fetched_state": "pre", "fetched_minute": None, "fetched_at": 1.0, "kickoff": "2026-10-29T19:00Z",
            "snapshot": "latest_pre", "first": {"dk_total_line": 2.5}, "latest": {"dk_total_line": 2.75}}
    res = GlobalRatingsResolver(reg, None, None, prior_lookup=lambda k: (pr, "espn_draftkings"),
                                slug_lookup=lambda k: "eng.1")
    r0, src0 = res.resolve("k", "H", "A", "Premier League")
    assert "fetch" not in res.prior_info["k"]
    res.prior_meta_lookup = lambda k: meta
    r1, src1 = res.resolve("k", "H", "A", "Premier League")
    info = res.prior_info["k"]
    assert (r0, src0) == (r1, src1)  # provenance is informational: same ratings
    assert info["fetch"]["true_pre_match"] is True and info["fetch"]["snapshot"] == "latest_pre"
    assert info["dk_latest"]["dk_total_line"] == 2.75


def test_dk_lines_of_fixture():
    d = dk_lines(json.loads((FIX / "espn_summary_live.json").read_text()))
    assert d["dk_total_line"] == 3.5 and d["dk_over"] == 2.0 and d["dk_home"] > 1.0
    assert dk_lines({"no": "pickcenter"}) == {}


# ---------------------------------------------------------------------------
# 4. manual fills: record_fill.py, data/fills.csv, DB columns, audit
# ---------------------------------------------------------------------------
def test_fill_validation_and_csv_roundtrip(tmp_path):
    with pytest.raises(ValueError):
        validate_fill(Fill("abcdef123456", "filled", 1.01, 10.0, None, 1.0))
    with pytest.raises(ValueError):
        validate_fill(Fill("abcdef123456", "skipped", 2.0, None, None, 1.0))
    with pytest.raises(ValueError):
        validate_fill(Fill("not-hex!", "filled", 2.0, None, None, 1.0))
    p = tmp_path / "fills.csv"
    append_fill(p, Fill("abcdef123456", "filled", 2.05, 20.0, None, 1_800_000_000.0))
    append_fill(p, Fill("abcdef999999", "skipped", None, None, "line_moved", 1_800_000_100.0))
    with p.open("a") as fh:  # a hand-edited bad row (GitHub web editor) is reported, not fatal
        fh.write("zzz,filled,0.5,,,\n")
    fills, problems = read_fills(p)
    assert [f.fill_status for f in fills] == ["filled", "skipped"] and len(problems) == 1
    assert list(csv.reader(p.open()))[0] == ["signal_id", "fill_status", "taken_odds", "taken_stake",
                                             "fill_reason", "filled_at"]
    assert (ROOT / "data/fills.csv").read_text().splitlines()[0] == ",".join(
        ["signal_id", "fill_status", "taken_odds", "taken_stake", "fill_reason", "filled_at"])


def test_db_fill_columns_migration_and_record_fill(tmp_path):
    p = tmp_path / "old.sqlite3"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE alerts (id TEXT PRIMARY KEY, created_at REAL, extra TEXT)")
    c.commit()
    assert migrate_fill_columns(c) == [n for n, _ in FILL_COLUMNS] and migrate_fill_columns(c) == []
    c.close()

    async def go():
        async with AlertDatabase(tmp_path / "q.sqlite3") as db:
            s = _sig()
            await db.insert_signal(s)
            await db.record_fill(s.signal_id, "filled", 2.1, 20.0)
            with pytest.raises(KeyError):
                await db.record_fill("nope", "skipped")
            cur = await db.db.execute("SELECT taken_odds, taken_stake, fill_status FROM alerts")
            return tuple(await cur.fetchone())
    assert run(go()) == (2.1, 20.0, "filled")


def test_record_fill_cli(tmp_path, capsys):
    rf = _script("record_fill")
    now = time.time()
    a1, a2, paper = _sig(created=now - 100), _sig(key="m2", created=now - 50), _sig(
        key="m3", market=MarketType.TOTAL_CARDS, family="cards", paper=True, created=now - 10)
    a1.stake_currency = 20.0
    db = _db_with(tmp_path, [a1, a2, paper])
    fills = tmp_path / "fills.csv"
    base = ["--db", str(db), "--fills", str(fills)]
    assert rf.main([a1.signal_id[:8], "--odds", "2.12", *base]) == 0  # unique prefix accepted
    assert rf.main([a1.signal_id, "--odds", "2.30", *base]) == 2  # double fill refused
    assert rf.main([a1.signal_id, "--odds", "2.30", "--force", *base]) == 0
    assert rf.main([a2.signal_id, "--skipped", "--reason", "line_moved", *base]) == 0
    assert rf.main([paper.signal_id, "--odds", "2.0", *base]) == 2  # paper is never a bet
    assert rf.main(["abcdef000000", "--odds", "2.0", *base]) == 2  # unknown id
    assert rf.main([a2.signal_id, "--odds", "1.01", "--force", *base]) == 2  # odds must be > 1.01
    assert rf.main(["abcdef000000", "--odds", "2.0", "--db", str(tmp_path / "none.sqlite3"),
                    "--fills", str(fills)]) == 2  # no DB and no --no-db
    assert rf.main(["abcdef000001", "--odds", "2.0", "--no-db", "--db", str(tmp_path / "none.sqlite3"),
                    "--fills", str(fills)]) == 0
    row = sqlite3.connect(db).execute(
        "SELECT taken_odds, taken_stake, fill_status FROM alerts WHERE id=?", (a1.signal_id,)).fetchone()
    assert row == (2.3, 20.0, "filled")
    rows = list(csv.DictReader(fills.open()))
    assert [r["fill_status"] for r in rows] == ["filled", "filled", "skipped", "filled"]
    capsys.readouterr()
    sqlite3.connect(db).execute("UPDATE alerts SET fill_status=NULL").connection.commit()
    assert rf.main(["--list", *base]) == 0
    out = capsys.readouterr().out
    assert "0 unfilled" in out  # a1 / a2 are in the CSV; the paper card is never listed
    fills.write_text("signal_id,fill_status,taken_odds,taken_stake,fill_reason,filled_at\n")
    assert rf.main(["--list", *base]) == 0
    out = capsys.readouterr().out
    assert "2 unfilled" in out and a1.signal_id in out and paper.signal_id not in out


def test_audit_fill_summary_and_merge():
    audit = _script("audit_run")
    from quantbet.paper import settle_total

    def alert(i, result_total, fill=None, paper=False, min_odds=2.0):  # noqa: ANN001, ANN202
        a = {"id": f"abcdef00000{i}", "odds": min_odds, "min_odds": min_odds - 0.05, "stake": 20.0,
             "line": 2.5, "selection": "over", "x": {"min_odds": min_odds, **({"paper_only": True} if paper else {})},
             "parts": settle_total(2.5, "over", result_total)}
        if fill:
            a.update(fill)
        return a

    alerts = [alert(1, 3), alert(2, 1), alert(3, 3), alert(4, 3, paper=True)]
    csv_fills = [Fill("abcdef000001", "filled", 2.2, 20.0, None, 10.0),
                 Fill("abcdef000002", "filled", 1.9, None, None, 10.0),
                 Fill("abcdef00000", "skipped", None, None, "x", 1.0),  # ambiguous prefix
                 Fill("abcdef000003", "skipped", None, None, "line_moved", 5.0),
                 Fill("abcdef000004", "filled", 2.0, 5.0, None, 5.0),
                 Fill("ffffff", "filled", 2.0, None, None, 1.0)]
    # a DB fill that is older than the CSV row loses
    alerts[2].update({"fill_status": "filled", "taken_odds": 2.5, "taken_stake": 10.0, "filled_at": 1.0})
    info = merge_fills(alerts, csv_fills)
    assert info == {"unmatched": ["ffffff"], "ambiguous": ["abcdef00000"]}
    F = audit.fill_summary(alerts)
    assert F["n_staked"] == 3 and F["n_filled"] == 2 and F["n_skipped"] == 1 and F["n_unrecorded"] == 0
    assert F["fill_rate_pct"] == pytest.approx(200 / 3) and F["skip_reasons"] == {"line_moved": 1}
    assert F["paper_fills_ignored"] == 1
    assert F["avg_slippage"] == pytest.approx(((2.2 - 2.0) + (1.9 - 2.0)) / 2) and F["below_min_odds"] == 1
    assert F["realized_pnl"] == pytest.approx(20 * 1.2 - 20) and F["theoretical_pnl_min_odds"] == pytest.approx(0.0)
    assert F["realized_flat"] == pytest.approx(0.2) and F["theoretical_flat_all_graded"] == pytest.approx(1.0)


def test_audit_prior_provenance_labels():
    audit = _script("audit_run")
    base = {"home": "H", "away": "A", "market": "TOTAL_GOALS", "line": 2.5, "selection": "over", "odds": 2.0,
            "min_odds": 1.95}
    pre = dict(base, id="a1", x={"prior": {"source": "espn_draftkings", "lambda_goals_prior": 2.7,
                                           "fetch": {"fetched_state": "pre"},
                                           "dk_latest": {"dk_home": 1.9, "dk_draw": 3.6, "dk_away": 4.2,
                                                         "dk_total_line": 2.5, "dk_over": 1.95, "dk_under": 1.9}},
                                 "lambda_pre_total": 2.7, "lift": {"lambda_prior": 1.1}, "home_goals": 1})
    inplay = dict(base, id="a2", x={"prior": {"source": "espn_draftkings", "fetch": {"fetched_state": "in"}}})
    nomkt = dict(base, id="a3", x={"prior": {"source": "league_default"}})
    P = audit.prior_provenance([pre, inplay, nomkt], [])
    assert P["counts"] == {"true_pre_match": 1, "in_play_fetch": 1, "no_market_prior": 1}
    r = P["rows"][0]
    assert r["pre_ref_ev"] is not None and r["lambda_move_pct"] is not None and "NOT Bet365" in P["note"]


def test_card_shows_short_id_line():
    s = _sig()
    assert f"ID: <code>{s.signal_id}</code>" in format_min_odds_signal(s)
    s2 = _sig()
    s2.urgency = Urgency.LIVE_VALUE if hasattr(Urgency, "LIVE_VALUE") else s2.urgency
    assert f"ID: <code>{s2.signal_id}</code>" in format_signal(s2)


def test_plumbing_does_not_touch_frozen_params():
    from quantbet.config import Settings
    from quantbet.main import build_rules

    s = Settings(alert_rules="v4.5-sharp")
    assert build_rules(s)[2] == V45_HASH
    fs = json.loads((ROOT / "config/engine_b_params.json").read_text())
    blob = json.dumps(fs)
    for k in ("prekick", "dedup_hydrate", "fills"):
        assert k not in blob
