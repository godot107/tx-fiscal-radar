"""Month-to-date cost of this project's resource group, by service and by resource (Cost Management API, read-only).

    python tools/cost_report.py            # prints a markdown table (paste into docs/COSTS.md)
Cost data lags usage by several hours; free-trial credit use is reported at list price.
"""
import json
import subprocess
import sys
from datetime import date


def az(*a):
    r = subprocess.run(["az", *a, "-o", "json"], capture_output=True, text=True)
    if r.returncode:
        sys.exit(r.stderr.strip()[-400:])
    return json.loads(r.stdout or "null")


def query(sub, rg, start, end, dim):
    body = {"type": "ActualCost", "timeframe": "Custom", "timePeriod": {"from": start, "to": end},
            "dataset": {"granularity": "None", "aggregation": {"c": {"name": "Cost", "function": "Sum"}},
                        "grouping": [{"type": "Dimension", "name": dim}]}}
    url = (f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/"
           "Microsoft.CostManagement/query?api-version=2023-11-01")
    return az("rest", "--method", "post", "--url", url, "--body", json.dumps(body))["properties"]["rows"]


def main():
    sub = az("account", "show")["id"]
    rg = "rg-tx-fiscal-radar-dev"
    start, end = date.today().replace(day=1).isoformat(), date.today().isoformat()
    total = 0.0
    print(f"Month to date {start} → {end}, resource group `{rg}` (ActualCost, USD)\n")
    print("| Service | Cost |\n| :--- | ---: |")
    for cost, svc, cur in sorted(query(sub, rg, start, end, "ServiceName"), key=lambda r: -r[0]):
        total += cost
        print(f"| {svc} | ${cost:,.2f} |")
    print(f"| **Total** | **${total:,.2f}** |\n")
    print("| Resource | Cost |\n| :--- | ---: |")
    for cost, rid, cur in sorted(query(sub, rg, start, end, "ResourceId"), key=lambda r: -r[0])[:12]:
        print(f"| {rid.split('/')[-1]} ({rid.split('/')[-2]}) | ${cost:,.2f} |")


if __name__ == "__main__":
    main()
