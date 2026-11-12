-- Serving layer: staging tables (exact copies of gold Parquet, loaded by Data Factory) and a star schema (dw)
-- loaded from staging by dw.usp_load_all. Every load is idempotent: rerunning it gives the same result.
--
-- Keys: bill_id = '<session>:<bill>' (e.g. '89R:HB2') is the durable business key. dw.dim_bill keeps SCD type 2
-- history of each bill's status/committee status; dw.v_bill is the current row per bill for Power BI.

IF SCHEMA_ID('stg') IS NULL EXEC('CREATE SCHEMA stg');
IF SCHEMA_ID('dw') IS NULL EXEC('CREATE SCHEMA dw');
GO

------------------------------------------------------------------------------------------- staging
DROP TABLE IF EXISTS stg.bill, stg.bill_action, stg.bill_subject, stg.fiscal_note_latest, stg.fiscal_note_version,
    stg.fiscal_note_drift, stg.fiscal_note_agency, stg.fiscal_note_year, stg.agency_spend,
    stg.ai_agency_cost, stg.ai_mandate;
GO
CREATE TABLE stg.bill (
    session varchar(4), bill varchar(12), bill_type varchar(4), bill_number int, caption nvarchar(max),
    authors nvarchar(max), sponsors nvarchar(max), house_committee nvarchar(200), house_committee_status nvarchar(100),
    senate_committee nvarchar(200), senate_committee_status nvarchar(100), filed_date date, referred_date date,
    passed_house_date date, passed_senate_date date, enrolled_date date, sent_to_governor_date date, signed_date date,
    vetoed_date date, filed_without_signature_date date, effective_date date, enacted bit, status varchar(30), last_action nvarchar(max),
    n_actions int, source_path nvarchar(500));
CREATE TABLE stg.bill_action (
    session varchar(4), bill varchar(12), seq int, action_number varchar(10), chamber varchar(2), action_date date,
    description nvarchar(1000), comment nvarchar(max));
CREATE TABLE stg.bill_subject (
    session varchar(4), bill varchar(12), subject nvarchar(300), subject_code varchar(10), is_agency bit);
CREATE TABLE stg.fiscal_note_latest (
    session varchar(4), bill varchar(12), version varchar(2), version_label nvarchar(100), note_date date,
    gr_2yr_net float, gr_2yr_kind varchar(20), no_appropriation bit, cannot_be_determined bit,
    local_gov_significant bit, n_agencies int, source_path nvarchar(500));
CREATE TABLE stg.fiscal_note_version (
    session varchar(4), bill varchar(12), version varchar(2), version_rank int, version_label nvarchar(100),
    note_date date, gr_2yr_net float, gr_2yr_kind varchar(20), source_path nvarchar(500));
CREATE TABLE stg.fiscal_note_drift (
    session varchar(4), bill varchar(12), latest_version varchar(2), latest_gr_2yr_net float, latest_note_date date,
    first_version varchar(2), first_gr_2yr_net float, first_note_date date, n_versions bigint);
CREATE TABLE stg.fiscal_note_agency (
    session varchar(4), bill varchar(12), agency_code varchar(3), agency_name nvarchar(300));
CREATE TABLE stg.fiscal_note_year (
    session varchar(4), bill varchar(12), table_name varchar(20), fiscal_year int, line_item nvarchar(300),
    amount float, fte float);
CREATE TABLE stg.agency_spend (
    fiscal_year int, agency_code varchar(3), agency_name nvarchar(300), spending_category nvarchar(200), amount float);
-- AI extraction outputs (phase 5b; empty until that job runs)
CREATE TABLE stg.ai_agency_cost (
    session varchar(4), bill varchar(12), version varchar(2), agency_code varchar(3), fiscal_year int, amount float,
    cost_type varchar(30), evidence nvarchar(max), reconciles bit, needs_review bit, model varchar(60));
CREATE TABLE stg.ai_mandate (
    session varchar(4), bill varchar(12), mandate_type varchar(30), agency_code varchar(3), evidence nvarchar(max),
    model varchar(60));
