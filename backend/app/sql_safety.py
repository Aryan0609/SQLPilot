from collections.abc import Iterable

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

ALLOWED_TABLES = {"customers", "orders", "products", "order_items"}
RETAIL_SCHEMA = "public"

DANGEROUS_FUNCTIONS = {
    "pg_sleep",
    "pg_sleep_for",
    "pg_sleep_until",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "nextval",
    "setval",
    "set_config",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "lo_import",
    "lo_export",
}


class UnsafeQuery(ValueError):
    pass


def _identifier_name(identifier: exp.Expression | None) -> str | None:
    """Return an SQL identifier as PostgreSQL resolves it."""
    if identifier is None:
        return None
    if isinstance(identifier, exp.Identifier):
        name = str(identifier.this)
        return name if identifier.args.get("quoted") else name.lower()
    return str(identifier).lower()


def _as_name_set(names: Iterable[str] | None) -> set[str] | None:
    if names is None:
        return None
    if isinstance(names, str):
        return {names}
    return {str(name) for name in names}


def _is_system_schema(schema: str) -> bool:
    normalized = schema.lower()
    return normalized == "information_schema" or normalized.startswith("pg_")


def _reject_dangerous_functions(tree: exp.Expression) -> None:
    for node in tree.walk():
        if isinstance(node, exp.Dot) and isinstance(node.expression, exp.Func):
            raise UnsafeQuery("Schema-qualified function calls are not allowed")

        if isinstance(node, exp.Anonymous):
            function_name = str(node.this).strip('"').lower()
            if function_name in DANGEROUS_FUNCTIONS:
                raise UnsafeQuery(f"Function is not allowed: {function_name}")


def _validate_tables(
    tree: exp.Expression,
    allowed_tables: set[str],
    allowed_schemas: set[str],
    *,
    require_schema_qualification: bool,
) -> bool:
    has_authorized_table = False
    for scope in traverse_scope(tree):
        for table in scope.tables:
            name = _identifier_name(table.args.get("this"))
            schema = _identifier_name(table.args.get("db"))
            catalog = _identifier_name(table.args.get("catalog"))

            # CTE and derived-table references are query-local. Scope resolution
            # distinguishes them from same-named physical tables in outer queries.
            source = scope.sources.get(table.alias_or_name)
            if (
                isinstance(source, Scope)
                and schema is None
                and catalog is None
            ):
                continue

            if catalog is not None:
                raise UnsafeQuery("Catalog-qualified table references are not allowed")
            if schema is not None and _is_system_schema(schema):
                raise UnsafeQuery(f"System schema is not available: {schema}")
            if name not in allowed_tables:
                raise UnsafeQuery(f"Table is not available: {name}")

            if schema is None:
                if require_schema_qualification:
                    raise UnsafeQuery("Dataset tables must be schema-qualified")
                # Pin legacy, unqualified retail references to public so they cannot
                # resolve through a user-controlled or expanded PostgreSQL search_path.
                table.set("db", exp.to_identifier(RETAIL_SCHEMA))
                continue

            if schema not in allowed_schemas:
                raise UnsafeQuery(f"Schema is not available: {schema}")
            has_authorized_table = True

    return has_authorized_table


def validate_select(
    sql: str,
    max_rows: int = 200,
    allowed_tables: Iterable[str] | None = None,
    allowed_schemas: Iterable[str] | None = None,
) -> str:
    """Validate one read-only SELECT and constrain its table scope and size.

    Omitting both allowlists preserves the Online Retail contract: only its four
    tables in ``public`` are available, and unqualified references are pinned to
    ``public``. For an uploaded dataset, pass its table names and schema; every
    physical table reference must then be qualified by one of those schemas.
    """
    try:
        statements = sqlglot.parse(sql, read="postgres")
    except sqlglot.errors.ParseError as exc:
        raise UnsafeQuery("Could not parse generated SQL") from exc

    if len(statements) != 1 or statements[0] is None:
        raise UnsafeQuery("Only one SQL statement is allowed")

    tree = statements[0]
    if not isinstance(tree, exp.Select):
        raise UnsafeQuery("Only SELECT queries are allowed")
    if tree.args.get("into") or tree.args.get("locks"):
        raise UnsafeQuery("SELECT INTO and locking queries are not allowed")

    for node in tree.walk():
        if isinstance(
            node,
            (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Command),
        ):
            raise UnsafeQuery("Only read-only SELECT queries are allowed")

    requested_tables = _as_name_set(allowed_tables)
    table_allowlist = (
        {name.lower() for name in ALLOWED_TABLES}
        if requested_tables is None
        else requested_tables
    )

    requested_schemas = _as_name_set(allowed_schemas)
    if requested_schemas is None:
        schema_allowlist = {RETAIL_SCHEMA}
        require_schema_qualification = False
    else:
        forbidden_schemas = {
            schema for schema in requested_schemas if _is_system_schema(schema)
        }
        if forbidden_schemas:
            raise UnsafeQuery("System schemas cannot be authorized")
        schema_allowlist = requested_schemas
        require_schema_qualification = True

    has_authorized_table = _validate_tables(
        tree,
        table_allowlist,
        schema_allowlist,
        require_schema_qualification=require_schema_qualification,
    )
    if require_schema_qualification and not has_authorized_table:
        raise UnsafeQuery("Dataset queries must reference the selected dataset table")
    _reject_dangerous_functions(tree)

    if not tree.args.get("limit"):
        tree = tree.limit(max_rows)
    else:
        count = tree.args["limit"].expression
        try:
            if int(count.this) > max_rows:
                tree.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
        except (AttributeError, ValueError, TypeError):
            tree.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))

    return tree.sql(dialect="postgres")
