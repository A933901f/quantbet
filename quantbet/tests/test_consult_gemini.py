"""scripts/consult_gemini.py with mocked HTTP (no network)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("consult_gemini", ROOT / "scripts/consult_gemini.py")
cg = importlib.util.module_from_spec(_spec)
sys.modules["consult_gemini"] = cg
_spec.loader.exec_module(cg)  # type: ignore[union-attr]

KEY = "AQ.TESTKEY1234567890abcdefXY"


def ok(text="do X next"):
    return cg.HttpResp(200, json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}]}).encode())


def err(code, msg="nope"):
    return cg.HttpResp(code, json.dumps({"error": {"code": code, "message": msg, "status": "X"}}).encode())


class Fake:
    def __init__(self, fn):
        self.fn, self.calls = fn, []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        return self.fn(method, url, headers)


def test_auth_falls_back_to_bearer():
    t = Fake(lambda m, u, h: ok() if h.get("Authorization") == f"Bearer {KEY}" else err(401, "API keys are not supported"))
    c = cg.Client(KEY, transport=t, sleep=lambda s: None)
    text, model, status = c.generate(cg.build_payload("hi", []))
    assert (text, model, status, c.auth_mode) == ("do X next", "gemini-2.5-flash", 200, "bearer")
    assert "x-goog-api-key" in t.calls[0][2] and "Authorization" not in t.calls[0][2]
    assert all(KEY not in call[1] for call in t.calls)  # key never in URL


def test_header_auth_preferred_when_it_works():
    t = Fake(lambda m, u, h: ok())
    c = cg.Client(KEY, transport=t)
    c.generate(cg.build_payload("hi", []))
    assert c.auth_mode == "x-goog-api-key" and len(t.calls) == 1


def test_model_fallback_to_listed_model():
    def fn(m, u, h):
        if m == "GET":
            return cg.HttpResp(200, json.dumps({"models": [
                {"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]},
                {"name": "models/gemini-9-test", "supportedGenerationMethods": ["generateContent"]}]}).encode())
        return ok("listed") if "gemini-9-test" in u else err(404)
    t = Fake(fn)
    c = cg.Client(KEY, transport=t)
    text, model, _ = c.generate(cg.build_payload("hi", []))
    assert (text, model) == ("listed", "gemini-9-test")
    posted = [u for m, u, *_ in t.calls if m == "POST"]
    assert "gemini-2.5-flash" in posted[0] and "gemini-2.5-pro" in posted[1]


def test_429_single_backoff_retry():
    seq = iter([err(429, "quota"), ok()])
    slept = []
    c = cg.Client(KEY, transport=Fake(lambda m, u, h: next(seq)), sleep=slept.append)
    assert c.generate(cg.build_payload("hi", []))[0] == "do X next"
    assert len(slept) == 1


def test_main_writes_output_and_masks_key_on_error(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text(f"OTHER=1\nGEMINI_API_KEY={KEY}\n")
    rep = tmp_path / "r.md"
    rep.write_text("# report\nBSS 0.05\n")
    out = tmp_path / "fb.md"
    seen = {}

    def fn(m, u, h):
        seen["payload"] = True
        return ok("reply body")
    t = Fake(fn)
    rc = cg.main(["--report", str(rep), "--prompt", "handshake", "--out", str(out), "--env-file", str(env)],
                 transport=t, sleep=lambda s: None)
    assert rc == 0
    body = json.loads(t.calls[0][3])
    assert body["systemInstruction"]["parts"][0]["text"] == cg.SYSTEM_INSTRUCTION
    assert "BSS 0.05" in body["contents"][0]["parts"][0]["text"]
    txt = out.read_text()
    assert "model: gemini-2.5-flash" in txt and "auth_mode: x-goog-api-key" in txt and "reply body" in txt
    assert KEY not in txt
    assert "reply body" in capsys.readouterr().out

    rc = cg.main(["--prompt", "x", "--out", str(out), "--env-file", str(env)],
                 transport=Fake(lambda m, u, h: err(400, f"bad key {KEY}")), sleep=lambda s: None)
    captured = capsys.readouterr()
    assert rc == 1 and "HTTP 400" in captured.err and KEY not in captured.err + captured.out


def test_rank_listed_skips_non_text_and_prefers_aliases():
    ms = ["gemini-2.5-flash-preview-tts", "gemma-4-31b-it", "gemini-3-pro-image", "gemini-3.8-flash",
          "gemini-flash-latest", "lyria-3.5"]
    assert cg.rank_listed(ms) == ["gemini-flash-latest", "gemini-3.8-flash", "gemma-4-31b-it"]


def test_transient_503_moves_to_next_model():
    t = Fake(lambda m, u, h: err(503, "busy") if "2.5-flash" in u else ok("pro"))
    text, model, _ = cg.Client(KEY, transport=t).generate(cg.build_payload("hi", []))
    assert (text, model) == ("pro", "gemini-2.5-pro")


def test_zero_quota_429_skips_model_without_backoff():
    slept = []
    t = Fake(lambda m, u, h: err(429, "Quota exceeded, limit: 0, model: x") if "2.5-flash" in u else ok("pro"))
    text, model, _ = cg.Client(KEY, transport=t, sleep=slept.append).generate(cg.build_payload("hi", []))
    assert (text, model, slept) == ("pro", "gemini-2.5-pro", [])


ORIGINAL_SYSTEM_TEXT = (
    "You are the Lead Quantitative Sports Betting Architect collaborating directly with the coding agent "
    "building `quantbet` (a $0/month Bet365 +EV live in-play football signal engine). Audit the attached "
    "report/status for Brier Skill Score (BSS), Kelly staking independence, Poisson/Negative-Binomial "
    "calibration, and data pipeline health. Reply with concise, direct, actionable engineering instructions "
    "for the agent to execute next."
)


def test_system_instruction_keeps_original_and_adds_hard_constraints():
    s = cg.SYSTEM_INSTRUCTION
    assert s.startswith(ORIGINAL_SYSTEM_TEXT)
    assert "HARD CONSTRAINTS" in s
    for banned in ("scraping Bet365", "WebSockets", "logging into Bet365", "browser automation",
                   "SofaScore", "OddsPortal", "Flashscore", "anti-bot evasion"):
        assert banned in s, banned
    for stack in ("ESPN open JSON", "__NEXT_DATA__", "Football-Data.co.uk", "free API", "Minimum Bet365 Odds",
                  "bets manually"):
        assert stack in s, stack
    for built in ("1 Goals + 1 Corners", "5% per-match", "quarter-Kelly", "eighth-Kelly", "single best line",
                  "Overs-only", "heartbeat gap logging", "SHA-256", "do NOT re-suggest",
                  "post-gap delta reset", "[PROBATION: n/150]"):
        assert built in s, built
    assert "variance/mean = 1.067" in s and "n=380" in s
    # sent verbatim as the system instruction
    assert cg.build_payload("q", [])["systemInstruction"]["parts"][0]["text"] == s


def test_quoted_epl_corner_dispersion_matches_data():
    csv_path = ROOT / "data/football_data/E0_2526.csv"
    if not csv_path.exists():
        pytest.skip("Football-Data E0_2526.csv not cached")
    spec = importlib.util.spec_from_file_location("corner_dispersion", ROOT / "scripts/corner_dispersion.py")
    cd = importlib.util.module_from_spec(spec)
    sys.modules["corner_dispersion"] = cd
    spec.loader.exec_module(cd)  # type: ignore[union-attr]
    m = cd.measure(csv_path)
    for k, v in cg.EPL_CORNER_DISPERSION.items():
        assert m[k] == pytest.approx(v, abs=1e-3), k
