import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app import datasets, llm, main


class FakeRow:
    def __init__(self, mapping):
        self._mapping = mapping


class FakeResult:
    def __init__(self, dataset=None, columns=None, rows=None):
        self.dataset = dataset
        self.columns = columns or []
        self.rows = rows or []

    def scalar_one_or_none(self):
        return self.dataset

    def mappings(self):
        return self

    def one_or_none(self):
        return vars(self.dataset) if self.dataset is not None else None

    def keys(self):
        return self.columns

    def fetchmany(self, count):
        return [FakeRow(row) for row in self.rows[:count]]

    def fetchall(self):
        return [FakeRow(row) for row in self.rows]


class FakeConnection:
    def __init__(self, engine):
        self.engine = engine

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, statement):
        sql = str(statement)
        if "uploaded_datasets" in sql.lower():
            if self.engine.metadata_error:
                raise self.engine.metadata_error
            return FakeResult(dataset=self.engine.dataset)

        self.engine.statements.append(sql)
        if sql.lstrip().upper().startswith("SELECT"):
            self.engine.executed_queries.append(sql)
            if self.engine.query_error:
                raise self.engine.query_error
            return FakeResult(
                columns=self.engine.result_columns,
                rows=self.engine.result_rows,
            )
        return FakeResult()


class FakeReaderEngine:
    def __init__(self, dataset=None):
        self.dataset = dataset
        self.metadata_error = None
        self.query_error = None
        self.result_columns = ["product", "total_revenue"]
        self.result_rows = [{"product": "Widget", "total_revenue": 25.5}]
        self.statements = []
        self.executed_queries = []

    def connect(self):
        return FakeConnection(self)

    def begin(self):
        return FakeConnection(self)


@pytest.fixture
def client():
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def dataset():
    dataset_id = uuid.uuid4()
    return SimpleNamespace(
        dataset_id=dataset_id,
        dataset_name="catalog",
        schema_name=f"dataset_{dataset_id.hex}",
        table_name="records",
        columns_info=[
            {
                "original_name": "Product name",
                "name": "product_name",
                "postgres_type": "TEXT",
            },
            {
                "original_name": "Revenue GBP",
                "name": "revenue_gbp",
                "postgres_type": "NUMERIC",
            },
        ],
    )


@pytest.fixture
def reader(monkeypatch, dataset):
    engine = FakeReaderEngine(dataset)
    monkeypatch.setattr(main, "reader_engine", engine)
    monkeypatch.setattr(
        main,
        "settings",
        SimpleNamespace(max_rows=200, query_timeout_ms=15000),
    )
    return engine


@pytest.fixture
def set_generator(monkeypatch):
    def configure(sql, explanation="A test explanation."):
        calls = []

        async def generate(question, dataset_name, schema_name, table_name, columns_info):
            calls.append({
                "question": question,
                "dataset_name": dataset_name,
                "schema_name": schema_name,
                "table_name": table_name,
                "columns_info": columns_info,
            })
            return sql, explanation

        monkeypatch.setattr(main, "generate_dataset_sql", generate)
        return calls

    return configure


def ask_dataset(client, dataset, question="What are the top products by revenue?"):
    return client.post(
        f"/api/datasets/{dataset.dataset_id}/ask",
        json={"question": question},
    )


def test_valid_dataset_query_uses_selected_schema_and_returns_results(
    client, dataset, reader, set_generator
):
    generated_sql = (
        f'SELECT "product_name", SUM("revenue_gbp") AS total_revenue '
        f'FROM "{dataset.schema_name}"."records" '
        'GROUP BY "product_name" ORDER BY total_revenue DESC LIMIT 10'
    )
    calls = set_generator(generated_sql)

    response = ask_dataset(client, dataset)

    assert response.status_code == 200
    body = response.json()
    assert body["sql"].endswith("LIMIT 10")
    assert body["explanation"] == "A test explanation."
    assert body["columns"] == ["product", "total_revenue"]
    assert body["rows"] == [{"product": "Widget", "total_revenue": 25.5}]
    assert body["row_count"] == 1
    assert calls[0]["schema_name"] == dataset.schema_name
    assert calls[0]["table_name"] == dataset.table_name
    assert calls[0]["columns_info"] == dataset.columns_info
    assert len(reader.executed_queries) == 1
    assert dataset.schema_name in reader.executed_queries[0]
    assert any("statement_timeout" in statement for statement in reader.statements)
    assert any("search_path TO pg_catalog" in statement for statement in reader.statements)


