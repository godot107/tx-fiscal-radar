# What did Texas's 2025 laws cost, and who has to carry them?

*Part of a project to learn how the Texas Legislature works, from its own files, ahead of the 90th Legislature
(convenes January 12, 2027). The session-level picture (filing, committees, deadlines, vetoes) is in
[LEGISLATURE.md](LEGISLATURE.md); this report is about the money. Draft, 2026-11-10.*

Every bill in the Texas Legislature gets a **fiscal note**: the Legislative Budget Board's estimate of what it would
cost the state. The notes are public, thorough, and written one bill at a time. Nobody adds them up by agency. If you run
a state agency's budget office, the question after a session is simple to ask and slow to answer: *what did the
Legislature just hand us, and how big is it for us?*

I built a pipeline that answers it from the Legislature's own bulk files, and keeps answering it as the 2027 session's
bills arrive (prefiling opened on November 9, 2026).

## What it found for 2025

**Most bills don't become law.** Of 8,719 House and Senate bills filed in the 2025 regular session, 1,213 passed both
chambers (14%) and 1,187 were enacted; 26 were vetoed. The bottleneck is committees: House Public Education received
679 bills and 556 never came out.

**The laws that did pass carry about $16.8 billion of new spending in 2026-27**, across 114 bills with a cost, plus
$2.2 billion of state revenue given up through tax relief, and about 800 new state jobs. School finance (HB 2) is half
of the spending by itself. Appropriations bills, which move money to many agencies at once (HB 500, $13.2 billion),
are counted separately.

**Estimates move.** Of the enacted bills with more than one fiscal note, 49 changed by more than a quarter between the
first and last version. The homestead exemption increase (SB 4) went from $1.0 billion to $2.7 billion as it moved.

**Some costs arrive later.** A franchise-tax credit (SB 2206) costs nothing in 2026-27 and $1.8 billion in 2028-30.
That belongs in the next budget request, not this one.

**Relative to their size**, the agencies with the most new work include the Department of Information Resources
(new cost of about 10% of its yearly spending, 139 new positions), the Office of Court Administration (~9%) and the
Public Utility Commission (~8%). The Comptroller tops the list at 38%, but most of that is a quirk worth knowing
about, below.

## Why you shouldn't just add up the fiscal notes

Three things made this harder than summing a column, and each one is a decision the dashboard makes visible:

1. **A fiscal note gives one cost for the whole bill.** When a bill touches nine agencies, which one pays is written in
   the explanation, in prose. A language model (GPT-4.1-mini on Azure) reads that prose and pulls out each agency's
   amounts, but its output only counts when the sentence it quotes is really in the note, the number is really in the
   sentence, and the sentence names the agency. 48% of extracted rows pass all of that; the rest wait for a person.
   The model was most often caught assigning a cost to the wrong agency, which is why the agency check exists.
2. **"Source agencies" are the agencies the Budget Board asked, not always the ones who pay.** On tax bills that is
   often just the Comptroller, even when the cost lands on the school finance system. The pipeline flags tax-bill costs
   instead of quietly moving them.
3. **"Passed" means different things.** Counting a bill as passed when each chamber passed *a* version overstated the
   total by eight bills: their versions were never reconciled. The pipeline now matches the Legislature's own count
   exactly (1,213), and checks that every day.

## How it works

The Legislature publishes everything on a public FTP site. Azure Data Factory copies it into a data lake, using the
Legislature's own daily change list so it only re-downloads what changed. Spark (in Azure Synapse) turns the HTML and
XML into tables; Azure SQL holds a star schema the dashboard reads; a Foundry agent answers questions in plain
English, quoting the warehouse for numbers and the fiscal notes for explanations. Nothing uses a password or a key;
every service signs in with its own managed identity. The whole build cost a few dollars of cloud credit.

## Limits

Fiscal notes are estimates of a bill's text at one moment; they are not appropriations. Agency spending is FY2024,
before these laws, used only to measure agency size. The AI split of multi-agency bills is a lower bound where the
note's text doesn't itemise everything. This is an independent analysis of public records.
