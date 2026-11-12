-- Control schema: what to ingest, on which schedule, the watermark per source, and a log of every run.
--
-- The Legislature FTP server does not report file modification times (ADF: UserErrorLastModifiedNotSupported),
-- so "copy files modified since the watermark" is impossible there. Instead:
--   schedule 'once'    full copy, then never again (finished sessions: 89R, 891, 892; Comptroller FY2024)
--   schedule 'daily'   kind 'ftp_manifest': load billhistory/history_periodic.xml (the Legislature's change
--                      manifest) into stg.legis_manifest, re-copy only the 100-bill folders whose bills changed
--                      after the watermark, set the watermark to the newest manifest timestamp (source clock, Central)
--   schedule 'weekly'  full copy of the live session's folders (reconciles anything a manifest window missed)
-- Data Factory reads ctl.v_sources_due / ctl.usp_sources_due, then calls ctl.usp_log_ingest, which advances the
-- watermark only when the copy succeeded. Idempotent: safe to run again.

IF SCHEMA_ID('ctl') IS NULL EXEC('CREATE SCHEMA ctl');
GO

IF OBJECT_ID('ctl.ingest_source') IS NULL
CREATE TABLE ctl.ingest_source (
    source_id     int IDENTITY(1,1) PRIMARY KEY,
    source_name   varchar(100)  NOT NULL UNIQUE,     -- e.g. legis-89R-billhistory-house
    kind          varchar(20)   NOT NULL CHECK (kind IN ('ftp', 'ftp_manifest', 'http')),
    session_code  varchar(4)    NULL,                -- 89R, 891, 892, 90R
    src_path      varchar(300)  NOT NULL,            -- FTP folder or HTTP relative URL
    sink_path     varchar(300)  NOT NULL,            -- path under the bronze container
    file_pattern  varchar(100)  NOT NULL DEFAULT '*',
    recursive     bit           NOT NULL DEFAULT 1,
    schedule      varchar(10)   NOT NULL DEFAULT 'once' CHECK (schedule IN ('once', 'daily', 'weekly')),
    enabled       bit           NOT NULL DEFAULT 1,
    watermark_utc datetime2(0)  NOT NULL DEFAULT '1900-01-01',
    updated_utc   datetime2(0)  NOT NULL DEFAULT SYSUTCDATETIME()
);
GO

IF OBJECT_ID('ctl.ingest_run') IS NULL
CREATE TABLE ctl.ingest_run (
    run_id            bigint IDENTITY(1,1) PRIMARY KEY,
    pipeline_run_id   varchar(64)  NOT NULL,
    source_id         int          NOT NULL REFERENCES ctl.ingest_source(source_id),
    window_start_utc  datetime2(0) NOT NULL,
    window_end_utc    datetime2(0) NOT NULL,
    status            varchar(20)  NOT NULL,          -- Succeeded | Failed
    files_read        int          NULL,
    files_written     int          NULL,
    bytes_read        bigint       NULL,
    error_message     nvarchar(2000) NULL,
    logged_utc        datetime2(0) NOT NULL DEFAULT SYSUTCDATETIME()
);
GO

-- Sources due for a run. @mode = 'daily' (default trigger) or 'weekly' (reconcile trigger).
-- Anything that has never succeeded (watermark 1900) is due in any mode.
CREATE OR ALTER PROCEDURE ctl.usp_sources_due @mode varchar(10) = 'daily'
AS
SELECT source_id, source_name, kind, ISNULL(session_code, '') AS session_code, src_path, sink_path, file_pattern,
       CAST(recursive AS int) AS recursive,
       CONVERT(varchar(30), watermark_utc, 127) + 'Z' AS watermark_utc
FROM ctl.ingest_source
WHERE enabled = 1
  AND (watermark_utc < '2000-01-01' OR schedule = 'daily' OR (schedule = 'weekly' AND @mode = 'weekly'))
ORDER BY source_id;
GO

-- The change manifest as copied by Data Factory (XML attributes @id/@path/@timestamp → columns).
IF OBJECT_ID('stg.legis_manifest') IS NULL
CREATE TABLE stg.legis_manifest (
    source_id int NULL, bill_label varchar(20), file_path varchar(300), ts_text varchar(40));
