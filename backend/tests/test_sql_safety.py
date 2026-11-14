import pytest

from app.sql_safety import UnsafeQuery, validate_select


UPLOAD_SCHEMA = "dataset_0123456789abcdef0123456789abcdef"
OTHER_SCHEMA = "dataset_fedcba9876543210fedcba9876543210"


def test_existing_online_retail_query_is_pinned_to_public():
    sql = validate_select(
        "SELECT country, COUNT(*) FROM customers GROUP BY country"
    )
    assert "FROM public.customers" in sql
    assert "LIMIT 200" in sql.upper()


def test_explicit_public_online_retail_reference_is_allowed():
    sql = validate_select("SELECT * FROM public.orders LIMIT 5")
    assert "FROM public.orders" in sql
    assert "LIMIT 5" in sql.upper()


def test_uploaded_dataset_query_uses_its_qualified_table():
    sql = validate_select(
        f'SELECT amount FROM "{UPLOAD_SCHEMA}".records',
        allowed_tables={"records"},
        allowed_schemas={UPLOAD_SCHEMA},
    )
    assert f'FROM "{UPLOAD_SCHEMA}".records' in sql
    assert "LIMIT 200" in sql.upper()


def test_unquoted_uploaded_schema_name_is_allowed():
    sql = validate_select(
        f"SELECT amount FROM {UPLOAD_SCHEMA}.records",
        allowed_tables={"records"},
        allowed_schemas={UPLOAD_SCHEMA},
    )
    assert f"FROM {UPLOAD_SCHEMA}.records" in sql


def test_uploaded_query_requires_schema_qualification():
    with pytest.raises(UnsafeQuery, match="schema-qualified"):
        validate_select(
            "SELECT amount FROM records",
            allowed_tables={"records"},
            allowed_schemas={UPLOAD_SCHEMA},
        )


def test_uploaded_query_must_reference_the_selected_dataset_table():
    with pytest.raises(UnsafeQuery, match="must reference the selected dataset table"):
        validate_select(
            "SELECT 1",
            allowed_tables={"records"},
            allowed_schemas={UPLOAD_SCHEMA},
        )


def test_incorrect_dataset_schema_is_rejected():
    with pytest.raises(UnsafeQuery, match="Schema is not available"):
        validate_select(
            f"SELECT amount FROM {OTHER_SCHEMA}.records",
            allowed_tables={"records"},
            allowed_schemas={UPLOAD_SCHEMA},
        )


def test_uploaded_query_cannot_access_online_retail_even_if_table_is_allowed():
    with pytest.raises(UnsafeQuery, match="Schema is not available"):
        validate_select(
            f'SELECT * FROM "{UPLOAD_SCHEMA}".records '
            "JOIN public.orders ON true",
            allowed_tables={"records", "orders"},
            allowed_schemas={UPLOAD_SCHEMA},
        )


def test_online_retail_query_cannot_access_an_uploaded_schema():
    with pytest.raises(UnsafeQuery, match="Schema is not available"):
        validate_select(f"SELECT * FROM {UPLOAD_SCHEMA}.orders")


def test_cross_dataset_access_is_rejected():
    with pytest.raises(UnsafeQuery, match="Schema is not available"):
        validate_select(
            f"SELECT * FROM {UPLOAD_SCHEMA}.records "
            f"JOIN {OTHER_SCHEMA}.records ON true",
            allowed_tables={"records"},
            allowed_schemas={UPLOAD_SCHEMA},
        )


def test_multiple_schemas_are_available_only_when_explicitly_allowed():
    sql = validate_select(
        f"SELECT * FROM {UPLOAD_SCHEMA}.records "
        f"JOIN {OTHER_SCHEMA}.records AS other ON true",
        allowed_tables={"records"},
        allowed_schemas={UPLOAD_SCHEMA, OTHER_SCHEMA},
    )
    assert UPLOAD_SCHEMA in sql
    assert OTHER_SCHEMA in sql


@pytest.mark.parametrize("sql", [
    "SELECT * FROM secret_table",
    f"SELECT * FROM {UPLOAD_SCHEMA}.secret_table",
])
def test_unauthorized_table_is_rejected(sql):
    with pytest.raises(UnsafeQuery, match="Table is not available"):
        validate_select(
            sql,
            allowed_tables={"records"},
            allowed_schemas={UPLOAD_SCHEMA},
        )


@pytest.mark.parametrize("schema", ["pg_catalog", "pg_toast", "pg_temp_3", "information_schema"])
def test_system_schema_reference_is_rejected(schema):
    with pytest.raises(UnsafeQuery):
        validate_select(
            f"SELECT * FROM {schema}.orders",
            allowed_tables={"orders"},
            allowed_schemas={schema},
        )


def test_system_schema_cannot_be_added_to_the_allowlist():
    with pytest.raises(UnsafeQuery, match="System schemas cannot be authorized"):
        validate_select(
            "SELECT 1",
            allowed_tables=set(),
            allowed_schemas={"pg_catalog"},
        )


def test_cte_and_nested_queries_stay_inside_uploaded_schema():
    sql = f"""
        WITH amounts AS (
            SELECT amount FROM {UPLOAD_SCHEMA}.records
        )
        SELECT * FROM (
            SELECT amount FROM amounts
        ) AS nested
    """
    result = validate_select(
        sql,
        allowed_tables={"records"},
        allowed_schemas={UPLOAD_SCHEMA},
    )
    assert f"{UPLOAD_SCHEMA}.records" in result
    assert "LIMIT 200" in result.upper()