def test_invalid_dataset_uuid_returns_422_without_querying_metadata(client, monkeypatch):
    engine = FakeReaderEngine()
    monkeypatch.setattr(main, "reader_engine", engine)
    response = client.post(
        "/api/datasets/not-a-uuid/ask",
        json={"question": "Summarize this data"},
    )
    assert response.status_code == 422
    assert engine.statements == []


def test_missing_dataset_returns_404(client, reader, dataset, set_generator):
    reader.dataset = None
    calls = set_generator("SELECT 1")

    response = ask_dataset(client, dataset)

    assert response.status_code == 404
    assert calls == []
    assert reader.executed_queries == []


def test_dataset_prompt_includes_only_column_metadata():
    prompt = llm.build_dataset_prompt(
        "What is total revenue?",
        "retail upload",
        "dataset_123",
        "records",
        [
            {
                "original_name": "Revenue GBP",
                "name": "revenue_gbp",
                "postgres_type": "NUMERIC",
            },
        ],
    )

    assert "retail upload" in prompt
    assert '"dataset_123"."records"' in prompt
    assert "revenue_gbp" in prompt
    assert "Revenue GBP" in prompt
    assert "NUMERIC" in prompt
    assert "Do not invent columns, tables" in prompt
    assert "CSV contents" not in prompt


def test_monthly_revenue_prompt_marks_uploaded_columns_as_authoritative():
    prompt = llm.build_dataset_prompt(
        "Calculate total revenue for each month. Display the month and revenue, sorted chronologically.",
        "manual_sales",
        "dataset_123",
        "records",
        [
            {"name": "order_date", "postgres_type": "DATE"},
            {"name": "quantity", "postgres_type": "BIGINT"},
            {"name": "unit_price", "postgres_type": "BIGINT"},
        ],
    )

    assert '"sql_column": "order_date"' in prompt
    assert '"sql_column": "quantity"' in prompt
    assert '"sql_column": "unit_price"' in prompt
    assert "Every listed SQL column exists" in prompt
    assert "Never say a listed SQL column is missing or unavailable" in prompt


