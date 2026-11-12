"""Privacy retention: delete Foundry agent threads the web app kept past their date (docs/PRIVACY.md).

Web-app threads carry metadata {"app": "fiscal-note-desk", "kept_until": "YYYY-MM-DD" | "delete"}: "delete" means the
app's own post-answer delete failed; a date means the user opted in to "help improve" (30 days). Untagged threads
(evaluation runs from the CLI) are left alone unless include_eval_older_than_days is given.
Runs nightly inside the Function App (`maintenance` timer) and by hand via tools/retention.py.
"""
from __future__ import annotations

import time


def run(delete: bool = True, include_eval_older_than_days: int | None = None) -> dict:
    from txfr.agent import agents_client
    cli, today = agents_client(), time.strftime("%Y-%m-%d", time.gmtime())
    due, kept, seen = [], 0, 0
    for t in cli.threads.list():
        seen += 1
        md = t.metadata or {}
        created = t.created_at.timestamp() if hasattr(t.created_at, "timestamp") else float(t.created_at or 0)
        if md.get("app") == "fiscal-note-desk":
            if md.get("kept_until") == "delete" or md.get("kept_until", "9999") < today:
                due.append(t.id)
            else:
                kept += 1
        elif include_eval_older_than_days and time.time() - created > include_eval_older_than_days * 86400:
            due.append(t.id)
    deleted = 0
    for tid in due if delete else []:
        try:
            cli.threads.delete(tid)
            deleted += 1
        except Exception:  # noqa: BLE001 - next night retries
            pass
    return {"seen": seen, "kept_within_retention": kept, "due": len(due), "deleted": deleted}
