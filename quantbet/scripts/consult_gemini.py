#!/usr/bin/env python3
"""Consult Google Gemini about a quantbet report (stdlib only; works with .venv python or plain python3).

    python3 scripts/consult_gemini.py --report dryrun/report_X.md --prompt "question" [--model M] [--out F]

* Key: ``GEMINI_API_KEY`` from the environment or ``.env`` (repo root). Never logged in full.
* Model: --model, else gemini-2.5-flash, gemini-2.5-pro; if those 404, the first text-capable model from
  ``GET /v1beta/models`` that supports ``generateContent`` (``*-latest`` aliases preferred; tts/image skipped).
* Auth: ``x-goog-api-key`` header first; on 401/403 retried with ``Authorization: Bearer``.
  The key is never put in a URL.
* Reply printed to stdout and written to --out (default dryrun/gemini_feedback_latest.md).
Gemini's reply is advisory text only - nothing in it is executed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODELS = ("gemini-2.5-flash", "gemini-2.5-pro")
USER_AGENT = "quantbet-consult-gemini/1.0 (+python-urllib; research tool)"
MAX_REPORT_CHARS = 120_000  # per report; keeps head + tail when exceeded
SYSTEM_INSTRUCTION = (
    "You are the Lead Quantitative Sports Betting Architect collaborating directly with the coding agent "
    "building `quantbet` (a $0/month Bet365 +EV live in-play football signal engine). Audit the attached "
    "report/status for Brier Skill Score (BSS), Kelly staking independence, Poisson/Negative-Binomial "
    "calibration, and data pipeline health. Reply with concise, direct, actionable engineering instructions "
    "for the agent to execute next."
)
# Measured offline with scripts/corner_dispersion.py on Football-Data E0_2526.csv (full 2025/26 EPL season).
# tests/test_consult_gemini.py recomputes these and fails if the quoted figures drift from the data.
EPL_CORNER_DISPERSION = {
    "n_matches": 380,
    "total_mean": 9.997,
    "total_var_over_mean": 1.067,             # raw: total corners per match, variance(ddof=1)/mean
    "total_team_adjusted_dispersion": 1.047,  # Pearson chi2/df of Poisson GLM total ~ home_team + away_team
    "side_team_adjusted_dispersion": 1.325,   # per-team counts: corners ~ home + attack + defence
    "home_away_corr": -0.289,
}
_D = EPL_CORNER_DISPERSION
HARD_CONSTRAINTS = (
    "\n\nHARD CONSTRAINTS (non-negotiable; any suggestion violating them is rejected unread):\n"
    "1. NEVER suggest scraping Bet365, using Bet365 WebSockets/live feeds, logging into Bet365, or any "
    "browser automation (Selenium/Playwright/Puppeteer/headless browsers).\n"
    "2. NEVER suggest scraping Cloudflare/anti-bot-protected sites (SofaScore, OddsPortal, Flashscore, etc.) "
    "and NEVER suggest anti-bot evasion (proxy rotation, fingerprint spoofing, CAPTCHA solving, TLS impersonation).\n"
    "3. The data stack is fixed: ESPN open JSON, FotMob HTML __NEXT_DATA__, Football-Data.co.uk CSVs, free API "
    "tiers, and Reverse Pricing (we publish a 'Minimum Bet365 Odds' and the user checks Bet365 and bets "
    "manually). No automated bet placement.\n"
    "4. ALREADY BUILT - do NOT re-suggest these: hard max 1 Goals + 1 Corners alert per match; 5% per-match "
    "stake cap; quarter-Kelly staking with eighth-Kelly for Tier-3 leagues; single best line per market "
    "family; Overs-only alerts; heartbeat gap logging; post-gap delta reset (polling gap > 180 s -> fresh baseline, neutral momentum "
    "until a post-gap window exists); league probation tag '[PROBATION: n/150]' for newly added global tiers "
    "until n >= 150 graded out-of-sample bets with BSS > 0; frozen parameter sets verified by SHA-256 hashes "
    "(v4.2, v4.3-global) that must not be changed.\n"
    f"5. Measured corner dispersion, Football-Data E0 2025/26 (n={_D['n_matches']} matches, mean "
    f"{_D['total_mean']:.2f} total corners): total corners per match variance/mean = "
    f"{_D['total_var_over_mean']:.3f}; team-adjusted (Poisson GLM home+away team) Pearson dispersion = "
    f"{_D['total_team_adjusted_dispersion']:.3f} - i.e. match totals are near-Poisson. Per-team corner counts "
    f"are over-dispersed (team-adjusted dispersion {_D['side_team_adjusted_dispersion']:.3f}) with home/away "
    f"correlation {_D['home_away_corr']:.2f}, so do not assume independent Poisson sides.\n"
    "Focus on improvements inside these constraints; if you think one blocks a real gain, say so in one line "
    "instead of proposing a workaround."
)
SYSTEM_INSTRUCTION = SYSTEM_INSTRUCTION + HARD_CONSTRAINTS
AUTH_MODES = ("x-goog-api-key", "bearer")
# models that list generateContent but cannot return plain text (or are agents/tools, not chat models)
NON_TEXT_HINTS = ("tts", "image", "banana", "lyria", "embedding", "audio", "live", "transcribe", "robotics",
                  "computer-use", "deep-research", "antigravity", "customtools", "aqa")
PREFERRED_ALIASES = ("gemini-flash-latest", "gemini-pro-latest")


def rank_listed(models: list[str]) -> list[str]:
    """Text-capable models only; stable aliases first, then other gemini-*, then the rest (e.g. gemma)."""
    text = [m for m in models if not any(h in m.lower() for h in NON_TEXT_HINTS)]
    pref = [m for m in PREFERRED_ALIASES if m in text]
    gem = [m for m in text if m.startswith("gemini-") and m not in pref]
    return pref + gem + [m for m in text if m not in pref and m not in gem]


class GeminiError(RuntimeError):
    def __init__(self, status: int | None, message: str):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status
        self.message = message


@dataclass
class HttpResp:
    status: int
    body: bytes


# transport(method, url, headers, body_bytes|None, timeout) -> HttpResp ; swappable in tests
Transport = Callable[[str, str, dict, "bytes | None", float], HttpResp]


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float) -> HttpResp:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return HttpResp(r.status, r.read())
    except urllib.error.HTTPError as e:
        return HttpResp(e.code, e.read() or b"")


def mask(key: str | None) -> str:
    if not key:
        return "<none>"
    return f"{key[:7]}...{key[-2:]}" if len(key) > 12 else "***"


def scrub(text: str, key: str | None) -> str:
    return text.replace(key, mask(key)) if key else text


def load_key(env_path: Path | None = None) -> str | None:
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"].strip()
    p = env_path or ROOT / ".env"
    if not p.exists():
        return None
    try:
        from dotenv import dotenv_values  # type: ignore
        v = dotenv_values(p).get("GEMINI_API_KEY")
        return v.strip() if v else None
    except ImportError:
        pass
    for line in p.read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if line.startswith("GEMINI_API_KEY="):
            v = line.split("=", 1)[1].strip().strip('"').strip("'")
            return v or None
    return None


def _err_message(resp: HttpResp) -> str:
    try:
        j = json.loads(resp.body.decode("utf-8", "replace"))
        e = j.get("error", {}) if isinstance(j, dict) else {}
        return f"{e.get('status', '')} {e.get('message', '')}".strip() or resp.body[:500].decode("utf-8", "replace")
    except (ValueError, AttributeError):
        return resp.body[:500].decode("utf-8", "replace")


def read_report(path: Path, limit: int = MAX_REPORT_CHARS) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= limit:
        return text
    head, tail = text[: int(limit * 0.7)], text[-int(limit * 0.3):]
    return f"{head}\n\n[... truncated {len(text) - len(head) - len(tail)} chars ...]\n\n{tail}"


def build_payload(prompt: str, reports: list[tuple[str, str]]) -> dict:
    parts = [prompt]
    for name, text in reports:
        parts.append(f"\n\n===== REPORT: {name} =====\n{text}\n===== END REPORT: {name} =====")
    return {"systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
            "contents": [{"role": "user", "parts": [{"text": "".join(parts)}]}]}


@dataclass
class Client:
    key: str
    transport: Transport = urllib_transport
    timeout: float = 120.0
    sleep: Callable[[float], None] = time.sleep
    auth_mode: str | None = None  # sticky once one works
    log: list[str] = field(default_factory=list)

    def _headers(self, mode: str) -> dict:
        h = {"User-Agent": USER_AGENT, "Content-Type": "application/json", "Accept": "application/json"}
        if mode == "bearer":
            h["Authorization"] = f"Bearer {self.key}"
        else:
            h["x-goog-api-key"] = self.key
        return h

    def _send(self, method: str, url: str, body: bytes | None, mode: str) -> HttpResp:
        resp = self.transport(method, url, self._headers(mode), body, self.timeout)
        if resp.status == 429 and "limit: 0" not in _err_message(resp):  # limit 0 = no quota for this model
            delay = 5.0
            try:
                for d in json.loads(resp.body).get("error", {}).get("details", []):
                    if str(d.get("@type", "")).endswith("RetryInfo"):
                        delay = min(60.0, float(str(d.get("retryDelay", "5s")).rstrip("s")))
            except (ValueError, AttributeError, TypeError):
                pass
            self.log.append(f"429 on {method} {url}; backing off {delay:.0f}s and retrying once")
            self.sleep(delay)
            resp = self.transport(method, url, self._headers(mode), body, self.timeout)
        return resp

    def request(self, method: str, url: str, body: bytes | None = None) -> HttpResp:
        modes = [self.auth_mode] if self.auth_mode else list(AUTH_MODES)
        resp = HttpResp(0, b"")
        for mode in modes:
            resp = self._send(method, url, body, mode)
            self.log.append(f"{method} {url} auth={mode} -> {resp.status}")
            if resp.status in (401, 403) and mode != modes[-1]:
                self.log.append(f"auth {mode} rejected ({scrub(_err_message(resp), self.key)}); trying next")
                continue
            if resp.status not in (401, 403):
                self.auth_mode = mode
            return resp
        return resp

    def list_generate_models(self) -> list[str]:
        out: list[str] = []
        url, token = f"{BASE}/models?pageSize=1000", None
        for _ in range(10):
            resp = self.request("GET", url + (f"&pageToken={token}" if token else ""))
            if resp.status != 200:
                raise GeminiError(resp.status, scrub(_err_message(resp), self.key))
            j = json.loads(resp.body)
            for m in j.get("models", []):
                if "generateContent" in (m.get("supportedGenerationMethods") or []):
                    out.append(str(m.get("name", "")).removeprefix("models/"))
            token = j.get("nextPageToken")
            if not token:
                break
        return out

    def generate(self, payload: dict, model: str | None = None) -> tuple[str, str, int]:
        """Return (reply_text, model_used, http_status)."""
        body = json.dumps(payload).encode()
        tried: list[str] = []
        candidates = [model] if model else list(DEFAULT_MODELS)
        listed = False
        last_transient: GeminiError | None = None
        while True:
            for m in candidates:
                if m in tried:
                    continue
                tried.append(m)
                resp = self.request("POST", f"{BASE}/models/{m}:generateContent", body)
                if resp.status == 404:
                    self.log.append(f"model {m} unavailable (404: {scrub(_err_message(resp), self.key)[:160]})")
                    continue
                if resp.status == 400 and "modalit" in _err_message(resp).lower():
                    self.log.append(f"model {m} cannot return text (400); skipping")
                    continue
                if resp.status in (429, 500, 502, 503, 504):  # quota is per model; overload is transient
                    last_transient = GeminiError(resp.status, scrub(_err_message(resp), self.key))
                    self.log.append(f"model {m} transient {resp.status}; trying next candidate")
                    continue
                if resp.status != 200:
                    raise GeminiError(resp.status, scrub(_err_message(resp), self.key))
                return extract_text(resp.body), m, resp.status
            if listed:
                if last_transient is not None:
                    raise last_transient
                raise GeminiError(404, f"no usable generateContent model (tried {tried})")
            listed = True
            listed_all = self.list_generate_models()
            candidates = [m for m in rank_listed(listed_all) if m not in tried][:8]
            self.log.append(f"listed {len(listed_all)} generateContent models; trying {candidates}")


def extract_text(body: bytes) -> str:
    j = json.loads(body)
    cands = j.get("candidates") or []
    if not cands:
        fb = j.get("promptFeedback", {})
        raise GeminiError(200, f"no candidates returned (promptFeedback={fb})")
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not text:
        raise GeminiError(200, f"empty reply (finishReason={cands[0].get('finishReason')})")
    return text


def write_output(out: Path, reply: str, model: str, auth: str, reports: list[str], prompt: str,
                 status: int, latency: float) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    hdr = ["# Gemini feedback", "",
           "> Advisory, untrusted model output - review before acting on anything below.", "",
           f"- timestamp_utc: {ts}", f"- model: {model}", f"- auth_mode: {auth}", f"- http_status: {status}",
           f"- latency_s: {latency:.2f}", f"- reports: {', '.join(reports) if reports else '(none)'}",
           f"- prompt: {prompt}", "", "---", ""]
    out.write_text("\n".join(hdr) + reply.rstrip() + "\n", encoding="utf-8")


def main(argv: list[str] | None = None, transport: Transport = urllib_transport,
         sleep: Callable[[float], None] = time.sleep) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", action="append", default=[], help="markdown report to attach (repeatable)")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--model", default=None)
    ap.add_argument("--out", default="dryrun/gemini_feedback_latest.md")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--env-file", default=None, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    key = load_key(Path(a.env_file) if a.env_file else None)
    if not key:
        print("ERROR: GEMINI_API_KEY not found in environment or .env", file=sys.stderr)
        return 2
    reports: list[tuple[str, str]] = []
    for r in a.report:
        p = Path(r) if Path(r).is_absolute() else (Path.cwd() / r if (Path.cwd() / r).exists() else ROOT / r)
        if not p.exists():
            print(f"ERROR: report not found: {r}", file=sys.stderr)
            return 2
        reports.append((r, read_report(p)))

    cli = Client(key, transport=transport, timeout=a.timeout, sleep=sleep)
    t0 = time.monotonic()
    try:
        reply, model, status = cli.generate(build_payload(a.prompt, reports), a.model)
    except GeminiError as e:
        for line in cli.log:
            print(f"[consult_gemini] {scrub(line, key)}", file=sys.stderr)
        print(f"ERROR: Gemini request failed: HTTP {e.status}: {scrub(e.message, key)} (key {mask(key)})",
              file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"ERROR: network failure: {scrub(str(e), key)}", file=sys.stderr)
        return 1
    latency = time.monotonic() - t0
    out = Path(a.out) if Path(a.out).is_absolute() else ROOT / a.out
    write_output(out, reply, model, cli.auth_mode or "?", [n for n, _ in reports], a.prompt, status, latency)
    for line in cli.log:
        print(f"[consult_gemini] {scrub(line, key)}", file=sys.stderr)
    print(f"[consult_gemini] OK status={status} model={model} auth={cli.auth_mode} latency={latency:.2f}s "
          f"key={mask(key)} saved={out}", file=sys.stderr)
    print(reply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
