"""Generate the Data Factory pipeline/trigger JSON in adf/pipeline and adf/trigger (ADF git-integration layout).

The JSON files are the deployable artifacts and are committed; this script keeps the repeated parts
(logging, failure handling, copy settings) consistent. Edit here, run `python adf/_generate.py`, review the diff,
then `adf/deploy.sh`.
"""
import json
from pathlib import Path

HERE = Path(__file__).parent


def expr(v):
    return {"value": v, "type": "Expression"}


def ref(name, kind="DatasetReference", **params):
    r = {"referenceName": name, "type": kind}
    if params:
        r["parameters"] = params
    return r


def dep(activity, *conds):
    return {"activity": activity, "dependencyConditions": list(conds or ["Succeeded"])}


SQL = {"referenceName": "ls_sql", "type": "LinkedServiceReference"}
COPY_POLICY = {"timeout": "0.02:00:00", "retry": 2, "retryIntervalInSeconds": 120}
SP_POLICY = {"timeout": "0.00:10:00", "retry": 2, "retryIntervalInSeconds": 60}
P = "@pipeline().parameters."


def sp(name, proc, params, depends):
    return {"name": name, "type": "SqlServerStoredProcedure", "dependsOn": depends, "policy": SP_POLICY,
            "linkedServiceName": SQL,
            "typeProperties": {"storedProcedureName": proc,
                               "storedProcedureParameters": {k: {"value": v, "type": t} for k, (v, t) in params.items()}}}


def log_params(status, window_end, files=None, error=None):
    p = {
        "pipeline_run_id": (expr(P + "parent_run_id"), "String"),
        "source_id": (expr(P + "source_id"), "Int32"),
        "window_start_utc": (expr(P + "watermark_utc"), "DateTime"),
        "window_end_utc": (expr(window_end), "DateTime"),
        "status": (status, "String"),
    }
    if files:
        p.update(files)
    if error:
        p["error_message"] = (expr(error), "String")
    return p


def copy_counts(copy):
    o = f"activity('{copy}').output"
    return {"files_read": (expr(f"@coalesce({o}?.filesRead, 0)"), "Int32"),
            "files_written": (expr(f"@coalesce({o}?.filesWritten, 0)"), "Int32"),
            "bytes_read": (expr(f"@coalesce({o}?.dataRead, 0)"), "Int64")}


def fail(name, after, msg_expr, code):
    return {"name": name, "type": "Fail", "dependsOn": [dep(after)],
            "typeProperties": {"message": expr(msg_expr), "errorCode": code}}


def ftp_copy(name, src_folder_expr, sink_folder_expr, pattern_expr="*", recursive_expr="@bool(true)", depends=None,
             description=""):
    """Binary FTP → lake copy. No modified-date filter: the Legislature FTP server doesn't report LastModified."""
    return {
        "name": name, "type": "Copy", "policy": COPY_POLICY, "description": description,
        "dependsOn": depends or [],
        "inputs": [ref("ds_ftp_folder", folder=expr(src_folder_expr))],
        "outputs": [ref("ds_lake_binary", container="bronze", folder=expr(sink_folder_expr), file="")],
        "typeProperties": {
            "source": {"type": "BinarySource", "storeSettings": {
                "type": "FtpReadSettings", "maxConcurrentConnections": 2,   # polite to the state server
                "recursive": expr(recursive_expr), "wildcardFileName": expr(pattern_expr) if pattern_expr.startswith("@") else pattern_expr,
                "useBinaryTransfer": True, "disableChunking": False}},
            "sink": {"type": "BinarySink", "storeSettings": {"type": "AzureBlobFSWriteSettings", "copyBehavior": "PreserveHierarchy"}},
            "enableStaging": False, "dataIntegrationUnits": 2}}


SOURCE_PARAMS = ["parent_run_id", "source_id", "source_name", "kind", "session_code", "src_path", "sink_path",
                 "file_pattern", "recursive", "watermark_utc", "window_end_utc"]


