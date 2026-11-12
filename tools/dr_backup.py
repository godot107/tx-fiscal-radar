"""DR: copy what can't be cheaply re-created to the geo-redundant lake, and restore it.

    .venv/bin/python tools/dr_backup.py push      # data/ai_cache/*.json → lake backup/ai_cache/ (after txfr.ai_extract runs)
    .venv/bin/python tools/dr_backup.py pull      # lake → data/ai_cache/ (new machine, or after losing the laptop)
    .venv/bin/python tools/dr_backup.py status    # counts on both sides + the lake's geo-replication lag (RPO)

Why the AI cache: re-running extraction costs ~$0.24, but model output isn't deterministic, so a re-run would change
which rows pass the four checks and every number downstream of them. The cache is the record of what the model said.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
LOCAL = ROOT / "data" / "ai_cache"


def main():
    from txfr.common import lake_client, load_env
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["push", "pull", "status"])
    a = ap.parse_args()
    fs = lake_client().get_file_system_client("backup")
    remote = {p.name.split("/")[-1] for p in fs.get_paths(path="ai_cache")} if fs.get_directory_client("ai_cache").exists() else set()
    local = {p.name for p in LOCAL.glob("*.json")} if LOCAL.exists() else set()
    if a.cmd == "push":
        for n in sorted(local - remote):
            fs.get_file_client(f"ai_cache/{n}").upload_data((LOCAL / n).read_bytes(), overwrite=True)
        print(f"pushed {len(local - remote)} new files ({len(local)} local, {len(remote | local)} in the lake)")
    elif a.cmd == "pull":
        LOCAL.mkdir(parents=True, exist_ok=True)
        for n in sorted(remote - local):
            (LOCAL / n).write_bytes(fs.get_file_client(f"ai_cache/{n}").download_file().readall())
        print(f"pulled {len(remote - local)} files ({len(remote)} in the lake)")
    else:
        import json
        import os
        import subprocess
        st = subprocess.run(["az", "storage", "account", "show", "-n", os.environ["LAKE_ACCOUNT"], "--expand",
                             "geoReplicationStats", "--query", "{sku:sku.name,secondary:secondaryLocation,"
                             "status:geoReplicationStats.status,lastSync:geoReplicationStats.lastSyncTime}", "-o", "json"],
                            capture_output=True, text=True)
        print(json.dumps({"local_files": len(local), "lake_files": len(remote), "only_local": len(local - remote),
                          "geo": json.loads(st.stdout or "{}")}, indent=2))


if __name__ == "__main__":
    main()
