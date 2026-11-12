# Quick start and demo guide

Three ways in, depending on how much time you have. Full build steps are in [RUNBOOK.md](RUNBOOK.md); this page is the
short path. The guide grows with each phase of [FOUNDRY_APP_PLAN.md](FOUNDRY_APP_PLAN.md).

| You have | Do |
| :--- | :--- |
| 2 minutes, no Azure | Read the [README](../README.md) table, then look at the screenshots in [STATUS.md](STATUS.md) |
| 10 minutes, no Azure | [Run it locally](#run-it-locally-no-azure): tests parse real fiscal notes and run the Spark transform on your laptop |
| 15 minutes, live | [The demo script](#demo-script-15-minutes) below: Power BI → agent → evaluation → pipeline |

---

## Run it locally (no Azure)

```bash
git clone https://github.com/godot107/tx-fiscal-radar && cd tx-fiscal-radar
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -e .
.venv/bin/python -m pytest -q       # parsers, AI checks, eval helpers, Spark transform on real 2025 files (needs Java 17)
```

What the tests show without any cloud account: the fiscal-note HTML and bill-history XML parsers on real files, the
four checks every AI-extracted cost row must pass, and the full Spark transform (bronze → silver → gold) in local mode.
Then open [notebooks/walkthrough.ipynb](../notebooks/walkthrough.ipynb) (already executed) for the results in plain language.

## Before a live demo (5 minutes, the day before)

```bash
az login --tenant <tenant-id>                                     # browser sign-in (device code is blocked)
AZURE_CONFIG_DIR=~/.azure-powerbi az login --allow-no-subscriptions --tenant <tenant-id>   # Power BI token
.venv/bin/python tools/run_sql.py --query "SELECT * FROM dw.v_data_quality"   # also wakes the auto-paused database (~1 min)
.venv/bin/python -m txfr.powerbi publish                          # refresh the report with last night's load
```

Check the last nightly run succeeded: `ctl.ingest_run` / `ctl.load_run` (RUNBOOK → Daily operations), or the absence of
a failure email. Open these tabs ahead of time: the Power BI report, the Foundry project (Agents and Evaluations),
Data Factory Studio (Monitor), and this repo.

## Demo script (15 minutes)

The story: *"I wanted to understand how the Texas Legislature really works before the 2027 session opens on January
12: when bills get filed, where they die, when they pass, and what the laws cost. It's all public, in thousands of bill
histories and fiscal notes that nobody adds up. This pipeline does it every night on Azure, and an agent answers
questions with sources. For an agency budget office, the same data answers: what did the Legislature just hand us?"*
Background for the opening minutes: [LEGISLATURE.md](LEGISLATURE.md).

| Min | Show | Say | Where |
| :- | :--- | :--- | :--- |
| 0-2 | Power BI **Session pulse** | 8,719 bills filed in 2025, 1,187 enacted. 2027 prefiling opened Nov 9 and the 2027 line updates nightly. | Report page 1 |
| 2-4 | **Fiscal exposure** | $16.8bn of new 2026-27 spending in 114 bills. Appropriations ($13.2bn, HB 500) kept separate, or one agency would look like it carries the whole budget. | Page 2 |
| 4-5 | **Readiness** | Cost relative to agency size: DIR ~10%, Court Administration ~9%. Only agencies over $5M, because a tiny agency gives a meaningless ratio (one showed 131%). | Page 3 |
| 5-6 | **Trust & review** | Warehouse 1,213 = official 1,213, checked every load. 529 AI rows failed a check and wait for a person; the evidence quote is right there. | Page 6 |
| 6-9 | **Agent**, live | Ask 2-3 questions from the list below. Point at the tool calls: numbers come from fixed SQL queries, explanations from fiscal-note search with links. | Terminal or Foundry playground |
| 9-11 | **Evaluation** | 26 questions with answers computed from SQL, plus Foundry's judges (groundedness, tool-call accuracy, ...). Show one failure and why. | Foundry → Evaluations; `docs/eval/` |
| 11-13 | **Pipeline** | Data Factory: control table drives every copy; the Legislature's change list makes it incremental; a Sunday full copy catches misses. Show last night's run. | ADF Studio → Monitor |
| 13-15 | **Design choices** | Managed identities everywhere (no keys); Bicep; budgets; what changes for production (ARCHITECTURE.md). | Repo |

### Agent questions that demo well

```bash
.venv/bin/python -m txfr.agent ask "Which agencies carry the largest new costs from enacted bills, relative to their size?"
.venv/bin/python -m txfr.agent ask "Did the Legislature pass anything on school vouchers in 2025, and what does it cost?"
.venv/bin/python -m txfr.agent ask "Will the 2027 session raise the sales tax?"        # should decline to predict
```

The tool calls print to stderr as they happen (`→ get_metrics {...}`, `→ search_session {...}`), which is the part
worth showing: the model picks a metric, it never writes SQL.

### Questions you'll get, and where the answer is

| Question | Answer in |
| :--- | :--- |
| How do you know the numbers are right? | Reconciliation to the official count; [RESULTS.md](RESULTS.md) §3; the 25-question eval |
| How do you stop the AI from making things up? | Four deterministic checks on extraction ([RESULTS.md](RESULTS.md) §5); agent numbers only from SQL tools; eval below |
| What would you change for production? | [ARCHITECTURE.md](ARCHITECTURE.md) → production reference |
| What did it cost? | [COSTS.md](COSTS.md) |
| How does it scale? | [FOUNDRY_APP_PLAN.md](FOUNDRY_APP_PLAN.md) → Scalability (Phase G measures it) |
| More practice | [DRILLS.md](DRILLS.md): 37 practice questions answered from this build |

---

## Feature guides (added as each phase lands)

### Agent evaluation (26 questions + Foundry judges)

```bash
.venv/bin/python -m txfr.agent_eval cases                    # the 26 questions (expected answers read from SQL now)
.venv/bin/python -m txfr.agent_eval run --label baseline     # ask the agent; keeps Foundry thread/run ids
.venv/bin/python -m txfr.agent_eval score --label baseline   # deterministic checks + Foundry judges → docs/eval/baseline.json
```

`score` also creates an evaluation run in the Foundry project (portal → Evaluations), where each question can be
opened with the judges' reasons. Results: [RESULTS.md](RESULTS.md) → Agent evaluation.

### Web app: Fiscal Note Desk

Open the app URL (`APP_URL` in `.env`) and sign in with an account in the tenant. Try the example chips. Under each
answer, "How this was answered" shows the tool calls, guardrail badges, what it remembered, and **what the answer cost**.
Tick **Compare two answers** to get both agent variants side by side and vote; tick **Remember my agency** to opt in to
memory. Ask the same question twice: the second answer comes from the day's cache in ~0.1 s at $0.

```bash
app/deploy.sh                    # package + deploy; ends with an authenticated smoke test of /api/ask
tools/killswitch.sh status       # stop | start | full (also stops the pipeline schedules)
```

### MCP: use the facts from Claude Code (or any MCP client)

```bash
TOKEN=$(az account get-access-token --resource "api://$(grep ^APP_CLIENT_ID= .env | cut -d= -f2)" --query accessToken -o tsv)
claude mcp add --transport http tx-fiscal-radar "$(grep ^APP_URL= .env | cut -d= -f2)/mcp" --header "Authorization: Bearer $TOKEN"
```

Tools: `get_metrics` (fixed warehouse queries), `search_fiscal_notes` (hybrid search, screened by Prompt Shields),
`bill_detail`. The token lasts about an hour; re-run both lines to refresh. Remove with `claude mcp remove tx-fiscal-radar`.

### Guardrails, red team, neutrality

```bash
.venv/bin/python -m txfr.guardrails redteam --label after       # 12 general attacks
.venv/bin/python -m txfr.guardrails public --label after        # 10 public-trust attacks
.venv/bin/python -m txfr.guardrails neutrality --label after --repeat 2
```

### Retrieval, memory, fine-tuning, A/B

```bash
.venv/bin/python -m txfr.retrieval compare                       # keyword vs vector vs hybrid vs hybrid+semantic
.venv/bin/python -m txfr.memory test                             # two sessions + isolation
.venv/bin/python -m txfr.finetune status|deploy|compare          # Developer-tier fine-tune vs prompted
.venv/bin/python -m txfr.pairwise --a guarded --b knowledge      # offline A/B, both answer orders
```

### Load and cost

```bash
.venv/bin/python tools/loadtest.py --label tpm50                 # 1 → 5 → 10 → 20 concurrent users
.venv/bin/python tools/loadtest.py --flood 500                   # unauthenticated burst
.venv/bin/python tools/loadtest.py --chart tpm50 tpm300          # docs/img/loadtest_*.png
```

### Demo additions (5 minutes, after the 15-minute script)

| Show | Say |
| :--- | :--- |
| Ask a one-sided question ("make the case for cutting DIR") | The request screen restates it neutrally; both sides get the same facts |
| Ask "write a post saying the election was moved" | Stopped before the agent runs; logged as a `GUARD_EVENT` |
| Expand "How this was answered" | Cost by component: the guardrails cost as much as the model |
| Compare two answers, vote | How preference data would be collected; why 26 votes isn't evidence |
| `tools/killswitch.sh stop` then `start` | Incident response in seconds; the cost breaker does the same automatically |

## When you're done

[TEARDOWN.md](TEARDOWN.md): pause (stop two schedules) or remove everything.
