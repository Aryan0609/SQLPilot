import logging
from datetime import date, datetime
from decimal import Decimal
from time import perf_counter
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from .config import settings
from .datasets import router as datasets_router
from .dataset_models import UploadedDataset
from .db import reader_engine
from .llm import generate_dataset_sql, generate_sql
from .response_insights import (
    build_follow_up_questions,
    ensure_dataset_full_rows,
    ensure_online_full_rows,
    parse_online_columns,
    summarize_answer,
)
from .schema import get_schema
from .sql_safety import UnsafeQuery, validate_select

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

app = FastAPI(
    title="SQLPilot",
    version="0.1.0",
    description="Ask questions about retail sales in plain English."
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)


class AskResponse(BaseModel):
    sql: str
    explanation: str
    columns: list[str]
    rows: list[dict]
    row_count: int
    follow_up_questions: list[str] = Field(default_factory=list)


def serialize(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


@app.get("/api/health")
def health():
    try:
        with reader_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok"}
    except SQLAlchemyError as exc:
        logger.exception("Database health check failed")
        raise HTTPException(
            status_code=503,
            detail="Database is unavailable"
        ) from exc


@app.get("/api/schema")
def schema(refresh: bool = False):
    try:
        return {"schema": get_schema(force_refresh=refresh)}
    except SQLAlchemyError as exc:
        logger.exception("Schema retrieval failed")
        raise HTTPException(
            status_code=503,
            detail="Database schema is unavailable"
        ) from exc


@app.post("/api/ask", response_model=AskResponse)
async def ask(request: AskRequest):
    request_started = perf_counter()
    try:
        active_schema = get_schema()
        sql, explanation = await generate_sql(
            request.question,
            active_schema
        )
        sql = ensure_online_full_rows(sql, request.question, active_schema)
        safe_sql = validate_select(sql, settings.max_rows)

    except UnsafeQuery as exc:
        raise HTTPException(422, str(exc)) from exc

    except httpx.HTTPError as exc:
        logger.exception("Ollama request failed")
        raise HTTPException(
            503,
            "Ollama is unavailable. Start Ollama and pull the configured model."
        ) from exc

    except (SQLAlchemyError, ValueError) as exc:
        logger.exception("SQL generation or validation failed")
        raise HTTPException(
            502,
            f"Could not generate a safe query: {exc}"
        ) from exc

    query_started = perf_counter()
    try:
        with reader_engine.begin() as conn:
            conn.execute(
                text(
                    f"SET LOCAL statement_timeout = "
                    f"{int(settings.query_timeout_ms)}"
                )
            )

            result = conn.execute(text(safe_sql))
            columns = list(result.keys())

            rows = [
                {
                    key: serialize(value)
                    for key, value in row._mapping.items()
                }
                for row in result.fetchall()
            ]

    except SQLAlchemyError as exc:
        logger.exception(
            "Generated SQL execution failed. SQL: %s",
            safe_sql
        )
        raise HTTPException(
            status_code=422,
            detail="The generated query could not be executed."
        ) from exc

    summary = summarize_answer(
        request.question,
        safe_sql,
        columns,
        rows,
        explanation,
    )
    follow_ups = build_follow_up_questions(
        request.question,
        parse_online_columns(active_schema),
        columns,
        rows,
        safe_sql,
    )
    logger.info(
        "Online Retail ask completed in %.1f ms (database_query_ms=%.1f, rows=%d)",
        (perf_counter() - request_started) * 1000,
        (perf_counter() - query_started) * 1000,
        len(rows),
    )
    return AskResponse(
        sql=safe_sql,
        explanation=summary,
        columns=columns,
        rows=rows,
        row_count=len(rows),
        follow_up_questions=follow_ups,
    )


@app.post(
    "/api/datasets/{dataset_id}/ask",
    response_model=AskResponse,
)
async def ask_dataset(dataset_id: UUID, request: AskRequest):
    request_started = perf_counter()
    try:
        with reader_engine.connect() as conn:
            dataset_row = conn.execute(
                select(UploadedDataset.__table__).where(
                    UploadedDataset.dataset_id == dataset_id
                )
            ).mappings().one_or_none()
            dataset = UploadedDataset(**dict(dataset_row)) if dataset_row else None
    except SQLAlchemyError as exc:
        logger.exception("Dataset metadata retrieval failed")
        raise HTTPException(
            status_code=503,
            detail="Dataset metadata is unavailable.",
        ) from exc

    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")

    try:
        sql, explanation = await generate_dataset_sql(
            request.question,
            dataset.dataset_name,
            dataset.schema_name,
            dataset.table_name,
            dataset.columns_info,
        )

        if not sql:
            logger.info(
                "Uploaded dataset ask completed without SQL in %.1f ms",
                (perf_counter() - request_started) * 1000,
            )
            return AskResponse(
                sql="",
                explanation=explanation,
                columns=[],
                rows=[],
                row_count=0,
                follow_up_questions=build_follow_up_questions(
                    request.question,
                    dataset.columns_info,
                    [],
                    [],
                ),
            )

        sql = ensure_dataset_full_rows(sql, request.question, dataset.table_name)
        safe_sql = validate_select(
            sql,
            settings.max_rows,
            allowed_tables={dataset.table_name},
            allowed_schemas={dataset.schema_name},
        )

    except UnsafeQuery as exc:
        raise HTTPException(422, str(exc)) from exc

    except httpx.HTTPError as exc:
        logger.exception("Ollama request failed for uploaded dataset")
        raise HTTPException(
            status_code=503,
            detail="Ollama is unavailable. Start Ollama and pull the configured model.",
        ) from exc

    except (SQLAlchemyError, ValueError) as exc:
        logger.exception("Dataset SQL generation or validation failed")
        raise HTTPException(
            status_code=502,
            detail=f"Could not generate a safe query: {exc}",
        ) from exc

    query_started = perf_counter()
    try:
        with reader_engine.begin() as conn:
            conn.execute(
                text(
                    f"SET LOCAL statement_timeout = "
                    f"{int(settings.query_timeout_ms)}"
                )
            )
            # All physical dataset table references must be qualified by the
            # validator, so keep unqualified names out of every user schema.
            conn.execute(text("SET LOCAL search_path TO pg_catalog"))

            result = conn.execute(text(safe_sql))
            columns = list(result.keys())
            rows = [
                {
                    key: serialize(value)
                    for key, value in row._mapping.items()
                }
                for row in result.fetchmany(settings.max_rows)
            ]

    except SQLAlchemyError as exc:
        logger.exception("Generated dataset query execution failed. SQL: %s", safe_sql)
        raise HTTPException(
            status_code=422,
            detail="The generated query could not be executed.",
        ) from exc

    summary = summarize_answer(
        request.question,
        safe_sql,
        columns,
        rows,
        explanation,
    )
    follow_ups = build_follow_up_questions(
        request.question,
        dataset.columns_info,
        columns,
        rows,
        safe_sql,
    )
    logger.info(
        "Uploaded dataset ask completed in %.1f ms (database_query_ms=%.1f, rows=%d)",
        (perf_counter() - request_started) * 1000,
        (perf_counter() - query_started) * 1000,
        len(rows),
    )
    return AskResponse(
        sql=safe_sql,
        explanation=summary,
        columns=columns,
        rows=rows,
        row_count=len(rows),
        follow_up_questions=follow_ups,
    )


app.include_router(datasets_router)
