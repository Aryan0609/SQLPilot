import pytest
from app.sql_safety import UnsafeQuery, validate_select

def test_select_gets_a_default_row_limit():
    sql = validate_select("SELECT country, COUNT(*) FROM customers GROUP BY country")
    assert "LIMIT 200" in sql.upper()

def test_cte_can_reference_its_own_name():
    sql = validate_select("WITH totals AS (SELECT customer_id FROM customers) SELECT * FROM totals")
    assert "WITH totals" in sql

def test_select_limit_is_capped():
    sql = validate_select("SELECT * FROM orders LIMIT 10000", max_rows=25)
    assert "LIMIT 25" in sql.upper()

def test_small_limit_is_preserved():
    sql = validate_select("SELECT * FROM products LIMIT 5", max_rows=25)
    assert "LIMIT 5" in sql.upper()

@pytest.mark.parametrize("sql", [
    "DELETE FROM customers",
    "UPDATE orders SET is_cancelled = true",
    "DROP TABLE orders",
    "SELECT * FROM pg_catalog.pg_tables",
    "SELECT * FROM secret_table",
    "SELECT * INTO copied_orders FROM orders",
    "SELECT 1; SELECT 2",
])
def test_unsafe_sql_is_rejected(sql):
    with pytest.raises(UnsafeQuery):
        validate_select(sql)