GO

------------------------------------------------------------------------------------------- dimensions
IF OBJECT_ID('dw.dim_date') IS NULL
BEGIN
    CREATE TABLE dw.dim_date (
        date_key int PRIMARY KEY, [date] date NOT NULL UNIQUE, [year] int, [month] int, month_name varchar(10),
        state_fiscal_year int,           -- Texas fiscal year: Sep 1 - Aug 31 (FY2026 = Sep 2025 - Aug 2026)
        week_start date);
    ;WITH d AS (SELECT CAST('2024-01-01' AS date) AS dt UNION ALL SELECT DATEADD(day, 1, dt) FROM d WHERE dt < '2031-12-31')
    INSERT dw.dim_date
    SELECT CONVERT(int, FORMAT(dt, 'yyyyMMdd')), dt, YEAR(dt), MONTH(dt), DATENAME(month, dt),
           CASE WHEN MONTH(dt) >= 9 THEN YEAR(dt) + 1 ELSE YEAR(dt) END,
           DATEADD(day, -(DATEPART(weekday, dt) + @@DATEFIRST - 2) % 7, dt)
    FROM d OPTION (MAXRECURSION 0);
END;
GO

IF OBJECT_ID('dw.dim_session') IS NULL
BEGIN
    CREATE TABLE dw.dim_session (
        session_code varchar(4) PRIMARY KEY, legislature int, session_kind varchar(20), label nvarchar(60),
        start_date date, live bit);
    INSERT dw.dim_session VALUES
        ('89R', 89, 'regular', N'89th Legislature, Regular Session (2025)', '2025-01-14', 0),
        ('891', 89, 'special', N'89th Legislature, 1st Called Session (2025)', '2025-07-21', 0),
        ('892', 89, 'special', N'89th Legislature, 2nd Called Session (2025)', '2025-08-15', 0),
        ('90R', 90, 'regular', N'90th Legislature, Regular Session (2027) - prefiling', '2027-01-12', 1);
END;
GO

IF OBJECT_ID('dw.dim_agency') IS NULL
CREATE TABLE dw.dim_agency (
    agency_code varchar(3) PRIMARY KEY,           -- state agency number (Comptroller) = LBB fiscal-note code
    agency_name nvarchar(300) NOT NULL,
    name_source varchar(20) NOT NULL,             -- comptroller | fiscal_note
    updated_utc datetime2(0) NOT NULL DEFAULT SYSUTCDATETIME());
GO

-- SCD type 2: a new row whenever a tracked attribute changes (status, committee status, last action date).
IF OBJECT_ID('dw.dim_bill') IS NULL
CREATE TABLE dw.dim_bill (
    bill_sk int IDENTITY(1,1) PRIMARY KEY,
    bill_id varchar(20) NOT NULL,
    session_code varchar(4) NOT NULL, bill varchar(12) NOT NULL, bill_type varchar(4), bill_number int,
    caption nvarchar(max), authors nvarchar(max), sponsors nvarchar(max),
    house_committee nvarchar(200), house_committee_status nvarchar(100),
    senate_committee nvarchar(200), senate_committee_status nvarchar(100),
    status varchar(30), enacted bit, filed_date date, referred_date date, passed_house_date date,
    passed_senate_date date, sent_to_governor_date date, signed_date date, vetoed_date date, effective_date date,
    last_action nvarchar(max), source_path nvarchar(500),
    row_hash varbinary(32) NOT NULL,
    valid_from_utc datetime2(0) NOT NULL, valid_to_utc datetime2(0) NULL, is_current bit NOT NULL);
GO
IF COL_LENGTH('dw.dim_bill', 'enrolled_date') IS NULL ALTER TABLE dw.dim_bill ADD enrolled_date date NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ux_dim_bill_current')
    CREATE UNIQUE INDEX ux_dim_bill_current ON dw.dim_bill (bill_id) WHERE is_current = 1;
GO

