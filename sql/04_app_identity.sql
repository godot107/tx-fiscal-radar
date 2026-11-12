-- Phase F: the web app's managed identity reads the warehouse, nothing else. ${APP_NAME} from .env. Idempotent.
-- Read-only on dw (the agent's get_metrics runs fixed SELECTs); no access to ctl or stg.
IF USER_ID('${APP_NAME}') IS NULL
    EXEC('CREATE USER [${APP_NAME}] FROM EXTERNAL PROVIDER');
GO
GRANT SELECT ON SCHEMA::dw TO [${APP_NAME}];
GO
