"""Deterministic, data-grounded answer summaries and follow-up questions."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

import sqlglot
from sqlglot import exp


_ROW_NOUNS = r"rows?|records?|entries|transactions?|line items?"
_ENTITY_NOUNS = r"customers?|products?|orders?|invoices?"
_ROW_ACTIONS = r"show|list|display|inspect|view|see|return|give me|find"
_ANALYTIC_TERMS = re.compile(
    r"\b(top|rank|ranking|highest|lowest|most|least|average|mean|sum|total|"
    r"revenue|sales|count|compare|group|per|by)\b",
    re.IGNORECASE,
)


def wants_full_rows(question: str) -> bool:
    """Recognize requests to inspect records while leaving analytical questions alone."""
    normalized = " ".join(question.lower().split())
    explicit = re.search(
        rf"\b(?:{_ROW_ACTIONS})\b.{{0,70}}\b(?:{_ROW_NOUNS}|all columns|full rows?|"
        rf"complete rows?|matching records?)\b|"
        rf"\b(?:all|complete|full|matching)\s+(?:{_ROW_NOUNS}|{_ENTITY_NOUNS})\b",
        normalized,
    )
    if explicit:
        return True

    entity_list = re.search(
        rf"\b(?:{_ROW_ACTIONS})\b.{{0,50}}\b{_ENTITY_NOUNS}\b",
        normalized,
    )
    return bool(entity_list and not _ANALYTIC_TERMS.search(normalized))


def ensure_dataset_full_rows(sql: str, question: str, table_name: str) -> str:
    """Expand simple uploaded-table projections for explicit record requests.

    Filtering, ordering, and LIMIT clauses are retained. More complex aggregate
    queries are left for validation and execution rather than being rewritten.
    """
    if not wants_full_rows(question):
        return sql

    try:
        statement = sqlglot.parse_one(sql, read="postgres")
    except sqlglot.errors.ParseError:
        return sql

    if not isinstance(statement, exp.Select):
        return sql
    from_clause = statement.args.get("from") or statement.args.get("from_")
    source = from_clause.this if from_clause else None
    if not isinstance(source, exp.Table) or source.name.lower() != table_name.lower():
        return sql
    if statement.args.get("joins") or statement.args.get("group") \
            or statement.args.get("having"):
        return sql
    if any(statement.find_all(exp.AggFunc)):
        return sql

    statement.set("expressions", [exp.Star()])
    return statement.sql(dialect="postgres")


def ensure_online_full_rows(sql: str, question: str, schema: str = "") -> str:
    """Expand simple Online Retail record selections to all source-table fields."""
    if not wants_full_rows(question):
        return sql
    try:
        statement = sqlglot.parse_one(sql, read="postgres")
    except sqlglot.errors.ParseError:
        return sql
    if not isinstance(statement, exp.Select):
        return sql
    if statement.args.get("group") or statement.args.get("having") \
            or any(statement.find_all(exp.AggFunc)):
        return sql

    from_clause = statement.args.get("from") or statement.args.get("from_")
    sources = [from_clause.this] if from_clause else []
    sources.extend(join.this for join in statement.args.get("joins") or [])
    if not sources or not all(isinstance(source, exp.Table) for source in sources):
        return sql
    aliases = [source.alias_or_name for source in sources]
    if len(sources) > 1:
        fields_by_table: dict[str, list[str]] = {}
        for column in parse_online_columns(schema):
            fields_by_table.setdefault(column["table"], []).append(column["name"])
        projections = []
        for source, alias in zip(sources, aliases):
            fields = fields_by_table.get(source.name.lower())
            if not fields:
                return sql
            for field in fields:
                reference = exp.Column(
                    this=exp.to_identifier(field, quoted=True),
                    table=exp.to_identifier(alias),
                )
                projections.append(exp.alias_(reference, f"{alias}_{field}"))
        statement.set("expressions", projections)
        return statement.sql(dialect="postgres")

    statement.set(
        "expressions",
        [
            exp.Column(this=exp.Star(), table=exp.to_identifier(alias))
            for alias in aliases
        ],
    )
    return statement.sql(dialect="postgres")


def _pretty_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.replace("_", " ")).strip()


def _sentence_name(name: str) -> str:
    label = _pretty_name(name)
    return label[:1].upper() + label[1:]


def _display_value(value: Any) -> str:
    if value is None:
        return "no value"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return f"{value:,}"
    text = str(value)
    return text if len(text) <= 120 else f"{text[:117]}…"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _date_value(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _question_requests_trend(question: str) -> bool:
    return bool(re.search(
        r"\b(trend|change|changed|over time|month|monthly|year|yearly|"
        r"weekly|daily|increase|decrease|growth|decline)\b",
        question,
        re.IGNORECASE,
    ))


def summarize_answer(
    question: str,
    sql: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    fallback_explanation: str = "",
) -> str:
    """Summarize only values present in the bounded query result."""
    if not sql:
        return fallback_explanation.strip() or "I couldn't form a query from the available fields."
    if not rows:
        return "The query ran successfully, but no matching rows were returned."

    if wants_full_rows(question):
        count = len(rows)
        return (
            f"The query returned {count:,} matching "
            f"{'record' if count == 1 else 'records'}. Each returned row is shown in full below."
        )

    numeric_columns = [
        column for column in columns
        if any(_number(row.get(column)) is not None for row in rows)
    ]
    non_numeric_columns = [
        column for column in columns
        if column not in numeric_columns
        and any(row.get(column) is not None for row in rows)
    ]
    measure_terms = re.compile(
        r"\b(total|sum|average|avg|mean|revenue|sales|count|quantity|amount|"
        r"price|rate|profit|margin|value|volume|growth|rentals?)\b",
        re.IGNORECASE,
    )
    ranked_measures = sorted(
        numeric_columns,
        key=lambda name: (
            2 if measure_terms.search(name.replace("_", " ")) else 0,
            1 if re.search(rf"(?<!\w){re.escape(name.lower())}(?!\w)", question.lower()) else 0,
            columns.index(name),
        ),
        reverse=True,
    )
    measure_columns = ranked_measures[:1]
    dimension_columns = [name for name in numeric_columns if name not in measure_columns]
    categorical_columns = non_numeric_columns + dimension_columns

    if _question_requests_trend(question) and measure_columns and categorical_columns:
        for dimension in categorical_columns:
            dated_rows = [
                (_date_value(row.get(dimension)), row)
                for row in rows
            ]
            dated_rows = [(moment, row) for moment, row in dated_rows if moment is not None]
            if len(dated_rows) >= 2:
                dated_rows.sort(key=lambda pair: pair[0].timestamp())
                first_date, first_row = dated_rows[0]
                last_date, last_row = dated_rows[-1]
                measure = measure_columns[0]
                first_value = _number(first_row.get(measure))
                last_value = _number(last_row.get(measure))
                if first_value is not None and last_value is not None:
                    direction = "rose" if last_value > first_value else "fell" if last_value < first_value else "was unchanged"
                    return (
                        f"{_sentence_name(measure)} {direction} from "
                        f"{_display_value(first_row.get(measure))} on {first_date.date().isoformat()} "
                        f"to {_display_value(last_row.get(measure))} on {last_date.date().isoformat()} "
                        "across the returned time points."
                    )

    if len(rows) == 1:
        details = [
            f"{_pretty_name(column)}: {_display_value(rows[0].get(column))}"
            for column in columns[:4]
        ]
        return "The result is " + "; ".join(details) + "."

    if measure_columns and categorical_columns:
        measure = measure_columns[0]
        valid_rows = [row for row in rows if _number(row.get(measure)) is not None]
        if valid_rows:
            asks_for_lowest = bool(re.search(
                r"\b(lowest|least|smallest|fewest|bottom)\b",
                question,
                re.IGNORECASE,
            ))
            selected_row = (
                min(valid_rows, key=lambda row: _number(row.get(measure)) or 0)
                if asks_for_lowest
                else max(valid_rows, key=lambda row: _number(row.get(measure)) or 0)
            )
            category = categorical_columns[0]
            rank = "lowest" if asks_for_lowest else "highest"
            return (
                f"Among the returned results, {_display_value(selected_row.get(category))} "
                f"has the {rank} {_pretty_name(measure)} at "
                f"{_display_value(selected_row.get(measure))}."
            )

    if measure_columns:
        measure = measure_columns[0]
        values = [_number(row.get(measure)) for row in rows]
        values = [value for value in values if value is not None]
        if values:
            return (
                f"The query returned {len(rows):,} rows. {_sentence_name(measure)} "
                f"ranges from {_display_value(min(values))} to {_display_value(max(values))}."
            )

    first_row = rows[0]
    sample = "; ".join(
        f"{_pretty_name(column)}: {_display_value(first_row.get(column))}"
        for column in columns[:3]
    )
    return f"The query returned {len(rows):,} rows. First result: {sample}."


def parse_online_columns(schema: str) -> list[dict[str, str]]:
    """Extract table and field names from the schema text sent to the model."""
    available: list[dict[str, str]] = []
    current_table = ""
    for line in schema.splitlines():
        table_match = re.match(r"^([a-zA-Z_][\w]*):$", line)
        if table_match:
            current_table = table_match.group(1)
            continue
        column_match = re.match(r"^\s+-\s+([\w]+)\s+(.+?)\s+(?:NULL|NOT NULL)$", line)
        if current_table and column_match:
            available.append({
                "name": column_match.group(1),
                "postgres_type": column_match.group(2),
                "table": current_table,
            })
    return available


def build_follow_up_questions(
    question: str,
    columns_info: list[dict[str, Any]],
    result_columns: list[str],
    rows: list[dict[str, Any]],
    sql: str = "",
) -> list[str]:
    """Create up to three prompts using only schema fields and returned values."""
    normalized_columns: list[dict[str, str]] = []
    for column in columns_info:
        name = column.get("name")
        postgres_type = column.get("postgres_type", column.get("type", "TEXT"))
        if isinstance(name, str) and isinstance(postgres_type, str):
            normalized_columns.append({"name": name, "postgres_type": postgres_type})
    if not normalized_columns:
        return ["Show the first 10 rows."]

    active_text = f"{question} {sql}".lower()
    output_names = {name.lower() for name in result_columns}

    def is_unrequested_identifier(column: dict[str, str]) -> bool:
        name = column["name"].lower()
        identifier_like = re.search(r"(?:^|_)(?:id|no|number|code)$", name)
        if not identifier_like or name in output_names:
            return False
        return not re.search(rf"(?<!\w){re.escape(name)}(?!\w)", question, re.I)

    def score(column: dict[str, str]) -> int:
        name = column["name"]
        points = 3 if name.lower() in output_names else 0
        if re.search(rf"(?<!\w){re.escape(name.lower())}(?!\w)", active_text):
            points += 2
        return points

    numeric = [
        column for column in normalized_columns
        if re.match(r"^(?:BIGINT|SMALLINT|INTEGER|INT|NUMERIC|DECIMAL|REAL|DOUBLE|FLOAT)", column["postgres_type"], re.I)
        and not is_unrequested_identifier(column)
    ]
    temporal = [
        column for column in normalized_columns
        if re.match(r"^(?:DATE|TIMESTAMP)", column["postgres_type"], re.I)
    ]
    categorical = [
        column for column in normalized_columns
        if not re.match(r"^(?:BIGINT|SMALLINT|INTEGER|INT|NUMERIC|DECIMAL|REAL|DOUBLE|FLOAT|DATE|TIMESTAMP)", column["postgres_type"], re.I)
        and not is_unrequested_identifier(column)
    ]
    numeric.sort(key=score, reverse=True)
    temporal.sort(key=score, reverse=True)
    categorical.sort(key=score, reverse=True)

    suggestions: list[str] = []
    if numeric and categorical:
        suggestions.append(
            f"How does {numeric[0]['name']} vary by {categorical[0]['name']}?"
        )
    if numeric and temporal:
        suggestions.append(
            f"How did {numeric[0]['name']} change over time using {temporal[0]['name']}?"
        )

    # Use a returned dimension and value only when the output header is an actual
    # source column in the selected dataset.
    for column in categorical:
        if column["name"].lower() not in output_names:
            continue
        value = next((row.get(column["name"]) for row in rows if row.get(column["name"]) is not None), None)
        if value is not None:
            suggestions.append(
                f"Show records where {column['name']} is '{_display_value(value)}'."
            )
            break

    if not suggestions:
        column = normalized_columns[0]["name"]
        suggestions = [
            f"How many rows have a value for {column}?",
            f"Show the first 10 rows including {column}.",
        ]
    elif len(suggestions) == 1:
        column = categorical[0]["name"] if categorical else normalized_columns[0]["name"]
        suggestions.append(f"Show the first 10 rows including {column}.")

    current = " ".join(question.lower().split()).rstrip("?.!")
    unique: list[str] = []
    for suggestion in suggestions:
        normalized = suggestion.lower().rstrip("?.!")
        if normalized == current or normalized in {item.lower().rstrip("?.!") for item in unique}:
            continue
        unique.append(suggestion)
        if len(unique) == 3:
            break
    return unique