def pl_ingest_source():
    full = ftp_copy("copy_ftp", P + "src_path", P + "sink_path", P + "file_pattern",
                    "@equals(pipeline().parameters.recursive, 1)",
                    description="Full copy of one FTP folder (backfill or weekly reconcile).")
    ftp_case = [
        full,
        sp("log_ok_ftp", "ctl.usp_log_ingest", log_params("Succeeded", P + "window_end_utc", copy_counts("copy_ftp")), [dep("copy_ftp")]),
        sp("log_fail_ftp", "ctl.usp_log_ingest", log_params("Failed", P + "window_end_utc", error="@string(activity('copy_ftp').error?.message)"), [dep("copy_ftp", "Failed")]),
        fail("fail_ftp", "log_fail_ftp", "@concat(pipeline().parameters.source_name, ': ', string(activity('copy_ftp').error?.message))", "INGEST_COPY_FAILED"),
    ]
    get_token = {
        "name": "get_socrata_token", "type": "WebActivity",
        "description": "Optional Socrata app token from Key Vault (managed identity; output hidden from run history). 'anonymous' = none.",
        "policy": {"timeout": "0.00:05:00", "retry": 1, "retryIntervalInSeconds": 30, "secureOutput": True},
        "typeProperties": {"method": "GET", "url": "${KEY_VAULT_URI}secrets/socrata-app-token?api-version=7.4",
                           "authentication": {"type": "MSI", "resource": "https://vault.azure.net"}}}
    token = "activity('get_socrata_token').output.value"
    copy_http = {
        "name": "copy_http", "type": "Copy", "policy": {**COPY_POLICY, "secureInput": True},
        "description": "Full refresh of a Socrata dataset as one JSON file.",
        "dependsOn": [dep("get_socrata_token")],
        "inputs": [ref("ds_http_file", relative_url=expr(P + "src_path"))],
        "outputs": [ref("ds_lake_binary", container="bronze", folder=expr(P + "sink_path"), file="data.json")],
        "typeProperties": {
            "source": {"type": "BinarySource", "storeSettings": {
                "type": "HttpReadSettings", "requestMethod": "GET", "requestTimeout": "00:05:00",
                "additionalHeaders": expr(f"@if(equals({token}, 'anonymous'), '', concat('X-App-Token: ', {token}))")}},
            "sink": {"type": "BinarySink", "storeSettings": {"type": "AzureBlobFSWriteSettings"}},
            "enableStaging": False}}
    http_case = [
        get_token, copy_http,
        sp("log_ok_http", "ctl.usp_log_ingest", log_params("Succeeded", P + "window_end_utc", copy_counts("copy_http")), [dep("copy_http")]),
        sp("log_fail_http", "ctl.usp_log_ingest", log_params("Failed", P + "window_end_utc", error="@string(activity('copy_http').error?.message)"), [dep("copy_http", "Failed")]),
        fail("fail_http", "log_fail_http", "@concat(pipeline().parameters.source_name, ': ', string(activity('copy_http').error?.message))", "INGEST_COPY_FAILED"),
    ]
    manifest_case = [{
        "name": "run_manifest", "type": "ExecutePipeline",
        "typeProperties": {"pipeline": ref("pl_ingest_manifest", "PipelineReference"), "waitOnCompletion": True,
                           "parameters": {k: expr(P + k) for k in SOURCE_PARAMS}}}]
    return {"name": "pl_ingest_source", "properties": {
        "description": "Ingest one control-table source into bronze and log it (watermark advances only on success). Called by pl_ingest.",
        "folder": {"name": "ingest"},
        "parameters": {k: {"type": "int" if k in ("source_id", "recursive") else "string"} for k in SOURCE_PARAMS},
        "activities": [{
            "name": "route_by_kind", "type": "Switch",
            "typeProperties": {"on": expr(P + "kind"),
                               "cases": [{"value": "ftp", "activities": ftp_case},
                                         {"value": "http", "activities": http_case},
                                         {"value": "ftp_manifest", "activities": manifest_case}],
                               "defaultActivities": [{"name": "unknown_kind", "type": "Fail", "typeProperties": {
                                   "message": expr("@concat('unknown source kind: ', pipeline().parameters.kind)"), "errorCode": "BAD_KIND"}}]}}]}}