def test_unauthorized_table_inside_nested_query_is_rejected():
    sql = f"""
        SELECT * FROM (
            SELECT * FROM {OTHER_SCHEMA}.records
        ) AS nested
    """
    with pytest.raises(UnsafeQuery, match="Schema is not available"):
        validate_select(
            sql,
            allowed_tables={"records"},
            allowed_schemas={UPLOAD_SCHEMA},
        )


def test_unauthorized_table_inside_cte_is_rejected():
    sql = f"""
        WITH hidden AS (
            SELECT * FROM {OTHER_SCHEMA}.records
        )
        SELECT * FROM hidden
    """
    with pytest.raises(UnsafeQuery, match="Schema is not available"):
        validate_select(
            sql,
            allowed_tables={"records"},
            allowed_schemas={UPLOAD_SCHEMA},
        )


def test_nested_cte_cannot_hide_same_named_physical_table_in_outer_scope():
    sql = f"""
        SELECT * FROM orders
        WHERE EXISTS (
            WITH orders AS (
                SELECT * FROM {UPLOAD_SCHEMA}.records
            )
            SELECT * FROM orders
        )
    """
    with pytest.raises(UnsafeQuery, match="Table is not available: orders"):
        validate_select(
            sql,
            allowed_tables={"records"},
            allowed_schemas={UPLOAD_SCHEMA},
        )


def test_retail_cte_can_reference_retail_tables():
    sql = validate_select(
        "WITH totals AS (SELECT customer_id FROM customers) "
        "SELECT * FROM totals"
    )
    assert "WITH totals" in sql
    assert "public.customers" in sql


def test_nested_retail_query_is_pinned_to_public():
    sql = validate_select(
        "SELECT * FROM (SELECT * FROM orders) AS nested"
    )
    assert "public.orders" in sql


@pytest.mark.parametrize("sql", [
    "SELECT pg_sleep(5)",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT pg_read_binary_file('/etc/passwd')",
    "SELECT pg_ls_dir('/')",
    "SELECT nextval('some_sequence')",
    "SELECT setval('some_sequence', 10)",
    "SELECT set_config('statement_timeout', '0', false)",
    "SELECT pg_terminate_backend(123)",
    "SELECT pg_cancel_backend(123)",
    "SELECT lo_import('/tmp/file')",
    "SELECT lo_export(123, '/tmp/file')",
])
def test_dangerous_functions_are_rejected(sql):
    with pytest.raises(UnsafeQuery):
        validate_select(sql)


@pytest.mark.parametrize("sql", [
    "SELECT pg_catalog.pg_sleep(5)",
    "SELECT public.custom_function(1)",
    "SELECT pg_catalog.now()",
])
def test_schema_qualified_functions_are_rejected(sql):
    with pytest.raises(UnsafeQuery, match="Schema-qualified function"):
        validate_select(sql)


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM orders",
    "SELECT SUM(quantity) FROM order_items",
    "SELECT AVG(unit_price) FROM order_items",
    "SELECT COALESCE(customer_id, 'unknown') FROM customers",
    "SELECT DATE_TRUNC('month', invoice_date) FROM orders",
    "SELECT generate_series(1, 10)",
])
def test_analytical_functions_are_allowed(sql):
    validate_select(sql)


@pytest.mark.parametrize("sql", [
    "DELETE FROM customers",
    "UPDATE orders SET is_cancelled = true",
    "INSERT INTO orders VALUES ('1')",
    "DROP TABLE orders",
    "ALTER TABLE orders ADD COLUMN extra TEXT",
    "CREATE TABLE copied_orders AS SELECT * FROM orders",
])
def test_ddl_and_dml_are_rejected(sql):
    with pytest.raises(UnsafeQuery):
        validate_select(sql)


def test_select_into_is_rejected():
    with pytest.raises(UnsafeQuery, match="SELECT INTO and locking"):
        validate_select("SELECT * INTO copied_orders FROM orders")


@pytest.mark.parametrize("sql", [
    "SELECT * FROM orders FOR UPDATE",
    "SELECT * FROM orders FOR SHARE",
])
def test_locking_queries_are_rejected(sql):
    with pytest.raises(UnsafeQuery, match="SELECT INTO and locking"):
        validate_select(sql)


def test_multiple_statements_are_rejected():
    with pytest.raises(UnsafeQuery, match="Only one SQL statement"):
        validate_select("SELECT 1; SELECT 2")


def test_default_limit_is_added():
    sql = validate_select("SELECT country, COUNT(*) FROM customers GROUP BY country")
    assert "LIMIT 200" in sql.upper()


def test_limit_is_capped():
    sql = validate_select("SELECT * FROM orders LIMIT 10000", max_rows=25)
    assert "LIMIT 25" in sql.upper()


def test_small_limit_is_preserved():
    sql = validate_select("SELECT * FROM products LIMIT 5", max_rows=25)
    assert "LIMIT 5" in sql.upper()


def test_uploaded_query_limit_is_enforced():
    sql = validate_select(
        f"SELECT * FROM {UPLOAD_SCHEMA}.records LIMIT 1000",
        max_rows=20,
        allowed_tables={"records"},
        allowed_schemas={UPLOAD_SCHEMA},
    )
    assert "LIMIT 20" in sql.upper()


def test_negative_limit_is_capped():
    sql = validate_select("SELECT * FROM orders LIMIT -1", max_rows=200)
    assert "LIMIT 200" in sql.upper()
