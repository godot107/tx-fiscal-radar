"""Shared helpers: project paths, .env loading, Azure SQL (Entra) connection, ADLS client."""
from __future__ import annotations

import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_env(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from .env (written by infra/env.sh) into os.environ, without overriding."""
    p = path or ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def connect_sql(retries: int = 6, wait: int = 20):
    """Azure SQL with Entra auth (az login). The free-offer database auto-pauses after 60 min idle;
    the first connection wakes it and can time out, so retry for ~2 minutes."""
    import mssql_python

    load_env()
    conn_str = (f"Server=tcp:{os.environ['SQL_SERVER']},1433;Database={os.environ['SQL_DATABASE']};"
                "Encrypt=yes;TrustServerCertificate=no;Authentication=ActiveDirectoryDefault")
    for attempt in range(retries):
        try:
            return mssql_python.connect(conn_str)
        except mssql_python.exceptions.OperationalError:
            if attempt == retries - 1:
                raise
            print(f"SQL not reachable yet (auto-paused database resuming?): retry {attempt + 1}/{retries - 1}")
            time.sleep(wait)


def lake_client():
    """ADLS Gen2 service client using the az login identity (shared keys are disabled on the account)."""
    from azure.identity import DefaultAzureCredential
    from azure.storage.filedatalake import DataLakeServiceClient

    load_env()
    return DataLakeServiceClient(os.environ["LAKE_DFS_ENDPOINT"], credential=DefaultAzureCredential())
