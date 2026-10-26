"""League probation: OOS graded-bet counts per new tier / league, BSS vs prior, card tag."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from quantbet.global_layer import LeagueInfo, LeagueRegistry
from quantbet.notifier import format_min_odds_signal
from quantbet.paper import SCHEMA as PAPER_SCHEMA
from quantbet.paper import PaperBook
from quantbet.probation import (CORE_GROUP, ORIGINAL_CALIBRATED_SLUGS, PROBATION_N, ProbationBook, oos_cutoff,
                                probation_group, update_from_paper_dbs)

CUT = datetime(2026, 10, 25, 23, 49, 44, tzinfo=timezone.utc).timestamp()


def test_original_set_and_groups():
    for s in ("eng.1", "esp.1", "ger.1", "ita.1", "fra.1", "uefa.champions", "uefa.europa", "uefa.europa.conf",
              "eng.fa", "eng.league_cup", "esp.copa_del_rey", "ger.dfb_pokal", "ita.coppa_italia",
              "fra.coupe_de_france"):
        assert s in ORIGINAL_CALIBRATED_SLUGS and probation_group(s, "TIER_1") == CORE_GROUP
    assert probation_group("usa.1", "TIER_1") == "NEW_TIER_1"
    assert probation_group("bra.1", "TIER_1") == "NEW_TIER_1"
    assert probation_group("arg.1", "TIER_2") == "NEW_TIER_2"
    assert probation_group("espn.9999", None) == "NEW_TIER_3"  # auto-discovered / unknown tier


def test_oos_cutoff_reads_v43_frozen_at():
    assert oos_cutoff("config/engine_b_params.json") == CUT


def _paper(path, rows, legacy=False):
    db = sqlite3.connect(str(path))
    db.executescript(PAPER_SCHEMA)
    if not legacy:
        for c, t in (("league_slug", "TEXT"), ("liquidity_tier", "TEXT"), ("prior_fair", "REAL"), ("probation", "TEXT")):
            db.execute(f"ALTER TABLE paper_trades ADD COLUMN {c} {t}")
    for r in rows:
        cols = ", ".join(r)
        db.execute(f"INSERT INTO paper_trades ({cols}) VALUES ({', '.join('?' * len(r))})", tuple(r.values()))
    db.commit()
    db.close()


def _row(i, created, league, slug, tier, fair, prior, result, card=""):
    return {"signal_id": f"s{i}", "created_at": created, "league": league, "market": "TOTAL_CORNERS", "line": 9.5,
            "selection": "over", "fair_odds": fair, "result": result, "card": card, "league_slug": slug,
            "liquidity_tier": tier, "prior_fair": prior}


def test_counts_bss_and_tag(tmp_path):
    rows = []
    # 150 OOS graded NEW_TIER_2 bets; model sharper than prior: wins at p_model .6 vs prior .5, losses at .4 vs .5
    for i in range(150):
        win = i % 2 == 0
        rows.append(_row(i, CUT + 10 + i, "Argentine Liga", "arg.1", "TIER_2", 1 / 0.6 if win else 1 / 0.4, 2.0,
                         "win" if win else "loss"))
    rows.append(_row(900, CUT - 100, "Argentine Liga", "arg.1", "TIER_2", 2.0, 2.0, "win"))  # in-sample: ignored
    rows.append(_row(901, CUT + 5, "Argentine Liga", "arg.1", "TIER_2", 2.0, 2.0, None))  # ungraded: ignored
    rows.append(_row(902, CUT + 5, "MLS", "usa.1", "TIER_1", 2.0, None, "push"))  # counts in n, no BSS
    rows.append(_row(903, CUT + 5, "English Premier League", "eng.1", "TIER_1", 2.0, 2.0, "loss"))
    _paper(tmp_path / "p.sqlite3", rows)
    book = ProbationBook(tmp_path / "prob.sqlite3")
    n, out = update_from_paper_dbs(book, [tmp_path / "p.sqlite3"], CUT)
    assert n == 152
    g = {r["key"]: r for r in out if r["scope"] == "group"}
    assert g["NEW_TIER_2"]["n_graded"] == 150 and g["NEW_TIER_2"]["n_scored"] == 150
    # Brier model = .16, prior = .25 -> BSS = 1 - .16/.25 = .36
    assert g["NEW_TIER_2"]["bss"] == pytest.approx(0.36) and not g["NEW_TIER_2"]["on_probation"]
    assert g["NEW_TIER_1"]["n_graded"] == 1 and g["NEW_TIER_1"]["bss"] is None and g["NEW_TIER_1"]["on_probation"]
    assert not g[CORE_GROUP]["on_probation"]
    lg = {r["key"]: r for r in out if r["scope"] == "league"}
    assert lg["arg.1"]["n_graded"] == 150 and lg["usa.1"]["n_graded"] == 1
    assert book.tag("arg.1", "TIER_2") == ""  # graduated
    assert book.tag("usa.1", "TIER_1") == f"[PROBATION: 1/{PROBATION_N}]"
    assert book.tag("bra.1", "TIER_1") == "[PROBATION: 1/150]"  # tier-level count
    assert book.tag("chn.1", "TIER_3") == "[PROBATION: 0/150]"
    assert book.tag("eng.1", "TIER_1") == "" and book.tag("ger.dfb_pokal", "TIER_2") == ""
    # idempotent re-import (dedup by signal_id)
    n2, _ = update_from_paper_dbs(book, [tmp_path / "p.sqlite3"], CUT)
    assert n2 == 152 and book.db.execute("SELECT COUNT(*) FROM probation_bets").fetchone()[0] == 152


def test_negative_bss_keeps_probation_even_with_150(tmp_path):
    rows = [_row(i, CUT + 1 + i, "Chinese Super League", "chn.1", "TIER_3", 1 / 0.4 if i % 2 == 0 else 1 / 0.6, 2.0,
                 "win" if i % 2 == 0 else "loss") for i in range(160)]
    _paper(tmp_path / "p.sqlite3", rows)
    book = ProbationBook(tmp_path / "prob.sqlite3")
    _, out = update_from_paper_dbs(book, [tmp_path / "p.sqlite3"], CUT)
    g = {r["key"]: r for r in out if r["scope"] == "group"}["NEW_TIER_3"]
    assert g["n_graded"] == 160 and g["bss"] < 0 and g["on_probation"]
    assert book.tag("chn.1", "TIER_3") == "[PROBATION: 160/150]"


def test_legacy_paper_db_parses_card_tier_and_prior(tmp_path):
    card = ("[LIVE MODEL · MIN ODDS]\n[LIQUIDITY: TIER 3 - SMALL LEAGUE]\n⚽ A vs B\n"
            "📈 Prior fair 2.03 -> model 1.81 (lift +12.2%, net λ x1.12)\n")
    row = _row(1, CUT + 60, "Colombian Primera A", None, None, 1.81, None, "half-loss", card)
    for k in ("league_slug", "liquidity_tier", "prior_fair"):
        row.pop(k)
    _paper(tmp_path / "old.sqlite3", [row], legacy=True)
    reg = LeagueRegistry({"col.1": LeagueInfo("col.1", "Colombian Primera A", tier="TIER_3")})
    book = ProbationBook(":memory:")
    update_from_paper_dbs(book, [tmp_path / "old.sqlite3"], CUT, reg)
    r = book.db.execute("SELECT league_slug, tier, grp, prior_fair, y FROM probation_bets").fetchone()
    assert r == ("col.1", "TIER_3", "NEW_TIER_3", 2.03, 0.0)


def test_paper_db_opened_read_only(tmp_path):
    p = tmp_path / "live.sqlite3"
    _paper(p, [_row(1, CUT + 1, "MLS", "usa.1", "TIER_1", 2.0, 2.1, "win")])
    before = p.read_bytes()
    update_from_paper_dbs(ProbationBook(":memory:"), [p], CUT)
    assert p.read_bytes() == before


def test_engine_attaches_tag_and_card_shows_it(tmp_path):
    from test_global_v43 import _geng, _st  # type: ignore[import-not-found]

    reg = LeagueRegistry({"t3.x": LeagueInfo("t3.x", "Three", tier="TIER_3"),
                          "eng.1": LeagueInfo("eng.1", "EPL", tier="TIER_1")})
    book = ProbationBook(":memory:")
    book.recompute()
    for slug, want in (("t3.x", "[PROBATION: 0/150]"), ("eng.1", None)):
        eng = _geng(reg, table_slugs=(slug,))
        eng.probation = book
        st = _st(minute=35, hg=2, ag=2, hc=3, ac=2, mh=1.25, ma=1.25, league_slug=slug, key=f"k{slug}")
        sigs = eng.on_match_state(st, st.updated_at)
        assert sigs
        for s in sigs:
            assert s.extra.get("probation") == want
            txt = format_min_odds_signal(s)
            assert (want in txt) if want else ("PROBATION" not in txt)
        if want:
            lines = format_min_odds_signal(sigs[0]).splitlines()
            assert "LIQUIDITY" in lines[1] and "[PROBATION: 0/150]" in lines[2]
            pb = PaperBook(tmp_path / "rec.sqlite3", tmp_path / "rec.md")
            sigs[0].extra["model_only"] = True
            pb.record(sigs[0])
            r = pb.db.execute("SELECT league_slug, liquidity_tier, probation, card FROM paper_trades").fetchone()
            assert r[:3] == ("t3.x", "TIER_3", "[PROBATION: 0/150]") and "[PROBATION: 0/150]" in r[3]


def test_paperbook_stores_probation_columns_and_migrates(tmp_path):
    p = tmp_path / "pb.sqlite3"
    _paper(p, [], legacy=True)
    PaperBook(p, tmp_path / "a.md")
    cols = {r[1] for r in sqlite3.connect(str(p)).execute("PRAGMA table_info(paper_trades)")}
    assert {"league_slug", "liquidity_tier", "prior_fair", "probation"} <= cols


def test_probation_is_not_in_frozen_params():
    raw = json.loads(open("config/engine_b_params.json").read())
    assert "probation" not in json.dumps(raw).lower()
