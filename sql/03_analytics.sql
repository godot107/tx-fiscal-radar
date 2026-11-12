-- Analytics views over the star schema: what the Power BI pages and the agent's fact cards read.
-- Conventions: costs are POSITIVE in these views (fiscal notes print costs as negative "impact").
-- "Enacted" = signed, filed without signature, or effective without veto (see txfr.bill_history).

-- Session funnel: how far bills get (one row per session and bill type)
CREATE OR ALTER VIEW dw.v_session_funnel AS
SELECT session_code, bill_type,
       COUNT(*) AS filed,
       SUM(CASE WHEN referred_date IS NOT NULL THEN 1 ELSE 0 END) AS referred,
       SUM(CASE WHEN passed_house_date IS NOT NULL OR passed_senate_date IS NOT NULL THEN 1 ELSE 0 END) AS passed_one_chamber,
       SUM(CASE WHEN enrolled_date IS NOT NULL THEN 1 ELSE 0 END) AS passed_both,   -- identical text agreed (enrolled)
       SUM(CASE WHEN enacted = 1 THEN 1 ELSE 0 END) AS enacted,
       SUM(CASE WHEN vetoed_date IS NOT NULL THEN 1 ELSE 0 END) AS vetoed
FROM dw.dim_bill WHERE is_current = 1
GROUP BY session_code, bill_type;
GO

-- Filing pace: cumulative bills filed by day since prefiling, per session (89R vs 90R at the same point)
CREATE OR ALTER VIEW dw.v_filing_pace AS
WITH f AS (
    SELECT b.session_code, b.filed_date,
           DATEDIFF(day, CASE b.session_code WHEN '89R' THEN '2024-11-12' WHEN '90R' THEN '2026-11-09' END, b.filed_date) AS day_of_prefiling
    FROM dw.dim_bill b WHERE b.is_current = 1 AND b.session_code IN ('89R', '90R') AND b.filed_date IS NOT NULL)
SELECT session_code, day_of_prefiling, COUNT(*) AS filed_that_day,
       SUM(COUNT(*)) OVER (PARTITION BY session_code ORDER BY day_of_prefiling ROWS UNBOUNDED PRECEDING) AS filed_cumulative
FROM f GROUP BY session_code, day_of_prefiling;
GO

-- Committee bottlenecks: referred bills that never got out, per committee and session
CREATE OR ALTER VIEW dw.v_committee_bottleneck AS
SELECT session_code, 'House' AS chamber, house_committee AS committee,
       COUNT(*) AS referred,
       SUM(CASE WHEN house_committee_status = 'In committee' THEN 1 ELSE 0 END) AS still_in_committee,
       SUM(CASE WHEN enacted = 1 THEN 1 ELSE 0 END) AS enacted
FROM dw.dim_bill WHERE is_current = 1 AND house_committee IS NOT NULL GROUP BY session_code, house_committee
UNION ALL
SELECT session_code, 'Senate', senate_committee, COUNT(*),
       SUM(CASE WHEN senate_committee_status = 'In committee' THEN 1 ELSE 0 END),
       SUM(CASE WHEN enacted = 1 THEN 1 ELSE 0 END)
FROM dw.dim_bill WHERE is_current = 1 AND senate_committee IS NOT NULL GROUP BY session_code, senate_committee;
GO

-- Per-bill costs from the latest fiscal note (FY2026-27 = the 2026-27 biennium; out-years FY2028-30).
--   cost_2yr     state spending: all-funds lines "Probable Savings/(Cost) from ..." and "Probable (Cost) from ..."
--   revenue_loss_2yr  state revenue given up: "Probable Revenue (Loss)/Gain from ..." lines, excluding local
--                governments (cities, counties, school districts, transit, hospital districts)
--   Bills without an all-funds table fall back to the GR net impact (cost_basis = 'gr_net').
--   is_appropriations: appropriations bills (e.g. HB 500, supplemental appropriations, $13.2B) list only the
--   Comptroller as source agency but fund many agencies; agency exposure excludes them (dw.v_appropriations_bills).
CREATE OR ALTER VIEW dw.v_bill_cost AS
WITH lines AS (
    SELECT bill_id, fiscal_year, line_item, amount, fte,
           CASE WHEN line_item LIKE 'Probable%(Cost)%' THEN 'cost'
                WHEN line_item LIKE 'Probable Revenue%' AND line_item NOT LIKE '%Cities%' AND line_item NOT LIKE '%Counties%'
                     AND line_item NOT LIKE '%School District%' AND line_item NOT LIKE '%Transit%'
                     AND line_item NOT LIKE '%Hospital%' THEN 'revenue' END AS kind
    FROM dw.fact_fiscal_year WHERE table_name = 'all_funds'),
