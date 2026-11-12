"""Sum Data Factory billable usage for a time window from activity-run billing records (read-only).

    python tools/adf_usage.py 2026-11-10T00:00:00Z
Prints activity runs and billable hours per meter (AzureIR data movement = DIU-hours; pipeline/external activity hours).
"""
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from txfr.common import load_env  # noqa: E402


def rest(method, url, body=None):
    cmd = ["az", "rest", "--method", method, "--url", url, "-o", "json"]
    if body:
        cmd += ["--body", json.dumps(body)]
    return json.loads(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout)


def main():
    load_env()
    start = sys.argv[1] if len(sys.argv) > 1 else "2026-11-10T00:00:00Z"
    end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    api = f"https://management.azure.com{os.environ['DATA_FACTORY_ID']}"
    window = {"lastUpdatedAfter": start, "lastUpdatedBefore": end}
    runs, token = [], None
    while True:
        body = dict(window, **({"continuationToken": token} if token else {}))
        r = rest("post", f"{api}/queryPipelineRuns?api-version=2018-06-01", body)
        runs += r["value"]
        token = r.get("continuationToken")
        if not token:
            break
    meters, kinds = defaultdict(float), Counter()
    for pr in runs:
        acts = rest("post", f"{api}/pipelineruns/{pr['runId']}/queryActivityruns?api-version=2018-06-01", window)["value"]
        for a in acts:
            kinds[a["activityType"]] += 1
            for b in ((a.get("output") or {}).get("billingReference") or {}).get("billableDuration", []) or []:
                meters[f"{b.get('meterType')} ({b.get('unit')})"] += b.get("duration", 0)
    print(f"pipeline runs: {len(runs)}; activity runs: {sum(kinds.values())}")
    for k, v in kinds.most_common():
        print(f"  {k}: {v}")
    for m, v in sorted(meters.items()):
        print(f"  billable {m}: {v:.3f}")


if __name__ == "__main__":
    main()
