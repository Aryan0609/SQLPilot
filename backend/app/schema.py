from sqlalchemy import inspect

from .db import reader_engine


def get_schema() -> str:
    inspector = inspect(reader_engine)

    lines = ["DATABASE TABLES AND COLUMNS:"]

    for table in inspector.get_table_names():
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