allf AS (
    SELECT bill_id,
           -SUM(CASE WHEN kind = 'cost' AND fiscal_year IN (2026, 2027) THEN amount END) AS all_funds_cost_2yr,
           -SUM(CASE WHEN kind = 'cost' AND fiscal_year IN (2028, 2029, 2030) THEN amount END) AS all_funds_cost_outyears,
           -SUM(CASE WHEN kind = 'revenue' AND fiscal_year IN (2026, 2027) THEN amount END) AS revenue_loss_2yr,
           MAX(CASE WHEN fiscal_year = 2027 THEN fte END) AS fte_2027,
           COUNT(*) AS n_lines
    FROM lines GROUP BY bill_id),
gr AS (
    SELECT bill_id,
           -SUM(CASE WHEN fiscal_year IN (2028, 2029, 2030) THEN amount END) AS gr_cost_outyears,
           -SUM(CASE WHEN fiscal_year = 2030 THEN amount END) AS gr_cost_2030,
           -SUM(CASE WHEN fiscal_year = 2026 THEN amount END) AS gr_cost_2026
    FROM dw.fact_fiscal_year WHERE table_name = 'gr' GROUP BY bill_id),
ag AS (SELECT bill_id, COUNT(*) AS n_agencies FROM dw.bridge_bill_agency GROUP BY bill_id)
SELECT b.bill_id, b.session_code, b.bill, b.caption, b.status, b.enacted, b.effective_date,
       n.version AS note_version, n.version_label, n.gr_2yr_cost, n.gr_2yr_kind, n.no_appropriation, n.cannot_be_determined,
       CASE WHEN a.n_lines IS NULL THEN n.gr_2yr_cost ELSE ISNULL(a.all_funds_cost_2yr, 0) END AS cost_2yr,
       CASE WHEN a.n_lines IS NULL THEN 'gr_net' ELSE 'all_funds_cost_lines' END AS cost_basis,
       a.revenue_loss_2yr, a.all_funds_cost_outyears, g.gr_cost_outyears,
       CASE WHEN b.caption LIKE 'Relating to making%appropriations%' OR b.caption LIKE '%general appropriations%'
            THEN 1 ELSE 0 END AS is_appropriations,
       -- grows later: the FY2030 GR cost is at least 50% above FY2026 (and material)
       CASE WHEN g.gr_cost_2030 > 1.5 * NULLIF(g.gr_cost_2026, 0) AND g.gr_cost_2030 > 1000000 THEN 1 ELSE 0 END AS grows_later,
       a.fte_2027, ISNULL(ag.n_agencies, 0) AS n_agencies, n.source_path
FROM dw.dim_bill b
JOIN dw.fact_fiscal_note n ON n.bill_id = b.bill_id AND n.is_latest = 1
LEFT JOIN allf a ON a.bill_id = b.bill_id
LEFT JOIN gr g ON g.bill_id = b.bill_id
LEFT JOIN ag ON ag.bill_id = b.bill_id
WHERE b.is_current = 1;
GO

-- Appropriations bills: money for many agencies through one bill; reported on their own, never as one agency's load
CREATE OR ALTER VIEW dw.v_appropriations_bills AS
SELECT bill_id, session_code, bill, caption, status, cost_2yr, gr_2yr_cost, revenue_loss_2yr
FROM dw.v_bill_cost WHERE is_appropriations = 1;
GO

-- Agency exposure to ENACTED bills of the 89th Legislature (regular + called sessions):
--   direct      bills naming exactly one agency → the whole note cost belongs to it (no assumption needed)
--   ai          multi-agency bills → per-agency amounts the model extracted from the note text that passed every
--               row check (quote in the note, number in the quote, agency listed: needs_review = 0). Where a bill's
--               rows don't add up to its total (reconciles = 0) the agency figure is a lower bound.
--               Rows that failed a check are counted, not summed.
--   shared      multi-agency bills with no checked AI split: counted, never spread evenly
-- load_pct_of_spend = (direct + ai) per year ÷ FY2024 spending. FY2024 predates these laws: it is a size yardstick.
-- Costs only (spending lines); revenue changes (tax relief) are in dw.v_bill_cost.revenue_loss_2yr, not agency load.
-- Appropriations bills are excluded (dw.v_appropriations_bills).
CREATE OR ALTER VIEW dw.v_agency_exposure AS
WITH enacted AS (
    SELECT c.* FROM dw.v_bill_cost c
    WHERE c.enacted = 1 AND c.session_code IN ('89R', '891', '892') AND c.is_appropriations = 0),