def pl_manifest_apply():
    copy_manifest = {
        "name": "copy_manifest", "type": "Copy", "policy": COPY_POLICY,
        "description": "Load the Legislature's change manifest (history_periodic.xml) into stg.legis_manifest.",
        "inputs": [ref("ds_ftp_xml", folder=expr(P + "src_path"), file="history_periodic.xml")],
        "outputs": [ref("ds_sql", schema="stg", table="legis_manifest")],
        "typeProperties": {
            "source": {"type": "XmlSource", "storeSettings": {"type": "FtpReadSettings", "recursive": False, "useBinaryTransfer": True},
                       "formatSettings": {"type": "XmlReadSettings", "validationMode": "none", "detectDataType": False, "namespaces": True}},
            "sink": {"type": "AzureSqlSink", "writeBehavior": "insert",
                     "preCopyScript": expr("@concat('DELETE FROM stg.legis_manifest WHERE source_id = ', string(pipeline().parameters.source_id), ' OR source_id IS NULL')")},
            "translator": {"type": "TabularTranslator", "collectionReference": "$['bills']['bill']",
                           "mappings": [{"source": {"path": "['@id']"}, "sink": {"name": "bill_label"}},
                                        {"source": {"path": "['@path']"}, "sink": {"name": "file_path"}},
                                        {"source": {"path": "['@timestamp']"}, "sink": {"name": "ts_text"}}]},
            "enableStaging": False}}
    lookup = {
        "name": "lookup_changes", "type": "Lookup", "dependsOn": [dep("copy_manifest")], "policy": SP_POLICY,
        "description": "Changed 100-bill folders since the watermark (and the newest manifest timestamp).",
        "typeProperties": {"source": {"type": "AzureSqlSource", "sqlReaderStoredProcedureName": "ctl.usp_manifest_changes",
                                      "storedProcedureParameters": {"source_id": {"type": "Int32", "value": expr(P + "source_id")}}},
                           "dataset": ref("ds_sql"), "firstRowOnly": False}}
    changes = "activity('lookup_changes').output.value"
    foreach = {
        "name": "for_each_group", "type": "ForEach", "dependsOn": [dep("lookup_changes")],
        "typeProperties": {"items": expr(f"@{changes}"), "isSequential": False, "batchCount": 2,
                           "activities": [{"name": "copy_group", "type": "ExecutePipeline", "typeProperties": {
                               "pipeline": ref("pl_copy_group", "PipelineReference"), "waitOnCompletion": True,
                               "parameters": {"session_code": expr(P + "session_code"),
                                              "chamber_folder": expr("@item().chamber_folder"),
                                              "group_folder": expr("@item().group_folder")}}}]}}
    new_wm = f"@if(empty({changes}), pipeline().parameters.watermark_utc, first({changes}).run_max_ts)"
    groups = {"files_read": (expr(f"@length({changes})"), "Int32")}
    return {"name": "pl_manifest_apply", "properties": {
        "description": "Manifest has entries: load it into SQL → changed folders → re-copy only those (bill history + fiscal notes). Watermark = newest manifest timestamp (source clock).",
        "folder": {"name": "ingest"},
        "parameters": {k: {"type": "int" if k in ("source_id", "recursive") else "string"} for k in SOURCE_PARAMS},
        "activities": [
            copy_manifest, lookup, foreach,
            sp("log_ok", "ctl.usp_log_ingest", log_params("Succeeded", new_wm, groups), [dep("for_each_group")]),
            sp("log_fail", "ctl.usp_log_ingest", log_params("Failed", P + "watermark_utc",
               error="@concat('manifest ingest failed: ', coalesce(activity('copy_manifest').error?.message, activity('lookup_changes').error?.message, activity('for_each_group').error?.message, 'see run'))"),
               [{"activity": "for_each_group", "dependencyConditions": ["Failed", "Skipped"]}]),
            fail("fail_manifest", "log_fail", "@concat(pipeline().parameters.source_name, ': manifest ingest failed')", "INGEST_MANIFEST_FAILED"),
        ]}}


