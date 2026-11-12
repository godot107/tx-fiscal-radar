"""Run .sql files against the project's Azure SQL database (Entra auth via az login).

    python tools/run_sql.py sql/01_control.sql sql/02_serving.sql
    python tools/run_sql.py --query "SELECT * FROM ctl.ingest_run"

Files are split on GO lines (like sqlcmd). ${VAR} placeholders are filled from .env / the environment.
"""
import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from txfr.common import connect_sql, load_env  # noqa: E402


def batches(text: str):
    for b in re.split(r"^\s*GO\s*$", text, flags=re.M | re.I):
        if b.strip():
            yield b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--query")
    a = ap.parse_args()
    load_env()
    conn = connect_sql()
    cur = conn.cursor()
    for f in a.files:
        text = re.sub(r"\$\{(\w+)\}", lambda m: os.environ[m.group(1)], Path(f).read_text())
        for i, b in enumerate(batches(text)):
            cur.execute(b)
            conn.commit()
        print(f"{f}: {i + 1} batches OK")
    if a.query:
        cur.execute(a.query)
        if cur.description:
            cols = [d[0] for d in cur.description]
            print("\t".join(cols))
            for row in cur.fetchall():
                print("\t".join("" if v is None else str(v) for v in row))
        conn.commit()


if __name__ == "__main__":
    main()
