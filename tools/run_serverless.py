"""Run a .sql file on the Synapse serverless SQL endpoint (Entra auth via az login).

Batches are split on GO. A '-- @database <db>' line switches the connection's database for the batches after it.
A '-- @cetas <container/path>' line empties that lake folder before the next batch (CETAS needs an empty target).
    python tools/run_serverless.py synapse/sql/serverless.sql
    python tools/run_serverless.py --query "SELECT * FROM gold.v_session_funnel"
"""
import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from txfr.common import lake_client, load_env  # noqa: E402


def connect(db):
    import mssql_python
    return mssql_python.connect(f"Server=tcp:{os.environ['SYNAPSE_SERVERLESS_SQL']},1433;Database={db};Encrypt=yes;"
                                "TrustServerCertificate=no;Authentication=ActiveDirectoryDefault", autocommit=True)


def empty_folder(path):
    container, _, folder = path.partition("/")
    fs = lake_client().get_file_system_client(container)
    d = fs.get_directory_client(folder)
    if d.exists():
        d.delete_directory()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--query")
    ap.add_argument("--database", default="txfr_lake")
    a = ap.parse_args()
    load_env()
    for f in a.files:
        text = re.sub(r"\$\{(\w+)\}", lambda m: os.environ[m.group(1)], Path(f).read_text())
        db, conn, n = "master", None, 0
        for batch in re.split(r"^\s*GO\s*$", text, flags=re.M | re.I):
            m = re.search(r"^--\s*@database\s+(\w+)", batch, re.M)
            if m and m.group(1) != db or conn is None:
                db = m.group(1) if m else db
                conn = connect(db)
            c = re.search(r"^--\s*@cetas\s+(\S+)", batch, re.M)
            if c:
                empty_folder(c.group(1))
            body = "\n".join(l for l in batch.splitlines() if not l.strip().startswith("-- @"))
            if body.strip() and not all(l.strip().startswith("--") or not l.strip() for l in body.splitlines()):
                conn.cursor().execute(body)
                n += 1
        print(f"{f}: {n} batches OK")
    if a.query:
        cur = connect(a.database).cursor()
        cur.execute(a.query)
        while cur.description is None and cur.nextset():   # skip informational/row-count results
            pass
        print("\t".join(d[0] for d in cur.description))
        for r in cur.fetchall():
            print("\t".join("" if v is None else str(v) for v in r))


if __name__ == "__main__":
    main()
