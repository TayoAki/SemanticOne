"""Warehouse connectors: run compiled SQL and return rows."""

from __future__ import annotations

import datetime as dt
import decimal
import ipaddress
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import duckdb

from ..config import get_settings
from .demo import ensure_demo_database

# kind -> (label, sqlglot dialect, SQLAlchemy URL schemes, example URL, driver hint)
KINDS: dict[str, dict[str, Any]] = {
    "demo": {
        "label": "Sample warehouse (DuckDB)",
        "dialect": "duckdb",
        "schemes": (),
        "example": "",
        "driver": None,
    },
    "postgres": {
        "label": "PostgreSQL",
        "dialect": "postgres",
        "schemes": ("postgresql", "postgresql+psycopg", "postgres"),
        "example": "postgresql://user:password@host:5432/database",
        "driver": "psycopg",
    },
    "redshift": {
        "label": "Amazon Redshift",
        "dialect": "redshift",
        "schemes": ("postgresql", "postgresql+psycopg", "redshift+psycopg"),
        "example": "postgresql://user:password@cluster.region.redshift.amazonaws.com:5439/dev",
        "driver": "psycopg",
    },
    "snowflake": {
        "label": "Snowflake",
        "dialect": "snowflake",
        "schemes": ("snowflake",),
        "example": "snowflake://user:password@account_identifier/database/schema?warehouse=COMPUTE_WH&role=ANALYST",
        "driver": "snowflake-sqlalchemy (install the `snowflake` extra)",
    },
    "bigquery": {
        "label": "Google BigQuery",
        "dialect": "bigquery",
        "schemes": ("bigquery",),
        "example": "bigquery://project-id/dataset",
        "driver": "sqlalchemy-bigquery (install the `bigquery` extra)",
    },
    "databricks": {
        "label": "Databricks SQL",
        "dialect": "databricks",
        "schemes": ("databricks",),
        "example": "databricks://token:dapi...@adb-123.azuredatabricks.net?http_path=/sql/1.0/warehouses/abc&catalog=main&schema=default",
        "driver": "databricks-sqlalchemy (install the `databricks` extra)",
    },
    "mysql": {
        "label": "MySQL",
        "dialect": "mysql",
        "schemes": ("mysql", "mysql+pymysql"),
        "example": "mysql+pymysql://user:password@host:3306/database",
        "driver": "pymysql (install the `mysql` extra)",
    },
}

# Kinds whose host must be checked against private address ranges.
_HOST_CHECKED = {"postgres", "redshift", "mysql", "databricks"}


