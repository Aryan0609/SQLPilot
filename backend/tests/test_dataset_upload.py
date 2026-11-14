import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError

from app import datasets
from app.main import app


class RecordingConnection:
    def __init__(self, engine, transaction):
        self.engine = engine
        self.transaction = transaction

    def execute(self, statement, parameters=None):
        compiled = statement.compile(dialect=self.engine.dialect)
        sql = compiled.string
        if self.engine.fail_when and self.engine.fail_when(sql, parameters):
            raise SQLAlchemyError("simulated database failure")

        if parameters is None:
            recorded_parameters = dict(compiled.params or {})
        elif isinstance(parameters, list):
            recorded_parameters = [dict(values) for values in parameters]
        else:
            recorded_parameters = dict(parameters)

        self.transaction.statements.append((sql, recorded_parameters))
        return None


class RecordingTransaction:
    def __init__(self, engine):
        self.engine = engine
        self.statements = []

    def __enter__(self):
        return RecordingConnection(self.engine, self)

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            self.engine.commits.append(self.statements)
        else:
            self.engine.rollbacks += 1
        return False


class RecordingEngine:
    def __init__(self, fail_when=None):
        self.dialect = postgresql.dialect()
        self.fail_when = fail_when
        self.commits = []
        self.rollbacks = 0

    def begin(self):
        return RecordingTransaction(self)


@pytest.fixture
def database(monkeypatch):
    engine = RecordingEngine()
    monkeypatch.setattr(datasets, "writer_engine", engine)
    return engine


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def upload(client, filename="sales.csv", content=b"amount\n10\n"):
    return client.post(
        "/api/datasets/upload",
        files={"file": (filename, content, "text/csv")},
    )


def test_valid_csv_upload_returns_dataset_metadata_and_safe_schema(client, database):
    response = upload(
        client,
        filename="quarterly-sales.csv",
        content=b"Customer ID,Order Value\n001,12.50\n002,30.00\n",
    )

    assert response.status_code == 201
    body = response.json()
    dataset_id = uuid.UUID(body["dataset_id"])
    assert body["dataset_name"] == "quarterly-sales"
    assert body["schema"]["name"] == f"dataset_{dataset_id.hex}"
    assert body["schema"]["table_name"] == "records"
    assert body["row_count"] == 2
    assert body["column_count"] == 2
    assert body["schema"]["columns"] == [
        {
            "name": "customer_id",
            "original_name": "Customer ID",
            "postgres_type": "TEXT",
        },
        {
            "name": "order_value",
            "original_name": "Order Value",
            "postgres_type": "NUMERIC",
        },
    ]

    sql = "\n".join(statement for statement, _ in database.commits[0])
    assert f'CREATE SCHEMA "{body["schema"]["name"]}"' in sql
    assert '"customer_id" TEXT' in sql
    assert '"order_value" NUMERIC' in sql
    assert "GRANT SELECT ON TABLE" in sql
    assert "customers" not in sql


@pytest.mark.parametrize(
    ("filename", "content", "content_type", "status_code"),
    [
        ("empty.csv", b"", "text/csv", 400),
        ("not-csv.txt", b"name\nAda\n", "text/csv", 415),
        ("data.csv", b"name\nAda\n", "application/pdf", 415),
        ("broken.csv", b'a,b\n"1,2\n', "text/csv", 422),
        ("wrong-width.csv", b"a,b\n1\n", "text/csv", 422),
        ("bad-encoding.csv", b"name\n\xff\n", "text/csv", 400),
        ("blank-header.csv", b"name,\nAda,1\n", "text/csv", 422),
    ],
)
def test_invalid_uploads_are_rejected(
    client, database, filename, content, content_type, status_code
):
    response = client.post(
        "/api/datasets/upload",
        files={"file": (filename, content, content_type)},
    )
    assert response.status_code == status_code
    assert database.commits == []


def test_upload_size_limit_is_enforced(client, database, monkeypatch):
    monkeypatch.setattr(datasets, "MAX_UPLOAD_BYTES", 4)
    response = upload(client, content=b"name\nAda\n")
    assert response.status_code == 413
    assert database.commits == []


def test_column_limit_is_enforced(client, database, monkeypatch):
    monkeypatch.setattr(datasets, "MAX_COLUMNS", 1)
    response = upload(client, content=b"first,second\n1,2\n")
    assert response.status_code == 422
    assert database.commits == []


def test_duplicate_headers_are_rejected(client, database):
    response = upload(client, content=b"name,name\nAda,Grace\n")
    assert response.status_code == 422
    assert "duplicate headers" in response.json()["detail"]
    assert database.commits == []


