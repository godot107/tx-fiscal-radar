"""Bronze → silver → gold transforms (PySpark). Runs in the Synapse notebook `nb_transform` and locally in tests.

Layout (ADLS Gen2 containers):
    bronze/legis/<session>/billhistory/<chamber>_bills/<group>/HB 1.xml      raw files, as copied by Data Factory
    bronze/legis/<session>/fiscalnotes/<chamber>_bills/<group>/HB00001I.htm
    bronze/comptroller/spend_by_county/fy2024/data.json
    silver/<table>   Delta (Synapse) or Parquet (local tests): one row per parsed entity, quarantine for failures
    gold/<table>     Parquet, flattened for Data Factory → Azure SQL staging and for serverless SQL views

Silver is rebuilt incrementally: only bronze files modified after the newest `source_modified` already in
silver are parsed, then MERGEd by business key (Delta). Gold is recomputed from silver every run (small).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql import types as T

from txfr.bill_history import parse_bill_history
from txfr.fiscal_note import parse_fiscal_note

# ------------------------------------------------------------------ schemas (explicit: no inference drift)
BILL = T.StructType([
    T.StructField("session", T.StringType()), T.StructField("bill", T.StringType()),
    T.StructField("bill_type", T.StringType()), T.StructField("bill_number", T.IntegerType()),
    T.StructField("caption", T.StringType()), T.StructField("authors", T.StringType()),
    T.StructField("sponsors", T.StringType()),
    T.StructField("house_committee", T.StringType()), T.StructField("house_committee_status", T.StringType()),
    T.StructField("senate_committee", T.StringType()), T.StructField("senate_committee_status", T.StringType()),
    T.StructField("filed_date", T.DateType()), T.StructField("referred_date", T.DateType()),
    T.StructField("passed_house_date", T.DateType()), T.StructField("passed_senate_date", T.DateType()),
    T.StructField("enrolled_date", T.DateType()),
    T.StructField("sent_to_governor_date", T.DateType()), T.StructField("signed_date", T.DateType()),
    T.StructField("vetoed_date", T.DateType()), T.StructField("filed_without_signature_date", T.DateType()),
    T.StructField("effective_date", T.DateType()), T.StructField("enacted", T.BooleanType()),
    T.StructField("status", T.StringType()), T.StructField("last_action", T.StringType()),
    T.StructField("n_actions", T.IntegerType()),
    T.StructField("source_path", T.StringType()), T.StructField("source_modified", T.TimestampType()),
])
ACTION = T.StructType([
    T.StructField("session", T.StringType()), T.StructField("bill", T.StringType()),
    T.StructField("seq", T.IntegerType()), T.StructField("action_number", T.StringType()),
    T.StructField("chamber", T.StringType()), T.StructField("action_date", T.DateType()),
    T.StructField("description", T.StringType()), T.StructField("comment", T.StringType()),
])
SUBJECT = T.StructType([
    T.StructField("session", T.StringType()), T.StructField("bill", T.StringType()),
    T.StructField("subject", T.StringType()), T.StructField("subject_code", T.StringType()),
    T.StructField("is_agency", T.BooleanType()),
])
NOTE = T.StructType([
    T.StructField("session", T.StringType()), T.StructField("bill", T.StringType()),
    T.StructField("version", T.StringType()), T.StructField("version_rank", T.IntegerType()),
    T.StructField("version_label", T.StringType()), T.StructField("note_date", T.DateType()),
    T.StructField("gr_2yr_net", T.DoubleType()), T.StructField("gr_2yr_kind", T.StringType()),
    T.StructField("no_appropriation", T.BooleanType()), T.StructField("cannot_be_determined", T.BooleanType()),
    T.StructField("local_gov_significant", T.BooleanType()), T.StructField("n_agencies", T.IntegerType()),
    T.StructField("summary", T.StringType()), T.StructField("fiscal_analysis", T.StringType()),
    T.StructField("methodology", T.StringType()), T.StructField("technology", T.StringType()),
    T.StructField("local_gov", T.StringType()),
    T.StructField("source_path", T.StringType()), T.StructField("source_modified", T.TimestampType()),
])
NOTE_AGENCY = T.StructType([
    T.StructField("session", T.StringType()), T.StructField("bill", T.StringType()),
    T.StructField("version", T.StringType()), T.StructField("agency_code", T.StringType()),
    T.StructField("agency_name", T.StringType()),
])
NOTE_AMOUNT = T.StructType([
    T.StructField("session", T.StringType()), T.StructField("bill", T.StringType()),
    T.StructField("version", T.StringType()), T.StructField("table_name", T.StringType()),  # gr | all_funds
    T.StructField("fiscal_year", T.IntegerType()), T.StructField("line_item", T.StringType()),
    T.StructField("amount", T.DoubleType()), T.StructField("fte", T.DoubleType()),
])
QUARANTINE = T.StructType([
    T.StructField("source_path", T.StringType()), T.StructField("kind", T.StringType()),
    T.StructField("error", T.StringType()), T.StructField("source_modified", T.TimestampType()),
    T.StructField("quarantined_utc", T.TimestampType()),
])

# Version order of a bill's fiscal notes: introduced → committee reports → engrossed → as passed 2nd house →
# conference committee report → enrolled
VERSION_RANK = {"I": 1, "H": 2, "S": 3, "E": 4, "A": 5, "C": 6, "F": 7}   # C = conference committee report


def _d(s):
    return datetime.strptime(s, "%Y-%m-%d").date() if s else None


def bill_status(b: dict) -> str:
    """One status per bill for the funnel, furthest stage reached."""
    if b.get("vetoed_date"):
        return "vetoed"
    if b.get("enacted"):
        return "enacted"
    if b.get("sent_to_governor_date"):
        return "sent to governor"
    if b.get("enrolled_date"):
        return "passed both chambers"   # identical text agreed by both (enrolled), as the Legislature counts it
    if b.get("passed_house_date") or b.get("passed_senate_date"):
        return "passed one chamber"
    if b.get("referred_date"):
        return "in committee"
    return "filed"


def _decode(content: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _ts(v) -> str | None:
    """Timestamp → 'yyyy-MM-ddTHH:mm:ss' (what from_json parses by default on every Spark runtime)."""
    if v is None:
        return None
    return v.strftime("%Y-%m-%dT%H:%M:%S") if hasattr(v, "strftime") else str(v)[:19]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def history_records(path: str, mod, content: bytes):
    """One bronze bill-history file → (tag, dict) records: 'bill', 'action', 'subject' or 'q' (quarantine)."""
    try:
        b, actions = parse_bill_history(_decode(content), path)
    except Exception as e:  # noqa: BLE001 - every failure goes to quarantine, never kills the batch
        yield "q", {"source_path": path, "kind": "billhistory", "error": f"{type(e).__name__}: {e}"[:1000],
                    "source_modified": _ts(mod), "quarantined_utc": _now()}
        return
    ch = b["committees"]
    rec = {k: b[k] for k in ("session", "bill", "bill_type", "bill_number", "caption", "enacted", "last_action", "n_actions")}
    rec.update(authors=" | ".join(b["authors"]), sponsors=" | ".join(b["sponsors"]),
               house_committee=ch.get("house", {}).get("name"), house_committee_status=ch.get("house", {}).get("status"),
               senate_committee=ch.get("senate", {}).get("name"), senate_committee_status=ch.get("senate", {}).get("status"),
               status=bill_status(b), source_path=path, source_modified=_ts(mod))
    for k in ("filed", "referred", "passed_house", "passed_senate", "enrolled", "sent_to_governor", "signed", "vetoed",
              "filed_without_signature", "effective"):
        rec[f"{k}_date"] = b[f"{k}_date"]
    yield "bill", rec
    for a in actions:
        yield "action", a
    for sub in b["subjects"]:
        yield "subject", {"session": b["session"], "bill": b["bill"], "subject": sub["subject"],
                          "subject_code": sub["code"], "is_agency": (sub["code"] or "").startswith("V")}


def note_records(path: str, mod, content: bytes):
    """One bronze fiscal-note file → (tag, dict) records: 'note', 'agency', 'amount' or 'q'."""
    try:
        n = parse_fiscal_note(_decode(content), path)
        if not n["bill"] or not n["session"]:
            raise ValueError("; ".join(n["warnings"]) or "no bill/session")
    except Exception as e:  # noqa: BLE001
        yield "q", {"source_path": path, "kind": "fiscalnote", "error": f"{type(e).__name__}: {e}"[:1000],
                    "source_modified": _ts(mod), "quarantined_utc": _now()}
        return
    if n["gr_2yr_kind"] == "unknown":  # parsed, but the headline figure is unreadable → keep the row, flag it
        yield "q", {"source_path": path, "kind": "fiscalnote", "error": "warning: " + "; ".join(n["warnings"]),
                    "source_modified": _ts(mod), "quarantined_utc": _now()}
    key = {"session": n["session"], "bill": n["bill"], "version": n["version"]}
    yield "note", {**key, "version_rank": VERSION_RANK.get(n["version"], 0), "n_agencies": len(n["agencies"]),
                   "source_path": path, "source_modified": _ts(mod),
                   **{k: n[k] for k in ("version_label", "note_date", "gr_2yr_net", "gr_2yr_kind", "no_appropriation",
                                        "cannot_be_determined", "local_gov_significant", "summary", "fiscal_analysis",
                                        "methodology", "technology", "local_gov")}}
    for a in n["agencies"]:
        yield "agency", {**key, **a}
    for y in n["gr_5yr"]:
        yield "amount", {**key, "table_name": "gr", "fiscal_year": y["fiscal_year"],
                         "line_item": "General Revenue Related Funds", "amount": y["amount"], "fte": None}
    for y in n["all_funds_5yr"]:
        yield "amount", {**key, "table_name": "all_funds", "fiscal_year": y["fiscal_year"], "line_item": y["column"],
                         "amount": y.get("amount"), "fte": y.get("value")}


TAGGED = T.StructType([T.StructField("tag", T.StringType()), T.StructField("payload", T.StringType())])


def _tagger(records_fn):
    """Wrap a per-file record generator as a mapInPandas function (works on classic Spark and Spark Connect:
    Databricks serverless / Fabric have no RDD API, so no rdd.mapPartitions here)."""
    def fn(batches):
        import pandas as pd
        for pdf in batches:
            out = [(tag, json.dumps(rec, default=str))
                   for path, mod, content in zip(pdf["path"], pdf["modificationTime"], pdf["content"])
                   for tag, rec in records_fn(path, mod, content)]
            yield pd.DataFrame(out, columns=["tag", "payload"])
    return fn


def _split(spark: SparkSession, tagged: DataFrame, schemas: dict[str, T.StructType], scratch: str) -> dict[str, DataFrame]:
    """Materialise the tagged records once (Parquet in scratch: parse each file once; no .cache(), which serverless
    runtimes refuse), then one typed DataFrame per tag via from_json."""
    tagged.write.mode("overwrite").parquet(scratch)
    t = spark.read.parquet(scratch)
    return {tag: t.filter(F.col("tag") == tag).select(F.from_json("payload", schema).alias("r")).select("r.*")
            for tag, schema in schemas.items()}


def read_bronze(spark: SparkSession, path: str, glob: str, modified_after: datetime | None) -> DataFrame:
    reader = (spark.read.format("binaryFile").option("recursiveFileLookup", "true").option("pathGlobFilter", glob))
    if modified_after:
        reader = reader.option("modifiedAfter", modified_after.strftime("%Y-%m-%dT%H:%M:%S"))
    try:
        df = reader.load(path)
        _ = df.columns   # Spark Connect analyses lazily: force it so a missing path fails here, not at the first action
        return df
    except Exception as e:  # noqa: BLE001 - path not created yet (no files for this source)
        if "Path does not exist" in str(e) or "PATH_NOT_FOUND" in str(e):
            return spark.createDataFrame([], "path string, modificationTime timestamp, length long, content binary")
        raise


def parse_bronze(spark: SparkSession, bronze: str, modified_after: datetime | None = None,
                 scratch: str | None = None) -> dict[str, DataFrame]:
    """Parse new/changed bronze legislative files into silver-shaped DataFrames."""
    scratch = scratch or f"{bronze}/_scratch"
    hist = read_bronze(spark, f"{bronze}/legis/*/billhistory", "*.xml", modified_after)
    notes = read_bronze(spark, f"{bronze}/legis/*/fiscalnotes", "*.htm", modified_after)
    h = _split(spark, hist.mapInPandas(_tagger(history_records), TAGGED),
               {"bill": BILL, "action": ACTION, "subject": SUBJECT, "q": QUARANTINE}, f"{scratch}/history")
    n = _split(spark, notes.mapInPandas(_tagger(note_records), TAGGED),
               {"note": NOTE, "agency": NOTE_AGENCY, "amount": NOTE_AMOUNT, "q": QUARANTINE}, f"{scratch}/notes")
    return {
        "bill": h["bill"], "bill_action": h["action"], "bill_subject": h["subject"],
        "fiscal_note": n["note"], "fiscal_note_agency": n["agency"], "fiscal_note_amount": n["amount"],
        "quarantine": h["q"].unionByName(n["q"]),
    }


def parse_spend(spark: SparkSession, bronze: str) -> tuple[DataFrame, dict]:
    """Comptroller 'State Expenditures by County' JSON → spend per agency, fiscal year and category.
    The dataset carries one grand-total row with no agency (FY2024: $189.9B). It is excluded from the facts (it would
    double-count everything) and used as a check: the agency rows must add up to it."""
    path = f"{bronze}/comptroller/spend_by_county/*/data.json"
    raw = spark.read.option("multiLine", "true").json(path)
    total_row = raw.filter(F.col("agency_number").isNull()).agg(F.sum(F.col("amount").cast("double"))).first()[0]
    raw = raw.filter(F.col("agency_number").isNotNull())
    agency_sum = raw.agg(F.sum(F.col("amount").cast("double"))).first()[0]
    check = {"total_row": round(total_row or 0, 2), "agency_rows_sum": round(agency_sum or 0, 2),
             "difference": round((agency_sum or 0) - (total_row or 0), 2)}
    return (raw.select(F.col("fiscal_year").cast("int"), F.lpad(F.col("agency_number").cast("string"), 3, "0").alias("agency_code"),
                       F.trim("agency_name").alias("agency_name"),
                       F.trim("major_spending_category").alias("spending_category"), F.col("amount").cast("double"))
               .groupBy("fiscal_year", "agency_code", "agency_name", "spending_category")
               .agg(F.round(F.sum("amount"), 2).alias("amount"))), check


# ------------------------------------------------------------------ silver writes (MERGE by business key)
KEYS = {
    "bill": ["session", "bill"],
    "bill_action": ["session", "bill"],        # replace a bill's actions wholesale
    "bill_subject": ["session", "bill"],
    "fiscal_note": ["session", "bill", "version"],
    "fiscal_note_agency": ["session", "bill", "version"],
    "fiscal_note_amount": ["session", "bill", "version"],
}
REPLACE_CHILDREN = {"bill_action", "bill_subject", "fiscal_note_agency", "fiscal_note_amount"}


def write_silver(spark: SparkSession, frames: dict[str, DataFrame], silver: str, fmt: str = "delta",
                 full: bool = False) -> dict[str, int]:
    """Upsert parsed frames into silver. Delta: MERGE (parents) / delete-then-insert per key (children).
    Parquet (local tests): overwrite. Returns row counts written per table."""
    counts = {}
    if fmt == "delta":   # new columns (e.g. enrolled_date) evolve the silver schema in place on MERGE
        spark.conf.set("spark.databricks.delta.schema.autoMerge.enabled", "true")
    for name, df in frames.items():
        target = f"{silver}/{name}"
        n = df.count()
        counts[name] = n
        if fmt != "delta":
            df.write.mode("overwrite").parquet(target)
            continue
        from delta.tables import DeltaTable  # available on Synapse Spark pools

        if name == "quarantine" or not DeltaTable.isDeltaTable(spark, target):
            # quarantine accumulates across incremental runs; a full rebuild re-parses everything, so it starts over
            mode = "append" if name == "quarantine" and not full else "overwrite"
            if n or not DeltaTable.isDeltaTable(spark, target):
                df.write.format("delta").mode(mode).option("overwriteSchema", "true").save(target)
            continue
        if n == 0:
            continue
        keys = KEYS[name]
        if name not in REPLACE_CHILDREN:
            # MERGE fails if two source rows match one target row (Lee, Delta Lake ch. 4): one row per key, newest file wins
            from pyspark.sql import Window
            w = Window.partitionBy(*keys).orderBy(F.col("source_modified").desc_nulls_last())
            df = df.withColumn("_rn", F.row_number().over(w)).filter("_rn = 1").drop("_rn")
        t = DeltaTable.forPath(spark, target)
        cond = " AND ".join(f"t.{k} = s.{k}" for k in keys)
        if name in REPLACE_CHILDREN:
            # remove every child row of the incoming keys, then insert the new set (handles deleted children)
            t.alias("t").merge(df.select(*keys).distinct().alias("s"), cond).whenMatchedDelete().execute()
            df.write.format("delta").mode("append").save(target)
        else:
            t.alias("t").merge(df.alias("s"), cond).whenMatchedUpdateAll().whenNotMatchedInsertAll().execute()
    return counts


def maintain_silver(spark: SparkSession, silver: str, retain_hours: int = 168) -> dict:
    """Weekly Delta maintenance: daily MERGEs leave many small files (Lee, Delta Lake ch. 5 and 10).
    OPTIMIZE compacts them; VACUUM removes files no longer referenced, keeping 7 days for time travel."""
    from delta.tables import DeltaTable
    done = {}
    for name in list(KEYS) + ["quarantine"]:
        path = f"{silver}/{name}"
        try:
            if not DeltaTable.isDeltaTable(spark, path):
                continue
            t = DeltaTable.forPath(spark, path)
            t.optimize().executeCompaction()
            t.vacuum(retain_hours)
            done[name] = "optimized+vacuumed"
        except Exception as e:  # noqa: BLE001 - maintenance must never fail the load
            done[name] = f"skipped: {str(e)[:120]}"
    return done


def silver_watermark(spark: SparkSession, silver: str, fmt: str = "delta") -> datetime | None:
    """Newest bronze modification time already in silver (bill + fiscal_note)."""
    best = None
    for name in ("bill", "fiscal_note"):
        try:
            v = spark.read.format(fmt).load(f"{silver}/{name}").agg(F.max("source_modified")).first()[0]
        except Exception:  # noqa: BLE001 - first run: table doesn't exist yet
            v = None
        if v and (best is None or v > best):
            best = v
    return best


# ------------------------------------------------------------------ gold
def build_gold(spark: SparkSession, silver: str, fmt: str = "delta") -> dict[str, DataFrame]:
    rd = lambda n: spark.read.format(fmt).load(f"{silver}/{n}")  # noqa: E731
    bill, note, agency, amount = rd("bill"), rd("fiscal_note"), rd("fiscal_note_agency"), rd("fiscal_note_amount")
    subject = rd("bill_subject")

    # Latest fiscal note per bill (highest version rank, then latest date) = the estimate for the bill as it stands
    w = "row_number() over (partition by session, bill order by version_rank desc, note_date desc)"
    latest = note.withColumn("rn", F.expr(w)).filter("rn = 1").drop("rn")
    # Agencies: enrolled notes often leave Source Agencies blank → latest version that lists any
    ag_ver = (note.filter("n_agencies > 0")
              .withColumn("rn", F.expr(w)).filter("rn = 1").select("session", "bill", "version"))
    note_agency = agency.join(ag_ver, ["session", "bill", "version"]).drop("version")

    latest_keys = latest.select("session", "bill", "version")
    note_year = (amount.join(latest_keys, ["session", "bill", "version"])
                 .drop("version"))

    # Version drift: first vs latest two-year figure
    first = note.withColumn("rn", F.expr(w.replace("desc", "asc"))).filter("rn = 1") \
                .select("session", "bill", F.col("version").alias("first_version"),
                        F.col("gr_2yr_net").alias("first_gr_2yr_net"), F.col("note_date").alias("first_note_date"))
    drift = (latest.select("session", "bill", F.col("version").alias("latest_version"),
                           F.col("gr_2yr_net").alias("latest_gr_2yr_net"), F.col("note_date").alias("latest_note_date"))
             .join(first, ["session", "bill"])
             .join(note.groupBy("session", "bill").agg(F.count("*").alias("n_versions")), ["session", "bill"]))

    fiscal_note_latest = latest.select(
        "session", "bill", "version", "version_label", "note_date", "gr_2yr_net", "gr_2yr_kind", "no_appropriation",
        "cannot_be_determined", "local_gov_significant", "n_agencies", "source_path")
    note_versions = note.select("session", "bill", "version", "version_rank", "version_label", "note_date",
                                "gr_2yr_net", "gr_2yr_kind", "source_path")
    def ordered(df, schema, drop=()):
        # gold column order = the declared schema, not silver's physical order (schema evolution appends new
        # columns at the end, and the Parquet → SQL copy must not depend on that)
        return df.select(*[f.name for f in schema.fields if f.name not in drop])

    return {
        "bill": ordered(bill, BILL, drop=("source_modified",)),
        "bill_action": ordered(rd("bill_action"), ACTION),
        "bill_subject": ordered(subject, SUBJECT),
        "fiscal_note_latest": fiscal_note_latest,
        "fiscal_note_version": note_versions,
        "fiscal_note_drift": drift,
        "fiscal_note_agency": note_agency,
        "fiscal_note_year": note_year,
        "fiscal_note_text": latest.select("session", "bill", "version", "summary", "fiscal_analysis",
                                          "methodology", "technology", "local_gov"),
    }


def write_gold(frames: dict[str, DataFrame], gold: str) -> dict[str, int]:
    out = {}
    for name, df in frames.items():
        df.coalesce(1).write.mode("overwrite").parquet(f"{gold}/{name}")
        out[name] = df.count()
    return out


def run(spark: SparkSession, lake_root: dict[str, str], fmt: str = "delta", full: bool = False,
        maintenance: bool = False) -> dict:
    """End-to-end transform. lake_root = {'bronze': uri, 'silver': uri, 'gold': uri}. Returns a run summary.
    maintenance=True (weekly run) compacts and vacuums the silver Delta tables after the load."""
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    wm = None if full else silver_watermark(spark, lake_root["silver"], fmt)
    frames = parse_bronze(spark, lake_root["bronze"], wm, scratch=f"{lake_root['silver']}/_scratch")
    silver_counts = write_silver(spark, frames, lake_root["silver"], fmt, full=full)
    try:
        spend, spend_check = parse_spend(spark, lake_root["bronze"])
        spend_counts = write_silver(spark, {"agency_spend": spend}, lake_root["silver"], "parquet")
        spend_counts["check_vs_total_row"] = spend_check
    except Exception as e:  # noqa: BLE001 - spend source not ingested yet
        spend_counts = {"agency_spend": f"skipped: {str(e)[:200]}"}
    gold = build_gold(spark, lake_root["silver"], fmt)
    try:
        spend_df = spark.read.parquet(f"{lake_root['silver']}/agency_spend")
        _ = spend_df.columns   # force analysis (lazy on Spark Connect)
        gold["agency_spend"] = spend_df
    except Exception:  # noqa: BLE001 - spending not ingested yet
        pass
    gold_counts = write_gold(gold, lake_root["gold"])
    summary = {"started_utc": started, "silver_watermark_in": str(wm), "parsed": silver_counts,
               "spend": spend_counts, "gold": gold_counts}
    if maintenance and fmt == "delta":
        summary["maintenance"] = maintain_silver(spark, lake_root["silver"])
    print(json.dumps(summary, indent=2, default=str))
    return summary
