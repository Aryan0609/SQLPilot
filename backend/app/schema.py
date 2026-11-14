import logging
from threading import Lock
from time import monotonic, perf_counter

from sqlalchemy import inspect

from .db import reader_engine

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
_SCHEMA_CACHE_TTL_SECONDS = 30
_INTERNAL_TABLES = {"uploaded_datasets"}
_schema_cache: str | None = None
_schema_cache_expires_at = 0.0
_schema_cache_lock = Lock()


def _inspect_schema() -> str:
    inspector = inspect(reader_engine)

    lines = ["DATABASE TABLES AND COLUMNS:"]

    for table in inspector.get_table_names():
        if table.lower() in _INTERNAL_TABLES:
            continue
        lines.append(f"\n{table}:")

        for column in inspector.get_columns(table):
            nullable = "NULL" if column.get("nullable") else "NOT NULL"
            lines.append(
                f"  - {column['name']} "
                f"{column['type']} {nullable}"
            )

        primary_key = inspector.get_pk_constraint(table)
        pk_columns = primary_key.get("constrained_columns", [])

        if pk_columns:
            lines.append(f"  PRIMARY KEY: {', '.join(pk_columns)}")

        foreign_keys = inspector.get_foreign_keys(table)

        for fk in foreign_keys:
            source = ", ".join(fk["constrained_columns"])
            target = ", ".join(fk["referred_columns"])

            lines.append(
                f"  FOREIGN KEY: {source} -> "
                f"{fk['referred_table']}.{target}"
            )

    return "\n".join(lines)


def get_schema(force_refresh: bool = False) -> str:
    """Return a short-lived schema snapshot to avoid reflecting every query."""
    global _schema_cache, _schema_cache_expires_at
    with _schema_cache_lock:
        now = monotonic()
        if not force_refresh and _schema_cache is not None and now < _schema_cache_expires_at:
            logger.debug("Schema metadata cache hit")
            return _schema_cache

        started = perf_counter()
        schema = _inspect_schema()
        _schema_cache = schema
        _schema_cache_expires_at = monotonic() + _SCHEMA_CACHE_TTL_SECONDS
        logger.info(
            "Schema metadata refreshed in %.1f ms",
            (perf_counter() - started) * 1000,
        )
        return schema
