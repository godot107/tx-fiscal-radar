"""Privacy retention job, by hand: .venv/bin/python tools/retention.py [--delete] [--include-eval-older-than DAYS].
The same job runs nightly in the Function App (src/txfr/retention.py, `maintenance` timer)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main():
    from txfr.common import load_env
    from txfr.retention import run
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--include-eval-older-than", type=int)
    a = ap.parse_args()
    print(json.dumps(run(a.delete, a.include_eval_older_than), indent=2))


if __name__ == "__main__":
    main()