------------------------------------------------------------------------------------------- facts
IF OBJECT_ID('dw.fact_bill_action') IS NULL
CREATE TABLE dw.fact_bill_action (
    bill_id varchar(20) NOT NULL, seq int NOT NULL, action_date_key int NULL, chamber varchar(2),
    action_number varchar(10), description nvarchar(1000), comment nvarchar(max),
    CONSTRAINT pk_fact_bill_action PRIMARY KEY (bill_id, seq));
IF OBJECT_ID('dw.bridge_bill_subject') IS NULL
CREATE TABLE dw.bridge_bill_subject (
    bill_id varchar(20) NOT NULL, subject nvarchar(300) NOT NULL, subject_code varchar(10), is_agency bit);
IF OBJECT_ID('dw.fact_fiscal_note') IS NULL
CREATE TABLE dw.fact_fiscal_note (
    bill_id varchar(20) NOT NULL, version varchar(2) NOT NULL, version_rank int, version_label nvarchar(100),
    note_date_key int NULL, gr_2yr_net float, gr_2yr_cost AS (-gr_2yr_net), gr_2yr_kind varchar(20),
    is_latest bit NOT NULL DEFAULT 0, no_appropriation bit, cannot_be_determined bit, local_gov_significant bit,
    source_path nvarchar(500),
    CONSTRAINT pk_fact_fiscal_note PRIMARY KEY (bill_id, version));
IF OBJECT_ID('dw.fact_fiscal_year') IS NULL
CREATE TABLE dw.fact_fiscal_year (
    bill_id varchar(20) NOT NULL, table_name varchar(20) NOT NULL, fiscal_year int NOT NULL,
    line_item nvarchar(300) NOT NULL, amount float NULL, cost AS (-amount), fte float NULL);
IF OBJECT_ID('dw.bridge_bill_agency') IS NULL
CREATE TABLE dw.bridge_bill_agency (
    bill_id varchar(20) NOT NULL, agency_code varchar(3) NOT NULL,
    n_agencies_on_bill int NOT NULL,
    CONSTRAINT pk_bridge_bill_agency PRIMARY KEY (bill_id, agency_code));
IF OBJECT_ID('dw.fact_agency_spend') IS NULL
CREATE TABLE dw.fact_agency_spend (
    agency_code varchar(3) NOT NULL, fiscal_year int NOT NULL, spending_category nvarchar(200) NOT NULL,
    amount float NOT NULL);
IF OBJECT_ID('dw.fact_agency_cost_ai') IS NULL
CREATE TABLE dw.fact_agency_cost_ai (
    bill_id varchar(20) NOT NULL, version varchar(2), agency_code varchar(3) NOT NULL, fiscal_year int NOT NULL,
    amount float NOT NULL, cost AS (-amount), cost_type varchar(30), evidence nvarchar(max),
    reconciles bit, needs_review bit, model varchar(60));
IF OBJECT_ID('dw.fact_bill_mandate') IS NULL
CREATE TABLE dw.fact_bill_mandate (
    bill_id varchar(20) NOT NULL, mandate_type varchar(30) NOT NULL, agency_code varchar(3) NULL,
    evidence nvarchar(max), model varchar(60));
IF OBJECT_ID('ctl.load_run') IS NULL
CREATE TABLE ctl.load_run (
    load_id bigint IDENTITY(1,1) PRIMARY KEY, pipeline_run_id varchar(64), started_utc datetime2(0),
    finished_utc datetime2(0), bills int, bill_rows_new_version int, notes int, status varchar(20),
    error_message nvarchar(2000));
GO

