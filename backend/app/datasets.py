import csv
import io
import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    MetaData,
    Numeric,
    Table,
    Text,
    insert,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.schema import CreateSchema, CreateTable
from sqlalchemy.sql.elements import quoted_name

from .dataset_models import UploadedDataset
from .db import writer_engine

router = APIRouter()
logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_COLUMNS = 100
MAX_IDENTIFIER_LENGTH = 63
INSERT_BATCH_SIZE = 500
READER_ROLE = "sqlpilot_reader"

ALLOWED_CONTENT_TYPES = {
    "application/csv",
    "application/octet-stream",
    "application/vnd.ms-excel",
    "text/csv",
}

INTEGER_PATTERN = re.compile(r"^[+-]?(?:0|[1-9][0-9]*)$")
PADDED_INTEGER_PATTERN = re.compile(r"^[+-]?0[0-9]+$")
NUMERIC_PATTERN = re.compile(
    r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$"
)
ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BOOLEAN_VALUES = {
    "true": True,
    "t": True,
    "yes": True,
    "y": True,
    "false": False,
    "f": False,
    "no": False,
    "n": False,
}

POSTGRES_TYPES = {
    "BIGINT": BigInteger,
    "NUMERIC": Numeric,
    "BOOLEAN": Boolean,
    "DATE": Date,
    "TIMESTAMP": DateTime,
    "TIMESTAMP WITH TIME ZONE": lambda: DateTime(timezone=True),
    "TEXT": Text,
}
POSTGRES_RESERVED_WORDS = postgresql.dialect().identifier_preparer.reserved_words


@dataclass(frozen=True)
class CsvColumn:
    original_name: str
    normalized_name: str
    postgres_type: str


class UploadedColumnResponse(BaseModel):
    name: str
    original_name: str
    postgres_type: str


class UploadedSchemaResponse(BaseModel):
    name: str
    table_name: str
    columns: list[UploadedColumnResponse]


class DatasetUploadResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    dataset_id: uuid.UUID
    dataset_name: str
    schema_info: UploadedSchemaResponse = Field(alias="schema")
    row_count: int
    column_count: int


def dataset_name_from_filename(filename: str | None) -> str:
    """Create a display name from the filename without using it as an identifier."""
    basename = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = basename[:-4] if basename.lower().endswith(".csv") else basename
    name = name.strip()
    return (name or "dataset")[:255]