direct AS (
    SELECT ba.agency_code, COUNT(*) AS direct_bills, SUM(e.cost_2yr) AS direct_cost_2yr, SUM(e.fte_2027) AS new_fte,
           -- LBB "Source Agencies" = agencies consulted for the estimate. On tax bills that is often only the
           -- Comptroller, while the cost lands elsewhere (HB 9: $193.5M to the Foundation School Program, run by TEA).
           SUM(CASE WHEN e.caption LIKE '%tax%' THEN e.cost_2yr ELSE 0 END) AS direct_tax_bill_cost_2yr
    FROM enacted e JOIN dw.bridge_bill_agency ba ON ba.bill_id = e.bill_id
    WHERE e.n_agencies = 1 GROUP BY ba.agency_code),
ai AS (
    SELECT x.agency_code,
           SUM(CASE WHEN x.needs_review = 0 AND x.fiscal_year IN (2026, 2027) AND x.cost_type <> 'revenue' THEN -x.amount END) AS ai_cost_2yr,
           SUM(CASE WHEN x.needs_review = 1 THEN 1 ELSE 0 END) AS ai_rows_needing_review,
           COUNT(DISTINCT CASE WHEN x.needs_review = 0 THEN x.bill_id END) AS ai_bills,
           COUNT(DISTINCT CASE WHEN x.needs_review = 0 AND x.reconciles = 0 THEN x.bill_id END) AS ai_bills_partial
    FROM dw.fact_agency_cost_ai x JOIN enacted e ON e.bill_id = x.bill_id
    WHERE e.n_agencies > 1 GROUP BY x.agency_code),
shared AS (
    SELECT ba.agency_code, COUNT(*) AS shared_bills
    FROM enacted e JOIN dw.bridge_bill_agency ba ON ba.bill_id = e.bill_id
    WHERE e.n_agencies > 1
      AND NOT EXISTS (SELECT 1 FROM dw.fact_agency_cost_ai x WHERE x.bill_id = e.bill_id AND x.needs_review = 0)
    GROUP BY ba.agency_code),
named AS (
    SELECT ba.agency_code, COUNT(*) AS enacted_bills FROM enacted e JOIN dw.bridge_bill_agency ba ON ba.bill_id = e.bill_id
    GROUP BY ba.agency_code),
spend AS (SELECT agency_code, SUM(amount) AS fy2024_spend FROM dw.fact_agency_spend WHERE fiscal_year = 2024 GROUP BY agency_code)
SELECT a.agency_code, a.agency_name, ISNULL(n.enacted_bills, 0) AS enacted_bills,
       ISNULL(d.direct_bills, 0) AS direct_bills, ISNULL(d.direct_cost_2yr, 0) AS direct_cost_2yr,
       ISNULL(d.direct_tax_bill_cost_2yr, 0) AS direct_tax_bill_cost_2yr,
       ISNULL(ai.ai_cost_2yr, 0) AS ai_cost_2yr, ISNULL(ai.ai_bills, 0) AS ai_bills,
       ISNULL(ai.ai_rows_needing_review, 0) AS ai_rows_needing_review, ISNULL(ai.ai_bills_partial, 0) AS ai_bills_partial,
       ISNULL(s.shared_bills, 0) AS shared_bills,
       d.new_fte, sp.fy2024_spend,
       ISNULL(d.direct_cost_2yr, 0) + ISNULL(ai.ai_cost_2yr, 0) AS attributed_cost_2yr,
       CAST(ROUND(100.0 * (ISNULL(d.direct_cost_2yr, 0) + ISNULL(ai.ai_cost_2yr, 0)) / 2 / NULLIF(sp.fy2024_spend, 0), 2) AS decimal(10, 2)) AS load_pct_of_spend
FROM dw.dim_agency a
LEFT JOIN named n ON n.agency_code = a.agency_code
LEFT JOIN direct d ON d.agency_code = a.agency_code
LEFT JOIN ai ON ai.agency_code = a.agency_code
LEFT JOIN shared s ON s.agency_code = a.agency_code
LEFT JOIN spend sp ON sp.agency_code = a.agency_code
WHERE n.enacted_bills > 0;
GO

