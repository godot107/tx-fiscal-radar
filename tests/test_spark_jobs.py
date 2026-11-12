"""Local Spark run of the bronze → silver → gold transform on real 89R files (Parquet instead of Delta)."""
import json
import shutil
from pathlib import Path

import pytest

pyspark = pytest.importorskip("pyspark")
from pyspark.sql import SparkSession  # noqa: E402

from txfr import spark_jobs  # noqa: E402

FX = Path(__file__).parent / "fixtures"
JAVA_OPTS = "--add-opens=java.base/java.nio=ALL-UNNAMED --add-opens=java.base/sun.nio.ch=ALL-UNNAMED -Dio.netty.tryReflectionSetAccessible=true"


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    root = tmp_path_factory.mktemp("lake")
    b = root / "bronze"
    hist = b / "legis/89R/billhistory/house_bills/HB00001_HB00099"
    notes = b / "legis/89R/fiscalnotes/house_bills/HB00001_HB00099"
    hist.mkdir(parents=True)
    notes.mkdir(parents=True)
    for f in (FX / "billhistory").glob("*.xml"):
        shutil.copy(f, hist / f.name.replace("HB", "HB "))
    for f in (FX / "fiscalnotes" / n for n in ("HB00002F.htm", "HB00002I.htm", "HB00010I.htm")):
        shutil.copy(f, notes / f.name)
    (notes / "HB00099X.htm").write_text("<html>not a fiscal note</html>")  # → quarantine
    spend = b / "comptroller/spend_by_county/fy2024"
    spend.mkdir(parents=True)
    rows = [
        {"fiscal_year": "2024", "agency_number": "701", "agency_name": "TEXAS EDUCATION AGENCY",
         "major_spending_category": "INTERGOVERNMENTAL PAYMENTS", "county": c, "amount": a}
        for c, a in (("TRAVIS", "100.50"), ("HARRIS", "200.25"))
    ]
    rows.append({"amount": "301.25"})   # the source's grand-total row: no agency
    (spend / "data.json").write_text(json.dumps(rows))
    return {k: str(root / k) for k in ("bronze", "silver", "gold")}


@pytest.fixture(scope="module")
def spark(tmp_path_factory):
    import os
    import sys
    import zipfile

    os.environ["PYSPARK_PYTHON"] = sys.executable
    # Spark 3.5's Arrow needs Java 8/11/17 (not 21). Use a local JDK 17 if there is one (CI sets up Java 17).
    jdk = sorted(Path.home().glob(".local/jdk/jdk-17*"))
    if jdk and "TXFR_KEEP_JAVA" not in os.environ:
        os.environ["JAVA_HOME"] = str(jdk[-1])
    # the driver JVM is launched by spark-submit before builder configs apply: pass the Arrow flags at launch
    os.environ["PYSPARK_SUBMIT_ARGS"] = f'--driver-java-options "{JAVA_OPTS}" pyspark-shell'
    # Ship the package to the workers the same way the Synapse notebook does (addPyFile of a zip).
    z = tmp_path_factory.mktemp("libs") / "txfr.zip"
    src = Path(__file__).resolve().parents[1] / "src"
    with zipfile.ZipFile(z, "w") as zf:
        for f in (src / "txfr").glob("*.py"):
            zf.write(f, f"txfr/{f.name}")
    s = (SparkSession.builder.master("local[2]").appName("txfr-test")
         .config("spark.sql.shuffle.partitions", "2").config("spark.ui.enabled", "false")
         # Arrow (mapInPandas) on Java 17+: managed runtimes (Synapse, Databricks) set these already
         .config("spark.driver.extraJavaOptions", JAVA_OPTS).config("spark.executor.extraJavaOptions", JAVA_OPTS)
         .getOrCreate())
    s.sparkContext.addPyFile(str(z))
    yield s
    s.stop()


@pytest.fixture(scope="module")
def summary(spark, lake):
    return spark_jobs.run(spark, lake, fmt="parquet", full=True)


def test_counts(summary):
    assert summary["parsed"]["bill"] == 3
    assert summary["parsed"]["fiscal_note"] == 3
    assert summary["parsed"]["quarantine"] == 1
    assert summary["gold"]["fiscal_note_latest"] == 2   # HB2 (I, F) + HB10 (I)


def test_hb2_latest_note_and_agency_fallback(spark, lake, summary):
    g = lake["gold"]
    latest = {r["bill"]: r for r in spark.read.parquet(f"{g}/fiscal_note_latest").collect()}
    assert latest["HB2"]["version"] == "F" and latest["HB2"]["gr_2yr_net"] == -8_381_506_751
    ag = spark.read.parquet(f"{g}/fiscal_note_agency").filter("bill = 'HB2'").collect()
    assert len(ag) == 9  # enrolled note lists none → taken from the introduced note


def test_drift(spark, lake, summary):
    d = spark.read.parquet(f"{lake['gold']}/fiscal_note_drift").filter("bill = 'HB2'").first()
    assert d["first_version"] == "I" and d["latest_version"] == "F" and d["n_versions"] == 2
    assert round(d["latest_gr_2yr_net"] / d["first_gr_2yr_net"] - 1, 3) == 0.107


def test_bill_status(spark, lake, summary):
    st = {r["bill"]: r["status"] for r in spark.read.parquet(f"{lake['gold']}/bill").collect()}
    assert st == {"HB2": "enacted", "HB1": "in committee", "HB10": "in committee"}


def test_spend_rolls_up_counties_and_drops_total_row(spark, lake, summary):
    rows = spark.read.parquet(f"{lake['gold']}/agency_spend").collect()
    assert len(rows) == 1 and rows[0]["agency_code"] == "701" and rows[0]["amount"] == 300.75
    assert summary["spend"]["check_vs_total_row"]["difference"] == -0.5