GO

-- Changed 100-bill folders since a source's watermark: one row per (chamber folder, group folder).
-- path '/bills/90R/billhistory/house_bills/HB00100_HB00199/HB 101.xml' → house_bills, HB00100_HB00199
CREATE OR ALTER PROCEDURE ctl.usp_manifest_changes @source_id int
AS
BEGIN
    SET NOCOUNT ON;
    -- rows just copied by Data Factory arrive without a source_id
    UPDATE stg.legis_manifest SET source_id = @source_id WHERE source_id IS NULL;
    DECLARE @wm datetime2(0) = (SELECT watermark_utc FROM ctl.ingest_source WHERE source_id = @source_id);
    ;WITH f AS (
        SELECT TRY_PARSE(ts_text AS datetime2(0) USING 'en-US') AS ts,
               LEFT(file_path, LEN(file_path) - CHARINDEX('/', REVERSE(file_path))) AS dir   -- drop the file name
        FROM stg.legis_manifest WHERE source_id = @source_id),
    p AS (
        SELECT ts, dir, LEFT(dir, LEN(dir) - CHARINDEX('/', REVERSE(dir))) AS parent,
               RIGHT(dir, CHARINDEX('/', REVERSE(dir)) - 1) AS group_folder
        FROM f WHERE CHARINDEX('/', dir) > 0),
    m AS (
        SELECT ts, group_folder, RIGHT(parent, CHARINDEX('/', REVERSE(parent)) - 1) AS chamber_folder FROM p)
    SELECT chamber_folder, group_folder, COUNT(*) AS changed_bills,
           CONVERT(varchar(30), MAX(ts), 127) AS max_ts,
           CONVERT(varchar(30), MAX(MAX(ts)) OVER (), 127) AS run_max_ts
    FROM m
    WHERE ts > @wm AND chamber_folder IN ('house_bills', 'senate_bills')
    GROUP BY chamber_folder, group_folder
    ORDER BY chamber_folder, group_folder;
END;
GO

CREATE OR ALTER PROCEDURE ctl.usp_log_ingest
    @pipeline_run_id varchar(64), @source_id int,
    @window_start_utc datetime2(0), @window_end_utc datetime2(0),
    @status varchar(20), @files_read int = NULL, @files_written int = NULL,
    @bytes_read bigint = NULL, @error_message nvarchar(2000) = NULL