def pl_ingest_manifest():
    """Entry point for kind=ftp_manifest. history_periodic.xml is a rolling 'changes today' list that resets to an
    empty <bills /> after midnight Central (observed 2026-11-10); ADF cannot infer a schema from an empty XML, so
    check the size first and log 'no changes' instead of failing. (Schedule: tr_daily runs 11:30 PM Central.)"""
    meta = {"name": "manifest_size", "type": "GetMetadata", "policy": SP_POLICY,
            "typeProperties": {"dataset": ref("ds_ftp_file", folder=expr(P + "src_path"), file="history_periodic.xml"),
                               "fieldList": ["size"],
                               "storeSettings": {"type": "FtpReadSettings", "useBinaryTransfer": True},
                               "formatSettings": {"type": "BinaryReadSettings"}}}
    apply = {"name": "apply_manifest", "type": "ExecutePipeline",
             "typeProperties": {"pipeline": ref("pl_manifest_apply", "PipelineReference"), "waitOnCompletion": True,
                                "parameters": {k: expr(P + k) for k in SOURCE_PARAMS}}}
    no_change = sp("log_no_change", "ctl.usp_log_ingest",
                   log_params("Succeeded", P + "watermark_utc", {"files_read": ("0", "Int32")}), [])
    return {"name": "pl_ingest_manifest", "properties": {
        "description": "Daily incremental for a live session (kind ftp_manifest): empty manifest → log no change; otherwise pl_manifest_apply.",
        "folder": {"name": "ingest"},
        "parameters": {k: {"type": "int" if k in ("source_id", "recursive") else "string"} for k in SOURCE_PARAMS},
        "activities": [
            meta,
            {"name": "if_entries", "type": "IfCondition", "dependsOn": [dep("manifest_size")],
             "typeProperties": {"expression": expr("@greater(activity('manifest_size').output.size, 100)"),  # empty = 50 bytes
                                "ifTrueActivities": [apply], "ifFalseActivities": [no_change]}},
            sp("log_fail_meta", "ctl.usp_log_ingest", log_params("Failed", P + "watermark_utc",
               error="@string(activity('manifest_size').error?.message)"), [dep("manifest_size", "Failed")]),
            fail("fail_meta", "log_fail_meta", "@concat(pipeline().parameters.source_name, ': manifest unreadable')", "INGEST_MANIFEST_FAILED"),
        ]}}


def pl_copy_group():
    s = P + "session_code"
    hist_src = f"@concat('bills/', {s[1:]}, '/billhistory/', pipeline().parameters.chamber_folder, '/', pipeline().parameters.group_folder)"
    hist_sink = f"@concat('legis/', {s[1:]}, '/billhistory/', pipeline().parameters.chamber_folder, '/', pipeline().parameters.group_folder)"
    note_src = f"@concat('bills/', {s[1:]}, '/fiscalnotes/html/', pipeline().parameters.chamber_folder, '/', pipeline().parameters.group_folder)"
    note_sink = f"@concat('legis/', {s[1:]}, '/fiscalnotes/', pipeline().parameters.chamber_folder, '/', pipeline().parameters.group_folder)"
    check = {"name": "notes_exist", "type": "GetMetadata", "policy": SP_POLICY,
             "typeProperties": {"dataset": ref("ds_ftp_folder", folder=expr(note_src)), "fieldList": ["exists"],
                                "storeSettings": {"type": "FtpReadSettings", "useBinaryTransfer": True},
                                "formatSettings": {"type": "BinaryReadSettings"}}}
    return {"name": "pl_copy_group", "properties": {
        "description": "Re-copy one 100-bill folder: bill history and (if published yet) fiscal notes.",
        "folder": {"name": "ingest"},
        "parameters": {k: {"type": "string"} for k in ("session_code", "chamber_folder", "group_folder")},
        "activities": [
            ftp_copy("copy_history", hist_src, hist_sink, recursive_expr="@bool(false)"),
            check,
            {"name": "if_notes", "type": "IfCondition", "dependsOn": [dep("notes_exist")],
             "typeProperties": {"expression": expr("@activity('notes_exist').output.exists"),
                                "ifTrueActivities": [ftp_copy("copy_notes", note_src, note_sink, recursive_expr="@bool(false)")]}},
        ]}}