-- Implementation calendar: enacted bills by effective date (from the bill's "Effective ..." action), per agency
CREATE OR ALTER VIEW dw.v_implementation_calendar AS
SELECT c.bill_id, c.session_code, c.bill, c.caption, ba.agency_code, a.agency_name,
       COALESCE(c.effective_date, b.signed_date) AS effective_or_signed, c.cost_2yr, c.fte_2027,
       (SELECT STRING_AGG(m.mandate_type, ', ') FROM (SELECT DISTINCT mandate_type FROM dw.fact_bill_mandate m
         WHERE m.bill_id = c.bill_id) m) AS duties
FROM dw.v_bill_cost c
JOIN dw.dim_bill b ON b.bill_id = c.bill_id AND b.is_current = 1
JOIN dw.bridge_bill_agency ba ON ba.bill_id = c.bill_id
JOIN dw.dim_agency a ON a.agency_code = ba.agency_code
WHERE c.enacted = 1;
GO

-- Estimate drift: first vs latest fiscal-note two-year figure per bill
CREATE OR ALTER VIEW dw.v_estimate_drift AS
WITH v AS (
    SELECT bill_id, version, version_label, gr_2yr_net, note_date_key,
           ROW_NUMBER() OVER (PARTITION BY bill_id ORDER BY version_rank, note_date_key) AS first_rn,
           ROW_NUMBER() OVER (PARTITION BY bill_id ORDER BY version_rank DESC, note_date_key DESC) AS last_rn,
           COUNT(*) OVER (PARTITION BY bill_id) AS n_versions
    FROM dw.fact_fiscal_note)
SELECT f.bill_id, b.session_code, b.bill, b.caption, b.status, f.n_versions,
       f.version AS first_version, -f.gr_2yr_net AS first_cost_2yr,
       l.version AS latest_version, -l.gr_2yr_net AS latest_cost_2yr,
       -l.gr_2yr_net - (-f.gr_2yr_net) AS change_2yr,
       -- percent change in COST (costs are negative in the note's net figure, so compute on the negated values)
       CASE WHEN f.gr_2yr_net <> 0 THEN CAST(ROUND(100.0 * ((-l.gr_2yr_net) - (-f.gr_2yr_net)) / ABS(f.gr_2yr_net), 1) AS decimal(10, 1)) END AS change_pct
FROM v f JOIN v l ON l.bill_id = f.bill_id AND l.last_rn = 1
JOIN dw.dim_bill b ON b.bill_id = f.bill_id AND b.is_current = 1
WHERE f.first_rn = 1 AND f.n_versions > 1;
GO

-- Live watchlist (90R): bills filed with their agency subjects and whether a fiscal note / hearing exists yet
CREATE OR ALTER VIEW dw.v_watchlist AS
SELECT b.bill_id, b.bill, b.caption, b.filed_date, b.status, b.house_committee, b.senate_committee,
       s.subject AS agency_subject, s.subject_code,
       CASE WHEN EXISTS (SELECT 1 FROM dw.fact_fiscal_note n WHERE n.bill_id = b.bill_id) THEN 1 ELSE 0 END AS has_fiscal_note,
       CASE WHEN EXISTS (SELECT 1 FROM dw.fact_bill_action x WHERE x.bill_id = b.bill_id AND x.description LIKE 'Scheduled for public hearing%') THEN 1 ELSE 0 END AS hearing_scheduled,
       (SELECT MAX(d.[date]) FROM dw.fact_bill_action x JOIN dw.dim_date d ON d.date_key = x.action_date_key WHERE x.bill_id = b.bill_id) AS last_action_date
FROM dw.dim_bill b
LEFT JOIN dw.bridge_bill_subject s ON s.bill_id = b.bill_id AND s.is_agency = 1
WHERE b.is_current = 1 AND b.session_code = '90R';
GO

-- Data quality: one row of checks for the dashboard's trust page
CREATE OR ALTER VIEW dw.v_data_quality AS
SELECT
    (SELECT COUNT(*) FROM dw.dim_bill WHERE is_current = 1) AS bills,
    (SELECT COUNT(*) FROM dw.dim_bill WHERE is_current = 0) AS bill_history_rows,
    (SELECT COUNT(*) FROM dw.fact_fiscal_note) AS fiscal_notes,
    (SELECT COUNT(*) FROM dw.dim_bill WHERE is_current = 1 AND session_code = '89R' AND bill_type IN ('HB', 'SB')
        AND enrolled_date IS NOT NULL) AS passed_89r_hb_sb,
    1213 AS passed_89r_hb_sb_official,   -- reports/passed.htm (7/9/2025): 619 HB + 594 SB
    (SELECT COUNT(*) FROM dw.fact_agency_cost_ai) AS ai_cost_rows,
    (SELECT COUNT(*) FROM dw.fact_agency_cost_ai WHERE needs_review = 1) AS ai_rows_needing_review,
    (SELECT MAX(logged_utc) FROM ctl.ingest_run WHERE status = 'Succeeded') AS last_ingest_utc,
    (SELECT MAX(finished_utc) FROM ctl.load_run WHERE status = 'Succeeded') AS last_load_utc,
    (SELECT COUNT(*) FROM ctl.ingest_run WHERE status = 'Failed' AND logged_utc > DATEADD(day, -7, SYSUTCDATETIME())) AS failed_ingests_7d;
GO
