-- Synapse serverless SQL: a lake database of views over silver (Delta) and gold (Parquet). No data is copied;
-- each query reads the lake and is billed by data processed (~$5/TB; this project's lake is < 1 GB).
-- Run with: python tools/run_serverless.py synapse/sql/serverless.sql   (${LAKE_ACCOUNT} filled from .env)
-- The first batch runs in master; the rest in txfr_lake.

-- @database master
IF DB_ID('txfr_lake') IS NULL CREATE DATABASE txfr_lake COLLATE Latin1_General_100_BIN2_UTF8;
GO
-- @database txfr_lake
-- Read the lake as the workspace managed identity (Storage Blob Data Contributor), so callers don't each
-- need storage RBAC. The master key only protects the credential metadata; no secret is stored.
IF NOT EXISTS (SELECT 1 FROM sys.symmetric_keys WHERE name = '##MS_DatabaseMasterKey##')
    CREATE MASTER KEY;
IF NOT EXISTS (SELECT 1 FROM sys.database_scoped_credentials WHERE name = 'workspace_identity')
    CREATE DATABASE SCOPED CREDENTIAL workspace_identity WITH IDENTITY = 'Managed Identity';
IF NOT EXISTS (SELECT 1 FROM sys.external_data_sources WHERE name = 'lake')
    CREATE EXTERNAL DATA SOURCE lake WITH (LOCATION = 'https://${LAKE_ACCOUNT}.dfs.core.windows.net', CREDENTIAL = workspace_identity);
IF NOT EXISTS (SELECT 1 FROM sys.external_file_formats WHERE name = 'parquet')
    CREATE EXTERNAL FILE FORMAT parquet WITH (FORMAT_TYPE = PARQUET, DATA_COMPRESSION = 'org.apache.hadoop.io.compress.SnappyCodec');
GO
IF SCHEMA_ID('silver') IS NULL EXEC('CREATE SCHEMA silver');
IF SCHEMA_ID('gold') IS NULL EXEC('CREATE SCHEMA gold');
GO
-- silver: Delta tables written by nb_transform (MERGE by business key)
CREATE OR ALTER VIEW silver.bill AS SELECT * FROM OPENROWSET(BULK 'silver/bill/', DATA_SOURCE = 'lake', FORMAT = 'DELTA') AS r;
GO
CREATE OR ALTER VIEW silver.fiscal_note AS
SELECT session, bill, version, version_label, note_date, gr_2yr_net, gr_2yr_kind, no_appropriation,
       cannot_be_determined, local_gov_significant, n_agencies, source_path, source_modified
FROM OPENROWSET(BULK 'silver/fiscal_note/', DATA_SOURCE = 'lake', FORMAT = 'DELTA') AS r;
GO
CREATE OR ALTER VIEW silver.quarantine AS SELECT * FROM OPENROWSET(BULK 'silver/quarantine/', DATA_SOURCE = 'lake', FORMAT = 'DELTA') AS r;
GO
-- gold: Parquet, the same tables Data Factory loads into Azure SQL
CREATE OR ALTER VIEW gold.bill AS SELECT * FROM OPENROWSET(BULK 'gold/bill/*.parquet', DATA_SOURCE = 'lake', FORMAT = 'PARQUET') AS r;
GO
CREATE OR ALTER VIEW gold.fiscal_note_latest AS SELECT * FROM OPENROWSET(BULK 'gold/fiscal_note_latest/*.parquet', DATA_SOURCE = 'lake', FORMAT = 'PARQUET') AS r;
GO
CREATE OR ALTER VIEW gold.fiscal_note_agency AS SELECT * FROM OPENROWSET(BULK 'gold/fiscal_note_agency/*.parquet', DATA_SOURCE = 'lake', FORMAT = 'PARQUET') AS r;
GO
CREATE OR ALTER VIEW gold.agency_spend AS SELECT * FROM OPENROWSET(BULK 'gold/agency_spend/*.parquet', DATA_SOURCE = 'lake', FORMAT = 'PARQUET') AS r;
GO
-- Ad-hoc exploration straight on bronze, no table needed: how many raw files per session and folder?
CREATE OR ALTER VIEW gold.v_bronze_inventory AS
SELECT r.filepath(1) AS session, r.filepath(2) AS folder, COUNT(*) AS files
FROM OPENROWSET(BULK 'bronze/legis/*/*/*/*/*', DATA_SOURCE = 'lake', FORMAT = 'CSV', PARSER_VERSION = '2.0',
                FIELDTERMINATOR = '0x0b', FIELDQUOTE = '0x0e', ROWTERMINATOR = '0x0c')
     WITH (doc varchar(8000)) AS r
GROUP BY r.filepath(1), r.filepath(2);
GO
-- Funnel per session, computed in serverless SQL
CREATE OR ALTER VIEW gold.v_session_funnel AS
SELECT session, COUNT(*) AS filed,
       SUM(CASE WHEN referred_date IS NOT NULL THEN 1 ELSE 0 END) AS referred,
       SUM(CASE WHEN passed_house_date IS NOT NULL OR passed_senate_date IS NOT NULL THEN 1 ELSE 0 END) AS passed_one_chamber,
       SUM(CASE WHEN enrolled_date IS NOT NULL THEN 1 ELSE 0 END) AS passed_both,   -- enrolled = identical text agreed
       SUM(CASE WHEN enacted = 1 THEN 1 ELSE 0 END) AS enacted,
       SUM(CASE WHEN vetoed_date IS NOT NULL THEN 1 ELSE 0 END) AS vetoed
FROM gold.bill GROUP BY session;
GO
-- @cetas gold/serverless/session_funnel
-- CETAS: persist the funnel as Parquet in the lake (tools/run_serverless.py empties the folder first;
-- CETAS refuses to write into a non-empty location).
IF OBJECT_ID('gold.session_funnel_snapshot') IS NOT NULL DROP EXTERNAL TABLE gold.session_funnel_snapshot;
CREATE EXTERNAL TABLE gold.session_funnel_snapshot
WITH (LOCATION = 'gold/serverless/session_funnel/', DATA_SOURCE = lake, FILE_FORMAT = parquet)
AS SELECT *, SYSUTCDATETIME() AS snapshot_utc FROM gold.v_session_funnel;
GO
