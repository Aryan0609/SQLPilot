from app.response_insights import (
    build_follow_up_questions,
    ensure_dataset_full_rows,
    ensure_online_full_rows,
    summarize_answer,
    wants_full_rows,
)
from app.sql_safety import validate_select


def test_full_row_intent_detects_record_requests_without_changing_analysis_intent():
    assert wants_full_rows("Show the first 10 rows, including all columns.")
    assert wants_full_rows("List all matching invoices for customer 42")
    assert not wants_full_rows("What were the top 10 products by revenue?")


def test_record_query_expands_to_complete_dataset_rows_and_keeps_filters_and_limit():
    schema_name = "dataset_0123456789abcdef0123456789abcdef"
    sql = (
        f'SELECT "product_name" FROM "{schema_name}"."records" '
        'WHERE "product_name" = \'Widget\' ORDER BY "revenue_gbp" DESC LIMIT 500'
    )

    expanded = ensure_dataset_full_rows(
        sql,
        "Show matching records where product_name is Widget",
        "records",
    )
    safe_sql = validate_select(
        expanded,
        max_rows=2,
        allowed_tables={"records"},
        allowed_schemas={schema_name},
    )

    assert "SELECT *" in safe_sql.upper()
    assert '"product_name" = \'Widget\'' in safe_sql
    assert "ORDER BY" in safe_sql.upper()
    assert safe_sql.endswith("LIMIT 2")


def test_online_record_query_selects_all_fields_from_relevant_table():
    expanded = ensure_online_full_rows(
        "SELECT customer_id FROM customers WHERE customer_id = '123' LIMIT 10",
        "Show customer records for customer 123",
    )
    safe_sql = validate_select(expanded)

    assert "customers.*" in safe_sql
    assert "public.customers" in safe_sql
    assert "WHERE" in safe_sql.upper()


def test_online_joined_records_keep_all_fields_with_unique_output_names():
    schema = """customers:
  - customer_id VARCHAR NULL
  - country VARCHAR NULL

orders:
  - invoice_no VARCHAR NOT NULL
  - customer_id VARCHAR NULL"""
    expanded = ensure_online_full_rows(
        "SELECT c.customer_id FROM customers c "
        "JOIN orders o ON o.customer_id = c.customer_id",
        "Show matching customer order records",
        schema,
    )
    safe_sql = validate_select(expanded)

    assert 'c."customer_id" AS c_customer_id' in safe_sql
    assert 'c."country" AS c_country' in safe_sql
    assert 'o."invoice_no" AS o_invoice_no' in safe_sql
    assert 'o."customer_id" AS o_customer_id' in safe_sql


def test_follow_ups_use_active_schema_and_current_result_values_only():
    follow_ups = build_follow_up_questions(
        "Show monthly revenue by country",
        [
            {"name": "country", "postgres_type": "TEXT"},
            {"name": "revenue", "postgres_type": "NUMERIC"},
            {"name": "order_date", "postgres_type": "DATE"},
        ],
        ["country", "revenue"],
        [{"country": "Germany", "revenue": 100.5}],
        "SELECT country, SUM(revenue) AS revenue FROM records GROUP BY country",
    )

    assert follow_ups == [
        "How does revenue vary by country?",
        "How did revenue change over time using order_date?",
        "Show records where country is 'Germany'.",
    ]
    assert len(follow_ups) <= 3
    assert all("profit" not in suggestion.lower() for suggestion in follow_ups)


def test_follow_ups_prefer_measures_and_dimensions_over_unrequested_identifiers():
    follow_ups = build_follow_up_questions(
        "How many customers are in the database?",
        [
            {"name": "id", "postgres_type": "INTEGER"},
            {"name": "customer_id", "postgres_type": "VARCHAR"},
            {"name": "country", "postgres_type": "VARCHAR"},
            {"name": "revenue", "postgres_type": "NUMERIC"},
            {"name": "invoice_date", "postgres_type": "TIMESTAMP"},
        ],
        ["total_customers"],
        [{"total_customers": 4}],
        "SELECT COUNT(DISTINCT customer_id) AS total_customers FROM customers",
    )

    assert follow_ups == [
        "How does revenue vary by country?",
        "How did revenue change over time using invoice_date?",
    ]


def test_summary_describes_values_and_time_trend_from_returned_rows():
    summary = summarize_answer(
        "How did monthly revenue change over time?",
        "SELECT month, revenue FROM records ORDER BY month",
        ["month", "revenue"],
        [
            {"month": "2026-01-01", "revenue": 82},
            {"month": "2026-02-01", "revenue": 94},
        ],
        "The query calculates revenue.",
    )

    assert summary == (
        "Revenue rose from 82 on 2026-01-01 to 94 on 2026-02-01 "
        "across the returned time points."
    )
    assert "calculates" not in summary


def test_summary_names_highest_returned_group_and_its_value():
    summary = summarize_answer(
        "Which products had the highest revenue?",
        "SELECT product, revenue FROM records ORDER BY revenue DESC",
        ["product", "revenue"],
        [
            {"product": "A", "revenue": 21.5},
            {"product": "B", "revenue": 35.0},
        ],
    )

    assert summary == "Among the returned results, B has the highest revenue at 35."


def test_summary_respects_a_lowest_value_question():
    summary = summarize_answer(
        "Which products had the lowest revenue?",
        "SELECT product, revenue FROM records ORDER BY revenue",
        ["product", "revenue"],
        [
            {"product": "A", "revenue": 21.5},
            {"product": "B", "revenue": 35.0},
        ],
    )

    assert summary == "Among the returned results, A has the lowest revenue at 21.5."


def test_summary_handles_empty_results_without_claiming_a_value():
    assert summarize_answer(
        "Find missing values",
        "SELECT * FROM records WHERE false",
        ["id"],
        [],
    ) == "The query ran successfully, but no matching rows were returned."
