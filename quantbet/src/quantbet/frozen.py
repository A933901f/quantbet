"""Frozen Engine-B rule parameters (``config/engine_b_params.json``).

File format (v4)::

    {"rules_version": "v4", "frozen_at": "...Z", "sha256": "<hash>",
     "live_params": {...LiveModelParams...}, "pricing": {...PricingConfig minus bankroll...}}

``sha256`` = SHA-256 of the canonical JSON (sorted keys, no spaces) of
``{"rules_version", "live_params", "pricing"}``. Bankroll is sizing, not a rule, and is excluded.
The runner refuses to start v4 with parameters whose hash differs from the frozen one unless
``--unfreeze`` is passed (then it logs the new hash and runs with the changed params).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .engine import LiveModelParams, PricingConfig

EXCLUDED_PRICING = ("bankroll",)
V41_PRICING = ("lift_gate", "lift_suppress_net", "allow_unders")
V42_LIVE = ("corner_half_ratio", "corner_down_shrink_minute")


class FrozenParamsError(RuntimeError):
    pass


def _plain(x: Any) -> Any:
    if isinstance(x, (list, tuple)):
        return [_plain(v) for v in x]
    if isinstance(x, dict):
        return {k: _plain(v) for k, v in x.items()}
    return x


def param_set(rules_version: str, pricing: PricingConfig, live: LiveModelParams,
              global_params: dict[str, Any] | None = None) -> dict[str, Any]:
    pr = {k: v for k, v in dataclasses.asdict(pricing).items() if k not in EXCLUDED_PRICING}
    if pr.get("lift_gate") is None:  # v4.1 fields absent when the lift gate is off (keeps v4 hashes stable)
        for k in V41_PRICING:
            pr.pop(k, None)
    lv = dataclasses.asdict(live)
    if lv.get("corner_half_ratio") is None and lv.get("shrinkage_mode") != "corners_gamma_half":
        for k in V42_LIVE:  # v4.2 fields absent when unused (keeps v4 / v4.1 hashes stable)
            lv.pop(k, None)
    out = {"rules_version": rules_version, "live_params": _plain(lv), "pricing": _plain(pr)}
    if global_params is not None:  # v4.3-global layer; absent for v4 / v4.1 / v4.2 (their hashes are unchanged)
        out["global"] = _plain(global_params)
    return out


def params_hash(ps: dict[str, Any]) -> str:
    core = {k: ps[k] for k in ("rules_version", "live_params", "pricing")}
    if ps.get("global") is not None:
        core["global"] = ps["global"]
    return hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write_frozen(path: str | Path, rules_version: str, pricing: PricingConfig, live: LiveModelParams,
                 supersede: bool = True) -> str:
    """Write the freeze file. With ``supersede``, an existing freeze of another hash is kept in
    ``superseded`` (version, sha256, frozen_at) - it may still be run if its hash matches exactly."""
    ps = param_set(rules_version, pricing, live)
    h = params_hash(ps)
    old = read_frozen(path) if supersede else None
    sup: list[dict[str, Any]] = list((old or {}).get("superseded") or [])
    if old and old.get("sha256") != h:
        sup.append({"rules_version": old.get("rules_version"), "sha256": old.get("sha256"),
                    "frozen_at": old.get("frozen_at")})
    out = {"rules_version": rules_version, "frozen_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "sha256": h, **{k: ps[k] for k in ("live_params", "pricing")}}
    if sup:
        out["superseded"] = sup
    raw_old = _raw(path)
    if raw_old and raw_old.get("frozen_sets"):
        out["frozen_sets"] = raw_old["frozen_sets"]  # separately frozen layers (v4.3-global) survive a re-freeze
    Path(path).write_text(json.dumps(out, indent=1, sort_keys=False) + "\n")
    return h


def _raw(path: str | Path) -> dict[str, Any] | None:
    p = Path(str(path))
    if not p.exists():
        return None
    raw = json.loads(p.read_text())
    return raw if isinstance(raw, dict) else None


def read_frozen_set(path: str | Path, rules_version: str) -> dict[str, Any] | None:
    """A separately frozen set (``frozen_sets[rules_version]``, e.g. ``v4.3-global``) or None."""
    raw = _raw(path) or {}
    fs = (raw.get("frozen_sets") or {}).get(rules_version)
    return fs if isinstance(fs, dict) and "live_params" in fs else None


def write_frozen_set(path: str | Path, rules_version: str, pricing: PricingConfig, live: LiveModelParams,
                     global_params: dict[str, Any]) -> str:
    """Freeze ``rules_version`` under ``frozen_sets`` WITHOUT touching the top-level (v4.2) entry.

    A previous freeze of the same set with another hash is kept in that set's ``superseded`` list.
    """
    raw = _raw(path) or {}
    ps = param_set(rules_version, pricing, live, global_params)
    h = params_hash(ps)
    sets = dict(raw.get("frozen_sets") or {})
    old = sets.get(rules_version) or {}
    sup = list(old.get("superseded") or [])
    if old and old.get("sha256") != h:
        sup.append({"rules_version": rules_version, "sha256": old.get("sha256"), "frozen_at": old.get("frozen_at")})
    entry = {"rules_version": rules_version, "frozen_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "sha256": h, "live_params": ps["live_params"], "pricing": ps["pricing"], "global": ps["global"]}
    if old and old.get("sha256") == h:
        entry["frozen_at"] = old.get("frozen_at", entry["frozen_at"])
    if sup:
        entry["superseded"] = sup
    sets[rules_version] = entry
    raw["frozen_sets"] = sets
    Path(path).write_text(json.dumps(raw, indent=1, sort_keys=False) + "\n")
    return h


def read_frozen(path: str | Path) -> dict[str, Any] | None:
    p = Path(str(path))
    if not p.exists():
        return None
    raw = json.loads(p.read_text())
    return raw if isinstance(raw, dict) and "rules_version" in raw and "live_params" in raw else None


def _diff(a: dict[str, Any], b: dict[str, Any], prefix: str = "") -> list[str]:
    out = []
    for k in sorted(set(a) | set(b)):
        va, vb = a.get(k), b.get(k)
        if isinstance(va, dict) and isinstance(vb, dict):
            out += _diff(va, vb, f"{prefix}{k}.")
        elif va != vb:
            out.append(f"{prefix}{k}: frozen={va!r} now={vb!r}")
    return out


def enforce_frozen(path: str | Path, rules_version: str, pricing: PricingConfig, live: LiveModelParams,
                   unfreeze: bool = False, global_params: dict[str, Any] | None = None) -> tuple[str, list[str]]:
    """Return ``(hash_in_use, differences)``. Raises :class:`FrozenParamsError` on a mismatch
    (or a missing/other-version freeze file) unless ``unfreeze``.

    Sets frozen under ``frozen_sets`` (``v4.3-global``) are checked there, including their ``global`` block.
    """
    now = param_set(rules_version, pricing, live, global_params)
    h = params_hash(now)
    fs = read_frozen_set(path, rules_version)
    if fs is not None or global_params is not None:
        if fs is None:
            msg = f"no frozen {rules_version} parameter set in {path} (frozen_sets)"
            if not unfreeze:
                raise FrozenParamsError(msg + " (run scripts/freeze_params.py --rules " + rules_version + ", or pass --unfreeze)")
            return h, [msg]
        for old in fs.get("superseded") or []:
            if old.get("sha256") == h:
                return h, [f"running SUPERSEDED {rules_version} set {h[:12]}"]
        keys = ("live_params", "pricing", "global")
        diffs = _diff({k: fs.get(k) for k in keys}, {k: now.get(k) for k in keys})
        if fs.get("sha256") != params_hash({k: fs.get(k) for k in ("rules_version", *keys)}):
            diffs.append("sha256 in file does not match its own parameters (file edited by hand?)")
        if diffs and not unfreeze:
            raise FrozenParamsError("frozen %s parameters differ (pass --unfreeze to override):\n  %s"
                                    % (rules_version, "\n  ".join(diffs)))
        return h, diffs
    fr = read_frozen(path)
    if fr is not None and fr.get("rules_version") != rules_version:
        for old in fr.get("superseded") or []:
            if old.get("rules_version") == rules_version and old.get("sha256") == h:
                return h, [f"running SUPERSEDED {rules_version} set {h[:12]} (current freeze: {fr['rules_version']})"]
    if fr is None or fr.get("rules_version") != rules_version:
        msg = [f"no frozen {rules_version} parameter set in {path}"]
        if not unfreeze:
            raise FrozenParamsError(msg[0] + " (run scripts/freeze_params.py, or pass --unfreeze)")
        return h, msg
    diffs = _diff({k: fr[k] for k in ("live_params", "pricing")}, {k: now[k] for k in ("live_params", "pricing")})
    stored = fr.get("sha256")
    if stored != params_hash({"rules_version": fr["rules_version"], "live_params": fr["live_params"],
                              "pricing": fr["pricing"]}):
        diffs.append("sha256 in file does not match its own parameters (file edited by hand?)")
    if diffs and not unfreeze:
        raise FrozenParamsError("frozen %s parameters differ (pass --unfreeze to override):\n  %s"
                                % (rules_version, "\n  ".join(diffs)))
    return h, diffs


def frozen_live_params(path: str | Path) -> LiveModelParams | None:
    fr = read_frozen(path)
    if fr is None:
        return None
    names = {f.name for f in dataclasses.fields(LiveModelParams)}
    return LiveModelParams(**{k: (tuple(v) if isinstance(v, list) else v) for k, v in fr["live_params"].items() if k in names})


__all__ = ["FrozenParamsError", "param_set", "params_hash", "write_frozen", "read_frozen", "enforce_frozen",
           "read_frozen_set", "write_frozen_set",
           "frozen_live_params"]