AS
BEGIN
    SET NOCOUNT ON;
    INSERT ctl.ingest_run (pipeline_run_id, source_id, window_start_utc, window_end_utc, status,
                           files_read, files_written, bytes_read, error_message)
    VALUES (@pipeline_run_id, @source_id, @window_start_utc, @window_end_utc, @status,
            @files_read, @files_written, @bytes_read, @error_message);
    -- Advance the watermark only on success, and never backwards (a late retry can't rewind it).
    IF @status = 'Succeeded'
        UPDATE ctl.ingest_source
        SET watermark_utc = @window_end_utc, updated_utc = SYSUTCDATETIME()
        WHERE source_id = @source_id AND watermark_utc < @window_end_utc;
END;
GO

-- Reset one source so the next run re-copies everything (runbook: full reload).
CREATE OR ALTER PROCEDURE ctl.usp_reset_watermark @source_name varchar(100)
AS
UPDATE ctl.ingest_source SET watermark_utc = '1900-01-01', updated_utc = SYSUTCDATETIME()
WHERE source_name = @source_name;
GO

-- Seed sources. Bills only (house_bills, senate_bills): resolutions carry no fiscal notes worth modelling.
-- 89R + its two special sessions = the enacted history; 90R = the live 2027 session (prefiling from 2026-11-09):
-- its four folders are the weekly full reconcile, legis-90R-manifest is the daily incremental.
MERGE ctl.ingest_source AS t
USING (VALUES
    ('legis-89R-billhistory-house',  'ftp', '89R', 'bills/89R/billhistory/house_bills',          'legis/89R/billhistory/house_bills', 'once'),
    ('legis-89R-billhistory-senate', 'ftp', '89R', 'bills/89R/billhistory/senate_bills',         'legis/89R/billhistory/senate_bills', 'once'),
    ('legis-89R-fiscalnotes-house',  'ftp', '89R', 'bills/89R/fiscalnotes/html/house_bills',     'legis/89R/fiscalnotes/house_bills', 'once'),
    ('legis-89R-fiscalnotes-senate', 'ftp', '89R', 'bills/89R/fiscalnotes/html/senate_bills',    'legis/89R/fiscalnotes/senate_bills', 'once'),
    ('legis-891-billhistory-house',  'ftp', '891', 'bills/891/billhistory/house_bills',          'legis/891/billhistory/house_bills', 'once'),
    ('legis-891-billhistory-senate', 'ftp', '891', 'bills/891/billhistory/senate_bills',         'legis/891/billhistory/senate_bills', 'once'),
    ('legis-891-fiscalnotes-house',  'ftp', '891', 'bills/891/fiscalnotes/html/house_bills',     'legis/891/fiscalnotes/house_bills', 'once'),
    ('legis-891-fiscalnotes-senate', 'ftp', '891', 'bills/891/fiscalnotes/html/senate_bills',    'legis/891/fiscalnotes/senate_bills', 'once'),
    ('legis-892-billhistory-house',  'ftp', '892', 'bills/892/billhistory/house_bills',          'legis/892/billhistory/house_bills', 'once'),
    ('legis-892-billhistory-senate', 'ftp', '892', 'bills/892/billhistory/senate_bills',         'legis/892/billhistory/senate_bills', 'once'),
    ('legis-892-fiscalnotes-house',  'ftp', '892', 'bills/892/fiscalnotes/html/house_bills',     'legis/892/fiscalnotes/house_bills', 'once'),
    ('legis-892-fiscalnotes-senate', 'ftp', '892', 'bills/892/fiscalnotes/html/senate_bills',    'legis/892/fiscalnotes/senate_bills', 'once'),
    ('legis-89R-reports',            'ftp', '89R', 'bills/89R/reports',                          'legis/89R/reports', 'once'),  -- top-level *.htm only (see UPDATE below)
    ('legis-90R-billhistory-house',  'ftp', '90R', 'bills/90R/billhistory/house_bills',          'legis/90R/billhistory/house_bills', 'weekly'),
    ('legis-90R-billhistory-senate', 'ftp', '90R', 'bills/90R/billhistory/senate_bills',         'legis/90R/billhistory/senate_bills', 'weekly'),
    ('legis-90R-fiscalnotes-house',  'ftp', '90R', 'bills/90R/fiscalnotes/html/house_bills',     'legis/90R/fiscalnotes/house_bills', 'weekly'),
    ('legis-90R-fiscalnotes-senate', 'ftp', '90R', 'bills/90R/fiscalnotes/html/senate_bills',    'legis/90R/fiscalnotes/senate_bills', 'weekly'),
    ('legis-90R-manifest',           'ftp_manifest', '90R', 'bills/90R/billhistory',            'legis/90R',                         'daily'),
    ('comptroller-spend-fy2024',     'http', NULL, 'resource/2zpi-yjjs.json?$limit=100000',      'comptroller/spend_by_county/fy2024', 'once')
) AS s (source_name, kind, session_code, src_path, sink_path, schedule)
ON t.source_name = s.source_name
WHEN MATCHED THEN UPDATE SET kind = s.kind, session_code = s.session_code, src_path = s.src_path,
                             sink_path = s.sink_path, schedule = s.schedule, updated_utc = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (source_name, kind, session_code, src_path, sink_path, schedule)
                      VALUES (s.source_name, s.kind, s.session_code, s.src_path, s.sink_path, s.schedule);
GO

-- reports/ has per-author/committee subfolders: take only the summary pages (passed.htm, signedbygov.htm, ...).
UPDATE ctl.ingest_source SET file_pattern = '*.htm', recursive = 0 WHERE source_name = 'legis-89R-reports';
UPDATE ctl.ingest_source SET file_pattern = '*', recursive = 0 WHERE kind = 'http';
GO
