#!/usr/bin/env python3
"""Run the main CLI from a source checkout (no install needed): scripts/live.py --live --rules v4 --cups"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from quantbet.main import main  # noqa: E402

if __name__ == "__main__":
    main()