def test_monthly_revenue_false_missing_column_refusal_is_retried(
    client, dataset, reader, monkeypatch
):
    dataset.columns_info = [
        {"name": "order_date", "postgres_type": "DATE"},
        {"name": "quantity", "postgres_type": "BIGINT"},
        {"name": "unit_price", "postgres_type": "BIGINT"},
    ]
    reader.result_columns = ["month", "revenue"]
    reader.result_rows = [
        {"month": "2026-01-01T00:00:00", "revenue": 82},
        {"month": "2026-02-01T00:00:00", "revenue": 94},
    ]
    prompts = []
    generated_sql = (
        f'SELECT DATE_TRUNC(\'month\', "order_date") AS month, '
        f'SUM("quantity" * "unit_price") AS revenue '
        f'FROM "{dataset.schema_name}"."records" '
        f'GROUP BY DATE_TRUNC(\'month\', "order_date") ORDER BY month'
    )
    responses = [
        (
            "",
            "The dataset needs additional columns 'order_date' and 'unit_price'; "
            "these columns are not available.",
        ),
        (generated_sql, "Monthly revenue is grouped chronologically."),
    ]

    async def fake_request(prompt, *, default_explanation, allow_empty_sql):
        prompts.append(prompt)
        assert allow_empty_sql is True
        return responses.pop(0)

    monkeypatch.setattr(llm, "_request_sql_from_prompt", fake_request)

    response = client.post(
        f"/api/datasets/{dataset.dataset_id}/ask",
        json={
            "question": (
                "Calculate total revenue for each month. Display the month and "
                "revenue, sorted chronologically."
            )
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["rows"] == [
        {"month": "2026-01-01T00:00:00", "revenue": 82},
        {"month": "2026-02-01T00:00:00", "revenue": 94},
    ]
    assert body["sql"]
    assert len(prompts) == 2
    assert '"sql_column": "order_date"' in prompts[0]
    assert '"sql_column": "unit_price"' in prompts[0]
    assert "SCHEMA GROUNDING RETRY" in prompts[1]
    assert '"order_date"' in prompts[1]
    assert '"unit_price"' in prompts[1]
    assert len(reader.executed_queries) == 1


def test_dataset_generator_sends_dynamic_prompt_to_ollama_helper(monkeypatch):
    captured = {}

    async def fake_request(prompt, *, default_explanation, allow_empty_sql):
        captured.update({
            "prompt": prompt,
            "default_explanation": default_explanation,
            "allow_empty_sql": allow_empty_sql,
        })
        return "", "Not enough columns are available."

    monkeypatch.setattr(llm, "_request_sql_from_prompt", fake_request)
    sql, explanation = asyncio.run(llm.generate_dataset_sql(
        "Which supplier had the highest margin?",
        "inventory",
        "dataset_456",
        "records",
        [{"name": "quantity", "postgres_type": "BIGINT"}],
    ))

    assert sql == ""
    assert explanation == "Not enough columns are available."
    assert "dataset_456" in captured["prompt"]
    assert "quantity" in captured["prompt"]
    assert captured["allow_empty_sql"] is True


@pytest.mark.parametrize(
    "generated_sql",
    [
        "SELECT * FROM public.records",
        'SELECT * FROM "dataset_other"."records"',
        "SELECT * FROM records",
        "SELECT 1",
    ],
)
def test_unauthorized_or_unqualified_dataset_tables_are_rejected(
    client, dataset, reader, set_generator, generated_sql
):
    set_generator(generated_sql)

    response = ask_dataset(client, dataset)

    assert response.status_code == 422
    assert reader.executed_queries == []


def test_query_cannot_access_another_uploaded_dataset(
    client, dataset, reader, set_generator
):
    other_schema = f"dataset_{uuid.uuid4().hex}"
    set_generator(f'SELECT * FROM "{other_schema}"."records"')

    response = ask_dataset(client, dataset)

    assert response.status_code == 422
    assert reader.executed_queries == []


def test_unanswerable_question_returns_explanation_without_execution(
    client, dataset, reader, set_generator
):
    set_generator("", "The dataset has no supplier margin column.")

    response = ask_dataset(client, dataset)

    assert response.status_code == 200
    assert response.json() == {
        "sql": "",
        "explanation": "The dataset has no supplier margin column.",
        "columns": [],
        "rows": [],
        "row_count": 0,
    }
    assert reader.executed_queries == []


def test_query_errors_return_the_existing_execution_error_shape(
    client, dataset, reader, set_generator
):
    reader.query_error = SQLAlchemyError("private database detail")
    set_generator(f'SELECT * FROM "{dataset.schema_name}"."records"')

    response = ask_dataset(client, dataset)

    assert response.status_code == 422
    assert response.json()["detail"] == "The generated query could not be executed."


def test_row_limit_and_statement_timeout_are_enforced(
    client, dataset, reader, set_generator, monkeypatch
):
    monkeypatch.setattr(
        main,
        "settings",
        SimpleNamespace(max_rows=2, query_timeout_ms=4321),
    )
    reader.result_rows = [
        {"product": "A", "total_revenue": 10},
        {"product": "B", "total_revenue": 9},
        {"product": "C", "total_revenue": 8},
    ]
    set_generator(
        f'SELECT * FROM "{dataset.schema_name}"."records" LIMIT 100'
    )

    response = ask_dataset(client, dataset)

    assert response.status_code == 200
    assert len(response.json()["rows"]) == 2
    assert response.json()["row_count"] == 2
    assert reader.executed_queries[0].endswith("LIMIT 2")
    assert any("statement_timeout = 4321" in s for s in reader.statements)


def test_existing_online_retail_endpoint_remains_independent(
    client, reader, monkeypatch
):
    calls = []

    async def generate_retail_sql(question, schema):
        calls.append((question, schema))
        return "SELECT customer_id FROM customers LIMIT 5", "Retail query."

    monkeypatch.setattr(main, "generate_sql", generate_retail_sql)
    monkeypatch.setattr(main, "get_schema", lambda: "retail schema")
    reader.result_columns = ["customer_id"]
    reader.result_rows = [{"customer_id": "123"}]

    response = client.post(
        "/api/ask",
        json={"question": "Show a few customers"},
    )

    assert response.status_code == 200
    assert response.json()["sql"] == "SELECT customer_id FROM public.customers LIMIT 5"
    assert response.json()["rows"] == [{"customer_id": "123"}]
    assert calls == [("Show a few customers", "retail schema")]
    assert not any("search_path TO pg_catalog" in statement for statement in reader.statements)


def test_uci_bike_sharing_csv_upload_then_dataset_query(
    client, reader, set_generator, monkeypatch
):
    """Exercise the UCI day.csv shape through upload and dataset-specific ask."""
    dataset_id = uuid.uuid4()
    schema_name = f"dataset_{dataset_id.hex}"
    captured = {}

    def fake_ingest(dataset_name, column_specs, rows):
        captured["dataset_name"] = dataset_name
        captured["column_specs"] = column_specs
        captured["rows"] = rows
        return dataset_id, schema_name, "records"

    monkeypatch.setattr(datasets, "ingest_dataset", fake_ingest)
    bike_day_csv = (
        b"instant,dteday,season,yr,mnth,holiday,weekday,workingday,weathersit,"
        b"temp,atemp,hum,windspeed,casual,registered,cnt\n"
        b"1,2011-01-01,1,0,1,0,6,0,2,0.34,0.36,0.80,0.16,331,654,985\n"
        b"2,2011-01-02,1,0,1,0,0,0,2,0.36,0.35,0.69,0.24,131,670,801\n"
        b"3,2011-01-03,1,0,1,0,1,1,1,0.19,0.19,0.43,0.24,120,1229,1349\n"
    )

    upload_response = client.post(
        "/api/datasets/upload",
        files={"file": ("day.csv", bike_day_csv, "text/csv")},
    )

    assert upload_response.status_code == 201
    uploaded = upload_response.json()
    assert uploaded["dataset_name"] == "day"
    assert uploaded["dataset_id"] == str(dataset_id)
    assert uploaded["row_count"] == 3
    assert uploaded["column_count"] == 16
    columns = uploaded["schema"]["columns"]
    by_name = {column["original_name"]: column for column in columns}
    assert by_name["dteday"]["name"] == "dteday"
    assert by_name["dteday"]["postgres_type"] == "DATE"
    assert by_name["season"]["postgres_type"] == "BIGINT"
    assert by_name["temp"]["postgres_type"] == "NUMERIC"
    assert by_name["cnt"]["postgres_type"] == "BIGINT"
    assert captured["dataset_name"] == "day"
    assert len(captured["rows"]) == 3

    reader.dataset = SimpleNamespace(
        dataset_id=dataset_id,
        dataset_name=uploaded["dataset_name"],
        schema_name=uploaded["schema"]["name"],
        table_name=uploaded["schema"]["table_name"],
        columns_info=[
            {
                "original_name": column["original_name"],
                "name": column["name"],
                "postgres_type": column["postgres_type"],
            }
            for column in columns
        ],
    )
    reader.result_columns = ["season", "average_daily_rentals"]
    reader.result_rows = [
        {"season": 1, "average_daily_rentals": 1045.0},
        {"season": 2, "average_daily_rentals": 2088.0},
    ]
    generated_sql = (
        f'SELECT "season", AVG("cnt") AS average_daily_rentals '
        f'FROM "{schema_name}"."records" '
        'GROUP BY "season" ORDER BY "season"'
    )
    generator_calls = set_generator(generated_sql, "Average daily rentals by season.")

    response = client.post(
        f"/api/datasets/{dataset_id}/ask",
        json={"question": "What was the average daily rental count by season?"},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["sql"].endswith("LIMIT 200")
    assert result["explanation"] == "Average daily rentals by season."
    assert result["columns"] == ["season", "average_daily_rentals"]
    assert result["rows"] == reader.result_rows
    assert result["row_count"] == 2
    assert generator_calls[0]["schema_name"] == schema_name
    assert generator_calls[0]["columns_info"] == reader.dataset.columns_info
    assert len(reader.executed_queries) == 1
    assert f'"{schema_name}"."records"' in reader.executed_queries[0]
