# Databricks notebook source
# MAGIC %md
# MAGIC # nb_transform on Databricks (serverless): the same `txfr` Spark job as Synapse
# MAGIC Comparison run for tx-fiscal-radar. Reads a bronze sample from a Unity Catalog volume, runs
# MAGIC `txfr.spark_jobs.run` unchanged, writes Delta silver and Parquet gold back to the volume.
# MAGIC Serverless compute is Spark Connect: no RDD API, no `sc.addPyFile`, which is why the job uses `mapInPandas`
# MAGIC and the package is shipped with `spark.addArtifacts`.

# COMMAND ----------

import json
import sys

ROOT = "/Volumes/workspace/txfr/lake"
ZIP = f"{ROOT}/libs/txfr.zip"
sys.path.insert(0, ZIP)                    # driver
spark.addArtifacts(ZIP, pyfile=True)       # Python workers (Spark Connect)
from txfr import spark_jobs

lake = {z: f"{ROOT}/{z}" for z in ("bronze", "silver", "gold")}
summary = spark_jobs.run(spark, lake, fmt="delta", full=True)

# COMMAND ----------

dbutils.notebook.exit(json.dumps(summary, default=str))