def test_empty_trailing_unnamed_columns_are_ignored(client, database):
    response = upload(
        client,
        content=b"name,note,,, \nAda,, , ,   \nGrace,ok,,,\n",
    )

    assert response.status_code == 201
    body = response.json()
    assert body["row_count"] == 2
    assert body["column_count"] == 2
    assert [column["original_name"] for column in body["schema"]["columns"]] == [
        "name",
        "note",
    ]

    _, insert_parameters = next(
        (sql, parameters)
        for transaction in database.commits
        for sql, parameters in transaction
        if "INSERT INTO" in sql and '"records"' in sql
    )
    assert insert_parameters[0]["note"] is None
    assert insert_parameters[1]["note"] == "ok"


def test_blank_header_with_data_is_rejected_with_column_positions(client, database):
    response = upload(client, content=b"name,,,\nAda,,value,tail\n")

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "CSV has data under blank headers at column position(s): 3, 4. "
        "Name these columns or remove their data before upload."
    )
    assert database.commits == []


def test_empty_interior_blank_header_is_rejected(client, database):
    response = upload(client, content=b"name,,amount\nAda,,1\n")

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "CSV has blank headers in interior column position(s): 2. "
        "Blank headers are allowed only in empty trailing columns."
    )
    assert database.commits == []


def test_duplicate_normalized_headers_are_rejected(client, database):
    response = upload(client, content=b"Customer ID,customer-id\n1,2\n")
    assert response.status_code == 422
    assert "duplicate column names" in response.json()["detail"]
    assert database.commits == []


def test_numeric_and_date_types_are_inferred():
    assert datasets.infer_postgres_type(["10", "-2"]) == "BIGINT"
    assert datasets.infer_postgres_type(["10.25", "-2", "3e2"]) == "NUMERIC"
    assert datasets.infer_postgres_type(["2025-01-30", "2025-02-01"]) == "DATE"
    assert datasets.infer_postgres_type(
        ["2025-01-30T12:00:00Z", "2025-02-01T08:30:00Z"]
    ) == "TIMESTAMP WITH TIME ZONE"


def test_nullable_values_are_loaded_as_null(client, database):
    response = upload(
        client,
        content=b"amount,note\n1,first\n2,\n,third\n",
    )
    assert response.status_code == 201

    insert_sql, insert_parameters = next(
        (sql, parameters)
        for transaction in database.commits
        for sql, parameters in transaction
        if "INSERT INTO" in sql and '"records"' in sql
    )
    assert "amount" in insert_sql
    assert any(None in row.values() for row in insert_parameters)


def test_inferred_values_are_bound_as_native_types(client, database):
    response = upload(
        client,
        content=b"count,price,sold_on\n2,10.50,2025-01-30\n",
    )
    assert response.status_code == 201

    _, insert_parameters = next(
        (sql, parameters)
        for transaction in database.commits
        for sql, parameters in transaction
        if "INSERT INTO" in sql and '"records"' in sql
    )
    values = insert_parameters[0]
    assert values["count"] == 2
    assert values["price"].as_tuple().exponent == -2
    assert str(values["sold_on"]) == "2025-01-30"


def test_original_headers_are_stored_in_database_metadata(client, database):
    response = upload(client, content=b"Original Name\nvalue\n")
    assert response.status_code == 201
    metadata_sql, metadata_parameters = next(
        (sql, parameters)
        for transaction in database.commits
        for sql, parameters in transaction
        if "uploaded_datasets" in sql
    )
    assert "INSERT INTO" in metadata_sql
    assert metadata_parameters["columns_info"][0]["original_name"] == "Original Name"
    assert metadata_parameters["columns_info"][0]["name"] == "original_name"


def test_identifier_normalization_is_safe_and_handles_reserved_words():
    assert datasets.normalize_column_name("Customer ID") == "customer_id"
    assert datasets.normalize_column_name("1 Total (%)") == "column_1_total"
    assert datasets.normalize_column_name("SELECT") == "column_select"
    assert datasets.normalize_column_name("DROP TABLE customers; --") == (
        "drop_table_customers"
    )
    assert len(datasets.normalize_column_name("x" * 100)) <= 63


def test_database_failure_rolls_back_the_ingestion(client, monkeypatch):
    engine = RecordingEngine(
        fail_when=lambda sql, _: "INSERT INTO" in sql and '"records"' in sql
    )
    monkeypatch.setattr(datasets, "writer_engine", engine)

    response = upload(client, content=b"amount\n10\n")

    assert response.status_code == 500
    assert response.json()["detail"] == "Dataset ingestion failed; no dataset was created."
    assert engine.commits == []
    assert engine.rollbacks == 1


def test_multiple_uploads_create_independent_datasets(client, database):
    first = upload(client, filename="first.csv").json()
    second = upload(client, filename="second.csv").json()

    assert first["dataset_id"] != second["dataset_id"]
    assert first["schema"]["name"] != second["schema"]["name"]
    assert len(database.commits) == 2
