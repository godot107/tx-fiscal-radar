# Questions this answers, and what someone does with the answer

**Client framing.** A state agency's budget and government-relations office, or a central budget office such as the
Legislative Budget Board. Every session they decide which bills to track before session, where to send people to
testify during session, and which new mandates to fund and staff first after it. This project turns the Legislature's
own files into numbers they can check, on a dashboard and through an agent.

| # | Question | Answered from | Action it drives | Where |
| :--- | :--- | :--- | :--- | :--- |
| **A. Session pipeline** | | | | |
| A1 | How many bills were filed, referred, passed and enacted, and where do most stop? | Bill-history actions | Spend analyst time on bills that get a hearing, not on every filing | `dw.v_session_funnel`, page *Session pulse* |
| A2 | Which committees are bottlenecks? | Committee referral + status | Plan testimony and prepare for heavy hearing days | `dw.v_committee_bottleneck` |
| A3 | Is 2027 filing ahead of or behind 2025 at the same point? | Filing dates by days since prefiling opened | Early read on January workload and staffing | `dw.v_filing_pace` |
| **B. Fiscal exposure** | | | | |
| B1 | What do enacted bills cost in 2026-27, by agency? | Fiscal-note tables (+ AI split of multi-agency bills) | Implementation budget requests | `dw.v_agency_exposure`, page *Fiscal exposure* |
| B2 | Which bills cost little now but grow in 2028-30? | Out-years of the 5-year table | The next Legislative Appropriations Request | `dw.v_bill_cost.grows_later` |
| B3 | How many new state jobs (FTEs) do enacted bills create? | "Change in number of state employees" | Hiring plans before the effective dates | `fte_2027` |
| B4 | Which costs "cannot be determined"? | Fiscal-note text | A risk list beside the dollar figures | `cannot_be_determined` |
| **C. Estimate drift** | | | | |
| C1 | How much did each estimate change from introduced to enrolled? | Every fiscal-note version | Keep contingency for volatile bills | `dw.v_estimate_drift`, page *Estimate drift* |
| **D. Readiness** | | | | |
| D1 | Which agencies face the largest new cost relative to their size? | Attributed cost ÷ FY2024 spending | A ranked "prepare first" list | page *Readiness* |
| D2 | What has to be in place by each effective date? | Effective-date actions | Implementation calendar per agency | `dw.v_implementation_calendar` |
| D3 | Which bills create costs without appropriating money? | "would make no appropriation" | Flag possible unfunded mandates | `no_appropriation` |
| **E. Live watchlist (2027)** | | | | |
| E1 | Which new bills touch my agency, before any fiscal note exists? | Agency subject codes (V-codes) | Morning triage for government relations | `dw.v_watchlist`, page *Watchlist* |
| E2 | Which watched bills got a hearing or fiscal note since yesterday? | Daily manifest-driven loads | An alert instead of manual checking | same |

## The hard part: per-agency attribution

A fiscal note's summary tables give **one cost for the whole bill**. HB 2 (2025) lists nine agencies against one total.
The warehouse never spreads a shared cost evenly without saying so:

1. **Direct**: bills that name exactly one agency. The whole cost belongs to it. No assumption needed.
2. **AI-attributed**: for multi-agency bills, gpt-4.1-mini extracts agency-specific amounts from the methodology text
   ("TEA assumes IT costs of $3.5 million in fiscal year 2026"). A row counts only if its quote appears verbatim in the
   note, its agency is one of the note's Source Agencies, and the bill's extracted amounts add up to the note's own
   totals. Otherwise it waits on the *Trust & review* page.
3. **Shared**: multi-agency bills without a checked split are counted, never summed into an agency.

## Caveats shown on the dashboard

- Fiscal notes estimate one version of a bill; they are not appropriations.
- General Revenue (GR) figures and all-funds figures differ; each visual says which it uses.
- FY2024 spending predates these laws. It measures agency size, not the new budget.
