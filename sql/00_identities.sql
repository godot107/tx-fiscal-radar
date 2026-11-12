-- Contained database users for the managed identities (Entra-only server: no SQL logins or passwords).
-- ${DATA_FACTORY} is filled in by tools/run_sql.py from .env. Idempotent.
IF USER_ID('${DATA_FACTORY}') IS NULL
    EXEC('CREATE USER [${DATA_FACTORY}] FROM EXTERNAL PROVIDER');
GO
-- Data Factory: read control/serving tables, write staging, run the control and load procedures.
ALTER ROLE db_datareader ADD MEMBER [${DATA_FACTORY}];
ALTER ROLE db_datawriter ADD MEMBER [${DATA_FACTORY}];
GO
IF SCHEMA_ID('ctl') IS NULL EXEC('CREATE SCHEMA ctl');
IF SCHEMA_ID('stg') IS NULL EXEC('CREATE SCHEMA stg');
IF SCHEMA_ID('dw') IS NULL EXEC('CREATE SCHEMA dw');
GO
GRANT EXECUTE ON SCHEMA::ctl TO [${DATA_FACTORY}];
GRANT EXECUTE ON SCHEMA::dw  TO [${DATA_FACTORY}];
-- Copy activity's "auto create / truncate" on staging needs DDL rights there, and only there.
GRANT ALTER ON SCHEMA::stg TO [${DATA_FACTORY}];
GRANT CREATE TABLE TO [${DATA_FACTORY}];
GO