def pl_ingest():
    return {"name": "pl_ingest", "properties": {
        "description": "Metadata-driven ingestion: ctl.usp_sources_due(mode) → pl_ingest_source per source (2 at a time).",
        "folder": {"name": "ingest"},
        "parameters": {"mode": {"type": "string", "defaultValue": "daily"}},
        "variables": {"window_end_utc": {"type": "String"}},
        "activities": [
            {"name": "set_window_end", "type": "SetVariable",
             "typeProperties": {"variableName": "window_end_utc", "value": expr("@formatDateTime(pipeline().TriggerTime, 'yyyy-MM-ddTHH:mm:ssZ')")}},
            {"name": "lookup_sources", "type": "Lookup", "dependsOn": [dep("set_window_end")],
             "policy": {"timeout": "0.00:10:00", "retry": 3, "retryIntervalInSeconds": 60},
             "typeProperties": {"source": {"type": "AzureSqlSource", "sqlReaderStoredProcedureName": "ctl.usp_sources_due",
                                           "storedProcedureParameters": {"mode": {"type": "String", "value": expr(P + "mode")}}},
                                "dataset": ref("ds_sql"), "firstRowOnly": False}},
            {"name": "for_each_source", "type": "ForEach", "dependsOn": [dep("lookup_sources")],
             "typeProperties": {"items": expr("@activity('lookup_sources').output.value"), "isSequential": False, "batchCount": 2,
                                "activities": [{"name": "ingest_one", "type": "ExecutePipeline", "typeProperties": {
                                    "pipeline": ref("pl_ingest_source", "PipelineReference"), "waitOnCompletion": True,
                                    "parameters": {"parent_run_id": expr("@pipeline().RunId"),
                                                   **{k: expr(f"@item().{k}") for k in SOURCE_PARAMS if k not in ("parent_run_id", "window_end_utc")},
                                                   "window_end_utc": expr("@variables('window_end_utc')")}}}]}}]}}


GOLD_TABLES = ["bill", "bill_action", "bill_subject", "fiscal_note_latest", "fiscal_note_version", "fiscal_note_drift",
               "fiscal_note_agency", "fiscal_note_year", "agency_spend", "ai_agency_cost", "ai_mandate"]


def pl_load_sql():
    return {"name": "pl_load_sql", "properties": {
        "description": "Gold Parquet → Azure SQL staging (truncate + insert), then dw.usp_load_all builds the star schema (SCD2 bills).",
        "folder": {"name": "serve"},
        "parameters": {"tables": {"type": "array", "defaultValue": GOLD_TABLES}},
        "activities": [
            {"name": "for_each_table", "type": "ForEach",
             "typeProperties": {"items": expr(P + "tables"), "isSequential": False, "batchCount": 4, "activities": [
                 {"name": "table_exists", "type": "GetMetadata", "policy": SP_POLICY,
                  "typeProperties": {"dataset": ref("ds_lake_parquet", folder=expr("@item()")), "fieldList": ["exists"],
                                     "storeSettings": {"type": "AzureBlobFSReadSettings"}}},
                 {"name": "if_exists", "type": "IfCondition", "dependsOn": [dep("table_exists")],
                  "typeProperties": {"expression": expr("@activity('table_exists').output.exists"), "ifTrueActivities": [{
                      "name": "copy_to_staging", "type": "Copy", "policy": COPY_POLICY,
                      "inputs": [ref("ds_lake_parquet", folder=expr("@item()"))],
                      "outputs": [ref("ds_sql", schema="stg", table=expr("@item()"))],
                      "typeProperties": {
                          "source": {"type": "ParquetSource", "storeSettings": {"type": "AzureBlobFSReadSettings", "recursive": True, "wildcardFileName": "*.parquet"}},
                          "sink": {"type": "AzureSqlSink", "writeBehavior": "insert", "sqlWriterUseTableLock": True,
                                   "preCopyScript": expr("@concat('TRUNCATE TABLE stg.', item())"), "tableOption": "none"},
                          "translator": {"type": "TabularTranslator", "typeConversion": True,
                                         "typeConversionSettings": {"allowDataTruncation": False, "treatBooleanAsNumber": False}},
                          "enableStaging": False}}]}}]}},
            sp("build_star", "dw.usp_load_all", {"pipeline_run_id": (expr("@pipeline().RunId"), "String")}, [dep("for_each_table")]),
        ]}}


