import logging
from datetime import date, datetime
from decimal import Decimal

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .config import settings
from .db import reader_engine
from .llm import generate_sql
from .schema import get_schema
from .sql_safety import UnsafeQuery, validate_select

logger = logging.getLogger(__name__)

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
def schema():
    try:
        return {"schema": get_schema()}
    except SQLAlchemyError as exc:
        logger.exception("Schema retrieval failed")
        raise HTTPException(
            status_code=503,
            detail="Database schema is unavailable"
        ) from exc


@app.post("/api/ask", response_model=AskResponse)
async def ask(request: AskRequest):
    try:
        sql, explanation = await generate_sql(
            request.question,
            get_schema()
        )
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

    return AskResponse(
        sql=safe_sql,
        explanation=explanation,
        columns=columns,
        rows=rows,
        row_count=len(rows)
    )