def normalize_column_name(original_name: str) -> str:
    """Convert a CSV header to a deterministic, safe PostgreSQL identifier."""
    ascii_name = (
        unicodedata.normalize("NFKD", original_name)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", ascii_name).lower()
    normalized = re.sub(r"_+", "_", normalized).strip("_") or "column"

    if normalized[0].isdigit():
        normalized = f"column_{normalized}"
    if normalized in POSTGRES_RESERVED_WORDS:
        normalized = f"column_{normalized}"

    return normalized[:MAX_IDENTIFIER_LENGTH].rstrip("_") or "column"


def _parse_date(value: str) -> date | None:
    if not ISO_DATE_PATTERN.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_datetime(value: str) -> datetime | None:
    if "T" not in value and " " not in value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _decimal_value(value: str) -> Decimal | None:
    if not NUMERIC_PATTERN.fullmatch(value):
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def infer_postgres_type(values: list[str]) -> str:
    nonempty = [value.strip() for value in values if value.strip()]
    if not nonempty:
        return "TEXT"

    lowered = [value.lower() for value in nonempty]
    if all(value in BOOLEAN_VALUES for value in lowered):
        return "BOOLEAN"

    if all(INTEGER_PATTERN.fullmatch(value) for value in nonempty):
        integers = [int(value) for value in nonempty]
        if all(-(2**63) <= value < 2**63 for value in integers):
            return "BIGINT"
        return "NUMERIC"

    decimal_values = [_decimal_value(value) for value in nonempty]
    if all(value is not None for value in decimal_values):
        # Keep zero-padded whole numbers as text; they are commonly identifiers.
        has_decimal_notation = any("." in value or "e" in value.lower() for value in nonempty)
        has_padded_integer = any(PADDED_INTEGER_PATTERN.fullmatch(value) for value in nonempty)
        if has_decimal_notation:
            return "NUMERIC"
        if not has_padded_integer:
            return "BIGINT"
        return "TEXT"

    dates = [_parse_date(value) for value in nonempty]
    if all(value is not None for value in dates):
        return "DATE"

    timestamps = []
    has_time_component = False
    for value in nonempty:
        timestamp = _parse_datetime(value)
        if timestamp is not None:
            has_time_component = True
            timestamps.append(timestamp)
            continue
        parsed_date = _parse_date(value)
        if parsed_date is None:
            timestamps = []
            break
        timestamps.append(datetime.combine(parsed_date, datetime_time.min))

    if timestamps and has_time_component:
        timezone_states = {value.utcoffset() is not None for value in timestamps}
        if len(timezone_states) == 1:
            if timezone_states == {True}:
                return "TIMESTAMP WITH TIME ZONE"
            return "TIMESTAMP"

    return "TEXT"


def parse_csv_upload(
    content: bytes,
    filename: str | None,
    content_type: str | None,
) -> tuple[str, list[str], list[list[str]]]:
    if not filename or not filename.lower().endswith(".csv"):
        raise HTTPException(status_code=415, detail="Upload must be a .csv file")

    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type and media_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported CSV content type")

    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="CSV upload exceeds the 10 MiB limit")
    if not content:
        raise HTTPException(status_code=400, detail="CSV file is empty")

    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise HTTPException(
            status_code=400,
            detail="CSV file must use UTF-8 encoding",
        ) from error

    try:
        reader = csv.reader(io.StringIO(decoded, newline=""), strict=True)
        headers = next(reader, None)
        if not headers:
            raise HTTPException(status_code=400, detail="CSV file has no header row")
        if len(headers) > MAX_COLUMNS:
            raise HTTPException(
                status_code=422,
                detail=f"CSV may contain at most {MAX_COLUMNS} columns",
            )
        if any(not header.strip() for header in headers):
            raise HTTPException(status_code=422, detail="CSV headers cannot be blank")
        if any("\x00" in header for header in headers):
            raise HTTPException(status_code=422, detail="CSV headers cannot contain NUL bytes")
        if len(set(headers)) != len(headers):
            raise HTTPException(status_code=422, detail="CSV contains duplicate headers")

        rows = []
        for record_number, row in enumerate(reader, start=2):
            if not row:
                continue
            if len(row) != len(headers):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"CSV record {record_number} has {len(row)} fields; "
                        f"expected {len(headers)}"
                    ),
                )
            if any("\x00" in value for value in row):
                raise HTTPException(
                    status_code=422,
                    detail=f"CSV record {record_number} contains a NUL byte",
                )
            rows.append(row)
    except csv.Error as error:
        raise HTTPException(status_code=422, detail=f"Malformed CSV: {error}") from error

    return dataset_name_from_filename(filename), headers, rows


def build_column_specs(headers: list[str], rows: list[list[str]]) -> list[CsvColumn]:
    normalized_names = [normalize_column_name(header) for header in headers]
    duplicates = sorted({
        name for name in normalized_names if normalized_names.count(name) > 1
    })
    if duplicates:
        raise HTTPException(
            status_code=422,
            detail=f"CSV headers normalize to duplicate column names: {duplicates}",
        )

    return [
        CsvColumn(
            original_name=header,
            normalized_name=normalized_name,
            postgres_type=infer_postgres_type([row[index] for row in rows]),
        )
        for index, (header, normalized_name) in enumerate(zip(headers, normalized_names))
    ]