------------------------------------------------------------------------------------------- load procedure
-- DELETE, not TRUNCATE: the procedure runs as Data Factory's identity with EXECUTE only; ownership chaining covers
-- DML inside the procedure but TRUNCATE would need ALTER on every dw table.
CREATE OR ALTER PROCEDURE dw.usp_load_all @pipeline_run_id varchar(64) = 'manual'
AS
BEGIN
    SET NOCOUNT ON; SET XACT_ABORT ON;
    DECLARE @now datetime2(0) = SYSUTCDATETIME(), @new_versions int = 0, @load_id bigint;
    INSERT ctl.load_run (pipeline_run_id, started_utc, status) VALUES (@pipeline_run_id, @now, 'Running');
    SET @load_id = SCOPE_IDENTITY();
    BEGIN TRY
        BEGIN TRAN;

        -- agencies: Comptroller names win; fiscal-note names fill agencies that report no spending
        ;WITH ranked AS (
            SELECT agency_code, agency_name, name_source,
                   ROW_NUMBER() OVER (PARTITION BY agency_code ORDER BY pri, LEN(agency_name) DESC) AS rn
            FROM (SELECT agency_code, agency_name, 'comptroller' AS name_source, 1 AS pri FROM stg.agency_spend
                  UNION ALL SELECT agency_code, agency_name, 'fiscal_note', 2 FROM stg.fiscal_note_agency) x
            WHERE agency_code IS NOT NULL AND agency_code <> '' AND agency_name IS NOT NULL)
        MERGE dw.dim_agency AS t
        USING (SELECT agency_code, agency_name, name_source FROM ranked WHERE rn = 1) AS s
        ON t.agency_code = s.agency_code
        WHEN MATCHED AND (t.agency_name <> s.agency_name OR t.name_source <> s.name_source)
            THEN UPDATE SET agency_name = s.agency_name, name_source = s.name_source, updated_utc = @now
        WHEN NOT MATCHED THEN INSERT (agency_code, agency_name, name_source) VALUES (s.agency_code, s.agency_name, s.name_source);

        -- bills: SCD2 on the tracked attributes
        SELECT session + ':' + bill AS bill_id, *,
               HASHBYTES('SHA2_256', CONCAT_WS('|', status, house_committee_status, senate_committee_status,
                         house_committee, senate_committee, CONVERT(varchar(10), enrolled_date, 23),
                         CONVERT(varchar(10), signed_date, 23),
                         CONVERT(varchar(10), vetoed_date, 23), CAST(n_actions AS varchar(10)))) AS row_hash
        INTO #bill FROM stg.bill;

        UPDATE d SET valid_to_utc = @now, is_current = 0
        FROM dw.dim_bill d JOIN #bill s ON s.bill_id = d.bill_id
        WHERE d.is_current = 1 AND d.row_hash <> s.row_hash;
        SET @new_versions = @@ROWCOUNT;

        INSERT dw.dim_bill (bill_id, session_code, bill, bill_type, bill_number, caption, authors, sponsors,
            house_committee, house_committee_status, senate_committee, senate_committee_status, status, enacted,
            filed_date, referred_date, passed_house_date, passed_senate_date, enrolled_date, sent_to_governor_date, signed_date,
            vetoed_date, effective_date, last_action, source_path, row_hash, valid_from_utc, valid_to_utc, is_current)
        SELECT s.bill_id, s.session, s.bill, s.bill_type, s.bill_number, s.caption, s.authors, s.sponsors,
            s.house_committee, s.house_committee_status, s.senate_committee, s.senate_committee_status, s.status,
            s.enacted, s.filed_date, s.referred_date, s.passed_house_date, s.passed_senate_date, s.enrolled_date,
            s.sent_to_governor_date, s.signed_date, s.vetoed_date, s.effective_date, s.last_action, s.source_path,
            s.row_hash, @now, NULL, 1
        FROM #bill s
        WHERE NOT EXISTS (SELECT 1 FROM dw.dim_bill d WHERE d.bill_id = s.bill_id AND d.is_current = 1);

        -- type-1 attributes (caption/authors can be corrected without a new version)
        UPDATE d SET caption = s.caption, authors = s.authors, sponsors = s.sponsors, last_action = s.last_action
        FROM dw.dim_bill d JOIN #bill s ON s.bill_id = d.bill_id
        WHERE d.is_current = 1;

        -- facts: replace rows for the bills present in staging (staging always holds the full gold set)
        DELETE f FROM dw.fact_bill_action f WHERE EXISTS (SELECT 1 FROM #bill b WHERE b.bill_id = f.bill_id);
        INSERT dw.fact_bill_action (bill_id, seq, action_date_key, chamber, action_number, description, comment)
        SELECT session + ':' + bill, seq, CONVERT(int, FORMAT(action_date, 'yyyyMMdd')), chamber, action_number,
               description, comment
        FROM stg.bill_action;

        DELETE FROM dw.bridge_bill_subject;
        INSERT dw.bridge_bill_subject SELECT DISTINCT session + ':' + bill, subject, subject_code, is_agency FROM stg.bill_subject;

        DELETE FROM dw.fact_fiscal_note;
        INSERT dw.fact_fiscal_note (bill_id, version, version_rank, version_label, note_date_key, gr_2yr_net,
                                    gr_2yr_kind, is_latest, no_appropriation, cannot_be_determined,
                                    local_gov_significant, source_path)
        SELECT v.session + ':' + v.bill, v.version, v.version_rank, v.version_label,
               CONVERT(int, FORMAT(v.note_date, 'yyyyMMdd')), v.gr_2yr_net, v.gr_2yr_kind,
               CASE WHEN l.version IS NOT NULL THEN 1 ELSE 0 END,
               l.no_appropriation, l.cannot_be_determined, l.local_gov_significant, v.source_path
        FROM stg.fiscal_note_version v
        LEFT JOIN stg.fiscal_note_latest l ON l.session = v.session AND l.bill = v.bill AND l.version = v.version;

        DELETE FROM dw.fact_fiscal_year;
        INSERT dw.fact_fiscal_year (bill_id, table_name, fiscal_year, line_item, amount, fte)
        SELECT session + ':' + bill, table_name, fiscal_year, line_item, amount, fte FROM stg.fiscal_note_year;

        DELETE FROM dw.bridge_bill_agency;
        INSERT dw.bridge_bill_agency (bill_id, agency_code, n_agencies_on_bill)
        SELECT session + ':' + bill, agency_code, COUNT(*) OVER (PARTITION BY session, bill)
        FROM (SELECT DISTINCT session, bill, agency_code FROM stg.fiscal_note_agency) a;

        DELETE FROM dw.fact_agency_spend;
        INSERT dw.fact_agency_spend SELECT agency_code, fiscal_year, spending_category, amount FROM stg.agency_spend
        WHERE agency_code IS NOT NULL AND agency_code <> '';   -- the source's grand-total row has no agency

        -- AI outputs: replace only when the AI job produced rows (it runs less often than the daily load)
        IF EXISTS (SELECT 1 FROM stg.ai_agency_cost)
        BEGIN
            DELETE FROM dw.fact_agency_cost_ai;
            INSERT dw.fact_agency_cost_ai (bill_id, version, agency_code, fiscal_year, amount, cost_type, evidence,
                                           reconciles, needs_review, model)
            SELECT session + ':' + bill, version, agency_code, fiscal_year, amount, cost_type, evidence, reconciles,
                   needs_review, model FROM stg.ai_agency_cost;
        END;
        IF EXISTS (SELECT 1 FROM stg.ai_mandate)
        BEGIN
            DELETE FROM dw.fact_bill_mandate;
            INSERT dw.fact_bill_mandate SELECT session + ':' + bill, mandate_type, agency_code, evidence, model FROM stg.ai_mandate;
        END;

        COMMIT;
        UPDATE ctl.load_run SET finished_utc = SYSUTCDATETIME(), status = 'Succeeded',
               bills = (SELECT COUNT(*) FROM #bill), bill_rows_new_version = @new_versions,
               notes = (SELECT COUNT(*) FROM dw.fact_fiscal_note)
        WHERE load_id = @load_id;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK;
        UPDATE ctl.load_run SET finished_utc = SYSUTCDATETIME(), status = 'Failed', error_message = ERROR_MESSAGE()
        WHERE load_id = @load_id;
        THROW;
    END CATCH
END;
GO
