#!/usr/bin/env python3
"""Freeze the v4.x rule parameters (v4.3-global: separate frozen_sets entry incl. the global layer) to config/engine_b_params.json (rules_version, frozen_at, sha256).

Calibrated model fields come from the current file (flat calibrate.py output or an earlier freeze);
v4 rule fields come from quantbet.engine.rules_preset(--rules, default v4.1); pricing knobs from Settings/.env.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from quantbet.config import Settings  # noqa: E402
from quantbet.frozen import write_frozen  # noqa: E402
from quantbet.main import load_live_params  # noqa: E402
from quantbet.engine import rules_preset  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--path", default=None)
ap.add_argument("--rules", default="v4.2", choices=("v4", "v4.1", "v4.2", "v4.3-global"))
a = ap.parse_args()
s = Settings()
path = a.path or s.engine_b_params_path
kw = dict(target_edge=s.target_edge, abort_edge=s.abort_edge, kelly_multiplier=s.kelly_multiplier,
          min_minute=s.min_alert_minute, max_minute=s.max_alert_minute, realert_cooldown_s=s.realert_cooldown_s,
          bankroll=s.bankroll, max_match_stake_frac=s.max_match_stake_frac, family_cooldown_min=s.family_cooldown_min)
pricing, live = rules_preset(a.rules, load_live_params(path), **kw)
if a.rules.startswith("v4.3"):
    # Separate entry under "frozen_sets": the top-level v4.2 freeze is left byte-for-byte as it is.
    from quantbet.frozen import write_frozen_set  # noqa: E402
    from quantbet.main import global_params_dict  # noqa: E402

    print(write_frozen_set(path, a.rules, pricing, live, global_params_dict(s)))
else:
    print(write_frozen(path, a.rules, pricing, live))
