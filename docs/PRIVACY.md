# Privacy and telemetry

The users (officials, lobbyists, advocacy groups, journalists) make *what they ask about* sensitive: a question can
reveal an investigation or a strategy. And on a state system, kept records can be requestable under the Texas Public
Information Act. So the design rule is **minimize by default, collect more only by explicit choice, disclose all of it**.
The user-facing notice is `app/static/privacy.html` (served at `/privacy`, linked from every page, shown once on first
visit). This page is the engineering view.

## Data inventory

| Data | Contains | Default | Retention | Control |
| :--- | :--- | :--- | :--- | :--- |
| Foundry agent thread (question + answer) | Full text | **Deleted right after the answer** | 0 | `ask(keep_thread=False)` in the web app |
| Same, user ticked "Help improve" | Full text | Opt-in | 30 days | Thread metadata `kept_until`; `tools/retention.py --delete` (daily) |
| `COST_EVENT`, `GUARD_EVENT`, `MCP_EVENT` (App Insights) | Pseudonymous id, session id, cost, timing, guardrail category, tool name. **No question text, no arguments** | Always | 30 days | Log Analytics retention |
| `FEEDBACK_EVENT` | Pseudonymous id, 👍/👎, comment **only if the user wrote one**, thread reference only if kept | User action | 30 days | |
| `PAIR_EVENT` / `PREF_EVENT` | Pseudonymous id, which variant was preferred | User action | 30 days | |
| Memory store | Work-context notes (agency, role, topics) | Opt-in ("Remember") | 30-day TTL | Store told to exclude personal data; `txfr.memory forget --scope` |
| Answer cache | Answer text, keyed by a hash of the question; no user | Always | ≤ 2 days | Storage lifecycle rule |
| Per-user spend counter | Pseudonymous id, date, dollars | Always | Rows per day (could add a cleanup rule) | |
| SQL audit | The app identity's fixed queries | Always | 30 days | |
| Evaluation runs (CLI) | Synthetic test questions, not users | Always | Until deleted | `tools/retention.py --include-eval-older-than 30` |

**Pseudonymous id** = `sha256(salt : Entra object id)[:16]`. Per-person counts and caps still work; reading the logs
doesn't reveal who asked. The salt is an app setting derived at deploy time (dev); production keeps it in Key Vault and
rotates it yearly (old pseudonyms then stop linking, which is a feature).

## Sentiment: explicit only

| Do | Don't |
| :--- | :--- |
| 👍/👎 on each answer, optional comment | Infer sentiment, stance or political leaning from question text |
| Aggregate which topics and tools are used, from the request screen's category and tool names | Report on groups smaller than 5 distinct users (re-identification) |
| Review kept (opted-in) threads to fix wrong answers | Use questions to train models |

```kusto
// Answer quality by tool path, from explicit feedback (aggregate only)
AppTraces | where Message startswith "FEEDBACK_EVENT" | extend f = parse_json(substring(Message, 15))
| summarize up = countif(tostring(f.rating) == "up"), down = countif(tostring(f.rating) == "down"),
            users = dcount(tostring(f.user)) by bin(TimeGenerated, 7d)
| where users >= 5

// What people ask about, by screen category, never below 5 distinct users per group
AppTraces | where Message startswith "GUARD_EVENT" or Message startswith "COST_EVENT"
| extend e = parse_json(substring(Message, indexof(Message, "{")))
| summarize questions = count(), users = dcount(tostring(e.user)) by status = tostring(e.status), week = bin(TimeGenerated, 7d)
| where users >= 5
```

## Opt-outs

- Don't tick anything: nothing about the question is kept; only the pseudonymous usage record remains for 30 days.
- "Remember" can be switched off any time; memories expire after 30 days (an operator can delete a user's memory
  immediately with `txfr.memory forget`).
- Pseudonymous usage records can't be opted out of, because they enforce the per-user cost cap and abuse limits;
  the notice says so.

## Production additions

A named data controller and contact, a records-retention schedule approved by the agency's records officer (Texas
State Library and Archives Commission schedules), a data-protection impact assessment, the salt in Key Vault with
rotation, and a subject-access/deletion process for kept threads and memories.