class ConnectorError(RuntimeError):
    """A warehouse-side failure. Message is safe to show to the tenant who owns the connection."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    duration_ms: int


def dialect_for(kind: str) -> str:
    return KINDS.get(kind, KINDS["demo"])["dialect"]


def _jsonable(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return int(value) if value == value.to_integral_value() and abs(value) < 2**53 else float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    return value


def _normalize_url(kind: str, url: str) -> str:
    url = url.strip()
    if kind in ("postgres", "redshift"):
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://") :]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://") :]
        elif url.startswith("redshift+psycopg://"):
            url = "postgresql+psycopg://" + url[len("redshift+psycopg://") :]
    if kind == "mysql" and url.startswith("mysql://"):
        url = "mysql+pymysql://" + url[len("mysql://") :]
    return url


def validate_connection_url(kind: str, url: str) -> str:
    """Check a tenant-supplied URL is allowed and return its normalized form."""
    if kind not in KINDS or kind == "demo":
        raise ConnectorError("Unsupported connection type")
    spec = KINDS[kind]
    parts = urlsplit(url.strip())
    if parts.scheme not in spec["schemes"]:
        raise ConnectorError(f"{spec['label']} URLs must start with {' or '.join(s + '://' for s in spec['schemes'])}")
    if kind in _HOST_CHECKED:
        host = parts.hostname
        if not host:
            raise ConnectorError("The connection URL must include a host")
        _check_public_host(host, parts.port)
    return _normalize_url(kind, url)


def _check_public_host(host: str, port: int | None) -> None:
    if get_settings().allow_private_networks:
        return
    try:
        infos = socket.getaddrinfo(host, port or 0, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ConnectorError(f"Cannot resolve host `{host}`") from e
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise ConnectorError(
                f"Host `{host}` resolves to a private address. Private networks are disabled on this deployment."
            )


class DemoConnector:
    kind = "demo"

    def execute(self, sql: str, timeout_seconds: int | None = None) -> QueryResult:
        settings = get_settings()
        path = ensure_demo_database(settings.data_dir)
        config = {
            "enable_external_access": "false",
            "autoinstall_known_extensions": "false",
            "autoload_known_extensions": "false",
            "threads": "2",
            "memory_limit": "512MB",
            "lock_configuration": "true",
        }
        con = duckdb.connect(str(path), read_only=True, config=config)
        # The sample warehouse is shared by every tenant, so keep queries short.
        timeout = min(timeout_seconds or settings.query_timeout_seconds, 20)
        timer = threading.Timer(timeout, con.interrupt)
        start = time.perf_counter()
        timer.start()
        try:
            cursor = con.execute(sql)
            columns = [d[0] for d in cursor.description or []]
            rows = [[_jsonable(v) for v in row] for row in cursor.fetchall()]
        except duckdb.InterruptException as e:
            raise ConnectorError(f"Query exceeded the {timeout}s time limit") from e
        except duckdb.Error as e:
            raise ConnectorError(str(e).splitlines()[0]) from e
        finally:
            timer.cancel()
            con.close()
        return QueryResult(columns=columns, rows=rows, duration_ms=int((time.perf_counter() - start) * 1000))

    def test(self) -> str:
        result = self.execute("SELECT COUNT(*) FROM shop.orders")
        return f"Connected. Sample warehouse has {result.rows[0][0]:,} orders."


_ENGINES: dict[tuple[int, str], Any] = {}
_ENGINES_LOCK = threading.Lock()


class SQLAlchemyConnector:
    def __init__(self, kind: str, url: str, cache_key: tuple[int, str] | None = None):
        self.kind = kind
        self.url = url
        self.cache_key = cache_key

    def _engine(self):
        from sqlalchemy import create_engine

        timeout = get_settings().query_timeout_seconds
        connect_args: dict[str, Any] = {}
        if self.kind in ("postgres", "redshift"):
            connect_args = {"connect_timeout": 10}
            if self.kind == "postgres":
                connect_args["options"] = f"-c statement_timeout={timeout * 1000}"
        elif self.kind == "mysql":
            connect_args = {"connect_timeout": 10, "read_timeout": timeout}

        def build():
            try:
                return create_engine(self.url, pool_pre_ping=True, pool_size=2, max_overflow=2, connect_args=connect_args)
            except ModuleNotFoundError as e:
                raise ConnectorError(
                    f"The {KINDS[self.kind]['label']} driver is not installed on this deployment "
                    f"({KINDS[self.kind]['driver']})"
                ) from e
            except Exception as e:
                raise ConnectorError(f"Invalid connection URL: {type(e).__name__}") from e

        if self.cache_key is None:
            return build()
        with _ENGINES_LOCK:
            engine = _ENGINES.get(self.cache_key)
            if engine is None:
                engine = build()
                _ENGINES[self.cache_key] = engine
            return engine

    def execute(self, sql: str, timeout_seconds: int | None = None) -> QueryResult:
        from sqlalchemy import text
        from sqlalchemy.exc import DBAPIError, SQLAlchemyError

        engine = self._engine()
        start = time.perf_counter()
        try:
            with engine.connect() as conn:
                result = conn.execute(text(sql))
                columns = list(result.keys())
                rows = [[_jsonable(v) for v in row] for row in result.fetchall()]
        except DBAPIError as e:
            message = str(getattr(e, "orig", e)).strip().splitlines()[0] if str(getattr(e, "orig", e)).strip() else type(e).__name__
            raise ConnectorError(message[:500]) from e
        except SQLAlchemyError as e:
            raise ConnectorError(f"{type(e).__name__}: {str(e).splitlines()[0][:400]}") from e
        return QueryResult(columns=columns, rows=rows, duration_ms=int((time.perf_counter() - start) * 1000))

    def test(self) -> str:
        self.execute("SELECT 1")
        return "Connected successfully."


def dispose_engine(cache_key: tuple[int, str]) -> None:
    with _ENGINES_LOCK:
        engine = _ENGINES.pop(cache_key, None)
    if engine is not None:
        engine.dispose()