def notebook_activity(name="transform", depends=None, maintenance_expr="@bool(false)"):
    return {"name": name, "type": "SynapseNotebook", "dependsOn": depends or [],
            "policy": {"timeout": "0.02:00:00", "retry": 1, "retryIntervalInSeconds": 120},
            "linkedServiceName": {"referenceName": "ls_synapse", "type": "LinkedServiceReference"},
            # Notebook and pool live in Synapse, not in the factory: reference them by expression
            "typeProperties": {"notebook": {"referenceName": expr("nb_transform"), "type": "NotebookReference"},
                               "parameters": {"full": {"value": expr(P + "full_transform"), "type": "bool"},
                                              "maintenance": {"value": expr(maintenance_expr), "type": "bool"}},
                               "sparkPool": {"referenceName": expr("${SPARK_POOL}"), "type": "BigDataPoolReference"},
                               "executorSize": "Small", "driverSize": "Small", "numExecutors": 1}}


def pl_transform():
    return {"name": "pl_transform", "properties": {
        "description": "Only the Spark transform (bronze → silver → gold). full_transform = re-parse every bronze file.",
        "folder": {"name": "transform"},
        "parameters": {"full_transform": {"type": "bool", "defaultValue": False}},
        "activities": [notebook_activity()]}}


def pl_daily():
    return {"name": "pl_daily", "properties": {
        "description": "End to end: ingest (daily or weekly mode) → Synapse Spark transform (bronze → silver → gold) → load Azure SQL.",
        "folder": {"name": "orchestration"},
        "parameters": {"mode": {"type": "string", "defaultValue": "daily"}, "full_transform": {"type": "bool", "defaultValue": False}},
        "activities": [
            {"name": "ingest", "type": "ExecutePipeline",
             "typeProperties": {"pipeline": ref("pl_ingest", "PipelineReference"), "waitOnCompletion": True,
                                "parameters": {"mode": expr(P + "mode")}}},
            notebook_activity("transform", [dep("ingest")], "@equals(pipeline().parameters.mode, 'weekly')"),
            {"name": "load_sql", "type": "ExecutePipeline", "dependsOn": [dep("transform")],
             "typeProperties": {"pipeline": ref("pl_load_sql", "PipelineReference"), "waitOnCompletion": True}},
        ]}}


def triggers():
    def sched(name, desc, freq, hours, mode, days=None, minutes=(0,)):
        rec = {"frequency": freq, "interval": 1, "startTime": "2026-11-11T00:00:00", "timeZone": "Central Standard Time",
               "schedule": {"hours": hours, "minutes": list(minutes)}}
        if days:
            rec["schedule"]["weekDays"] = days
        return {"name": name, "properties": {
            "description": desc, "runtimeState": "Stopped",
            "type": "ScheduleTrigger", "typeProperties": {"recurrence": rec},
            "pipelines": [{"pipelineReference": ref("pl_daily", "PipelineReference"), "parameters": {"mode": mode}}]}}
    return [
        sched("tr_daily", "Daily 11:30 PM Central: the change manifest (history_periodic.xml) lists today's changes and "
              "resets after midnight, so the incremental must run before then.",
              "Week", [23], "daily", ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"], minutes=[30]),
        sched("tr_weekly", "Sunday 5:00 AM Central: full reconcile copy of the live session (catches anything a manifest "
              "window missed) + transform with Delta OPTIMIZE/VACUUM + load.",
              "Week", [5], "weekly", ["Sunday"]),
    ]


def main():
    for pl in (pl_copy_group(), pl_manifest_apply(), pl_ingest_manifest(), pl_ingest_source(), pl_ingest(), pl_transform(), pl_load_sql(), pl_daily()):
        (HERE / "pipeline" / f"{pl['name']}.json").write_text(json.dumps(pl, indent=2) + "\n")
    for tr in triggers():
        (HERE / "trigger" / f"{tr['name']}.json").write_text(json.dumps(tr, indent=2) + "\n")
    print("generated:", sorted(p.name for p in (HERE / "pipeline").glob("*.json")) + sorted(p.name for p in (HERE / "trigger").glob("*.json")))


if __name__ == "__main__":
    main()