def _coerce_value(value: str, postgres_type: str):
    if not value.strip():
        return None

    if postgres_type == "BIGINT":
        return int(value)
    if postgres_type == "NUMERIC":
        return Decimal(value)
    if postgres_type == "BOOLEAN":
        return BOOLEAN_VALUES[value.strip().lower()]
    if postgres_type == "DATE":
        return date.fromisoformat(value.strip())
    if postgres_type in {"TIMESTAMP", "TIMESTAMP WITH TIME ZONE"}:
        timestamp = _parse_datetime(value.strip())
        if timestamp is not None:
            return timestamp
        parsed_date = _parse_date(value.strip())
        if parsed_date is not None:
            return datetime.combine(parsed_date, datetime_time.min)
    return value


def _quoted_identifier(identifier: str) -> str:
    return writer_engine.dialect.identifier_preparer.quote_identifier(identifier)


def ingest_dataset(
    dataset_name: str,
    column_specs: list[CsvColumn],
    rows: list[list[str]],
) -> tuple[uuid.UUID, str, str]:
    dataset_id = uuid.uuid4()
    schema_name = f"dataset_{dataset_id.hex}"
    table_name = "records"
    columns_info = [
        {
            "original_name": column.original_name,
            "name": column.normalized_name,
            "postgres_type": column.postgres_type,
        }
        for column in column_specs
    ]
    dynamic_table = Table(
        quoted_name(table_name, quote=True),
        MetaData(),
        *[
            Column(
                quoted_name(column.normalized_name, quote=True),
                POSTGRES_TYPES[column.postgres_type](),
                nullable=True,
            )
            for column in column_specs
        ],
        schema=quoted_name(schema_name, quote=True),
    )
    insert_rows = [
        {
            column.normalized_name: _coerce_value(row[index], column.postgres_type)
            for index, column in enumerate(column_specs)
        }
        for row in rows
    ]

    with writer_engine.begin() as connection:
        connection.execute(CreateSchema(quoted_name(schema_name, quote=True)))
        connection.execute(CreateTable(dynamic_table))
        connection.execute(
            insert(UploadedDataset).values(
                dataset_id=dataset_id,
                dataset_name=dataset_name,
                schema_name=schema_name,
                table_name=table_name,
                row_count=len(rows),
                column_count=len(column_specs),
                columns_info=columns_info,
            )
        )

        for start in range(0, len(insert_rows), INSERT_BATCH_SIZE):
            connection.execute(
                dynamic_table.insert(),
                insert_rows[start:start + INSERT_BATCH_SIZE],
            )

        quoted_schema = _quoted_identifier(schema_name)
        quoted_table = _quoted_identifier(table_name)
        connection.execute(
            text(f"GRANT USAGE ON SCHEMA {quoted_schema} TO {READER_ROLE}")
        )
        connection.execute(
            text(
                f"GRANT SELECT ON TABLE {quoted_schema}.{quoted_table} "
                f"TO {READER_ROLE}"
            )
        )

    return dataset_id, schema_name, table_name


@router.post(
    "/api/datasets/upload",
    response_model=DatasetUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_dataset(file: UploadFile = File(...)):
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="CSV upload exceeds the 10 MiB limit")

    dataset_name, headers, rows = parse_csv_upload(
        content,
        file.filename,
        file.content_type,
    )
    column_specs = build_column_specs(headers, rows)

    try:
        dataset_id, schema_name, table_name = ingest_dataset(
            dataset_name,
            column_specs,
            rows,
        )
    except SQLAlchemyError as error:
        # DDL, rows, metadata, and grants share one transaction and roll back together.
        logger.exception("Dataset ingestion failed")
        raise HTTPException(
            status_code=500,
            detail="Dataset ingestion failed; no dataset was created.",
        ) from error

    return DatasetUploadResponse(
        dataset_id=dataset_id,
        dataset_name=dataset_name,
        schema_info=UploadedSchemaResponse(
            name=schema_name,
            table_name=table_name,
            columns=[
                UploadedColumnResponse(
                    name=column.normalized_name,
                    original_name=column.original_name,
                    postgres_type=column.postgres_type,
                )
                for column in column_specs
            ],
        ),
        row_count=len(rows),
        column_count=len(column_specs),
    )
