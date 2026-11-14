import csv
import json
import math
import re
import subprocess
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BENCHMARK = ROOT / "datasets" / "benchmark.json"
OUTPUT = ROOT / "results" / "baseline.csv"

API_URL = "http://localhost:8000/api/ask"
API_TIMEOUT = 180
MAX_ROWS = 200
TOLERANCE = 0.01
DIAGNOSTIC_SAMPLE_SIZE = 3


def load_benchmark():
    with open(BENCHMARK, encoding="utf-8") as file:
        return json.load(file)


def ask_sqlpilot(question):
    payload = json.dumps({"question": question}).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    start = time.perf_counter()

    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:
            body = response.read().decode("utf-8", errors="replace")

        latency = round(time.perf_counter() - start, 3)
        try:
            return json.loads(body), latency, None
        except json.JSONDecodeError as error:
            # The HTTP request succeeded, but the response cannot be evaluated.
            return {
                "_invalid_json": True,
                "_response_body": body[:1000],
                "_parse_error": str(error),
            }, latency, None

    except urllib.error.HTTPError as error:
        latency = round(time.perf_counter() - start, 3)
        body = error.read().decode("utf-8", errors="replace").strip()

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = None

        detail = payload.get("detail") if isinstance(payload, dict) else None
        message = detail or body or error.reason
        return None, latency, f"HTTP {error.code}: {message}"

    except Exception as error:
        latency = round(time.perf_counter() - start, 3)
        return None, latency, str(error)


def execute_reference(sql):
    sql = sql.strip().rstrip(";")

    # Match SQLPilot's maximum response size.
    if not re.search(r"\bLIMIT\s+\d+\b", sql, re.IGNORECASE):
        sql += " LIMIT 200"

    wrapped_sql = (
        "SELECT COALESCE(json_agg(row_to_json(q)), '[]'::json)::text "
        f"FROM ({sql}) AS q"
    )

    command = [
        "docker", "compose", "exec", "-T", "db",
        "sh", "-lc",
        'psql -X -q -A -t -v ON_ERROR_STOP=1 '
        '-U "$POSTGRES_USER" -d sqlpilot -c "$1"',
        "sh",
        wrapped_sql,
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=60,
    )

    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())

    rows = json.loads(result.stdout.strip() or "[]")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Reference query did not return a list of object rows")
    return rows


def value_equal(expected, actual):
    if expected is None or actual is None:
        return expected is None and actual is None

    # bool is a subclass of int in Python, so check it before numeric values.
    if isinstance(expected, bool) or isinstance(actual, bool):
        return (
            isinstance(expected, bool)
            and isinstance(actual, bool)
            and expected is actual
        )

    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return math.isclose(
            float(expected),
            float(actual),
            rel_tol=1e-8,
            abs_tol=TOLERANCE,
        )

    return expected == actual


def rows_equal_by_name(expected, actual, columns):
    return all(value_equal(expected[column], actual[column]) for column in columns)


def validate_response(response):
    """Return (is_valid, normalized_response, diagnostic_details)."""
    if not isinstance(response, dict):
        return False, {}, {
            "summary": "API response must be a JSON object.",
            "response_type": type(response).__name__,
        }

    if response.get("_invalid_json"):
        return False, response, {
            "summary": "API returned a successful HTTP response with invalid JSON.",
            "parse_error": response.get("_parse_error", ""),
            "response_body_sample": response.get("_response_body", ""),
        }

    sql = response.get("sql")
    columns = response.get("columns")
    rows = response.get("rows")
    explanation = response.get("explanation")
    row_count = response.get("row_count")

    issues = []
    if not isinstance(sql, str) or not sql.strip():
        issues.append("sql must be a non-empty string")
    if not isinstance(columns, list) or any(not isinstance(column, str) for column in columns):
        issues.append("columns must be a list of strings")
    elif len(columns) != len(set(columns)):
        issues.append("columns must not contain duplicate names")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        issues.append("rows must be a list of objects")
    if not isinstance(explanation, str):
        issues.append("explanation must be a string")
    if isinstance(row_count, bool) or not isinstance(row_count, int):
        issues.append("row_count must be an integer")
    elif isinstance(rows, list) and row_count != len(rows):
        issues.append(f"row_count says {row_count}, but rows contains {len(rows)}")

    if isinstance(columns, list) and all(isinstance(column, str) for column in columns):
        if isinstance(rows, list):
            expected_keys = set(columns)
            for index, row in enumerate(rows):
                if isinstance(row, dict) and set(row) != expected_keys:
                    issues.append(
                        f"row {index + 1} keys do not match the columns list"
                    )
                    break

    if issues:
        return False, response, {
            "summary": "API response has an invalid shape: " + "; ".join(issues),
            "issues": issues,
            "response_fields": sorted(response),
        }

    return True, response, {}


def compare_results(expected, actual, actual_columns, ordered):
    """Compare results by column name and return a pass flag and diagnostics."""
    expected_columns = list(expected[0]) if expected else []
    expected_column_set = set(expected_columns)
    actual_column_set = set(actual_columns)
    missing_columns = [
        column for column in expected_columns if column not in actual_column_set
    ]
    unexpected_columns = [
        column for column in actual_columns if column not in expected_column_set
    ]

    diagnostics = {
        "summary": "Results match.",
        "diagnostic_codes": [],
        "ordered": ordered,
        "expected_columns": expected_columns,
        "actual_columns": actual_columns,
        "missing_columns": missing_columns,
        "unexpected_columns": unexpected_columns,
        "expected_row_count": len(expected),
        "actual_row_count": len(actual),
    }

    if missing_columns or unexpected_columns:
        diagnostics["diagnostic_codes"].append("COLUMN_MISMATCH")
        parts = []
        if missing_columns:
            parts.append(f"missing columns: {missing_columns}")
        if unexpected_columns:
            parts.append(f"unexpected columns: {unexpected_columns}")
        diagnostics["summary"] = "Column mismatch; " + "; ".join(parts) + "."

        if len(expected) != len(actual):
            diagnostics["diagnostic_codes"].append("ROW_COUNT_MISMATCH")
            diagnostics["summary"] += (
                f" Row count also differs: expected {len(expected)}, got {len(actual)}."
            )
        return False, diagnostics

    if len(expected) != len(actual):
        diagnostics["diagnostic_codes"].append("ROW_COUNT_MISMATCH")

    if ordered:
        mismatch_rows = []
        for index, (expected_row, actual_row) in enumerate(zip(expected, actual)):
            if not rows_equal_by_name(expected_row, actual_row, expected_columns):
                cell_mismatches = [
                    {
                        "column": column,
                        "expected": expected_row[column],
                        "actual": actual_row[column],
                    }
                    for column in expected_columns
                    if not value_equal(expected_row[column], actual_row[column])
                ]
                mismatch_rows.append({
                    "row_number": index + 1,
                    "expected": expected_row,
                    "actual": actual_row,
                    "cell_mismatches": cell_mismatches,
                })
                break

        if mismatch_rows:
            diagnostics["diagnostic_codes"].append("VALUE_MISMATCH")
            diagnostics["first_mismatch"] = mismatch_rows[0]
            diagnostics["summary"] = (
                f"Ordered results first differ at row "
                f"{mismatch_rows[0]['row_number']}: "
                f"{len(mismatch_rows[0]['cell_mismatches'])} cell(s) differ."
            )

        if len(expected) > len(actual):
            missing_rows = expected[len(actual):]
            diagnostics["missing_row_count"] = len(missing_rows)
            diagnostics["missing_rows_sample"] = missing_rows[:DIAGNOSTIC_SAMPLE_SIZE]
        elif len(actual) > len(expected):
            unexpected_rows = actual[len(expected):]
            diagnostics["unexpected_row_count"] = len(unexpected_rows)
            diagnostics["unexpected_rows_sample"] = unexpected_rows[:DIAGNOSTIC_SAMPLE_SIZE]

    else:
        unmatched_actual = list(actual)
        missing_rows = []

        for expected_row in expected:
            match_index = next(
                (
                    index for index, actual_row in enumerate(unmatched_actual)
                    if rows_equal_by_name(expected_row, actual_row, expected_columns)
                ),
                None,
            )
            if match_index is None:
                missing_rows.append(expected_row)
            else:
                unmatched_actual.pop(match_index)

        unexpected_rows = unmatched_actual
        if missing_rows or unexpected_rows:
            diagnostics["diagnostic_codes"].append("VALUE_MISMATCH")
            diagnostics["missing_row_count"] = len(missing_rows)
            diagnostics["unexpected_row_count"] = len(unexpected_rows)
            diagnostics["missing_rows_sample"] = missing_rows[:DIAGNOSTIC_SAMPLE_SIZE]
            diagnostics["unexpected_rows_sample"] = unexpected_rows[:DIAGNOSTIC_SAMPLE_SIZE]
            diagnostics["summary"] = (
                f"Unordered results differ: {len(missing_rows)} expected row(s) "
                f"missing and {len(unexpected_rows)} unexpected row(s)."
            )

    codes = diagnostics["diagnostic_codes"]
    if len(expected) != len(actual) and "ROW_COUNT_MISMATCH" in codes:
        diagnostics["summary"] += (
            f" Row count differs: expected {len(expected)}, got {len(actual)}."
        )
    if codes and diagnostics["summary"] == "Results match.":
        diagnostics["summary"] = (
            f"Row count differs: expected {len(expected)}, got {len(actual)}."
        )

    return not codes, diagnostics


def result_record(
    item,
    status,
    *,
    latency="",
    generated_sql="",
    explanation="",
    expected=None,
    actual=None,
    expected_columns=None,
    actual_columns=None,
    diagnostic_code="",
    diagnostics=None,
    error="",
):
    diagnostic_codes = diagnostics.get("diagnostic_codes", []) if diagnostics else []
    return {
        "id": item["id"],
        "category": item["category"],
        "question": item["question"],
        "status": status,
        "accuracy": str(status == "PASS").upper(),
        "latency_seconds": latency,
        "generated_sql": generated_sql,
        "explanation": explanation,
        "reference_sql": item["reference_sql"],
        "expected_columns": json.dumps(expected_columns or []),
        "actual_columns": json.dumps(actual_columns or []),
        "expected_rows": json.dumps(expected, default=str) if expected is not None else "",
        "actual_rows": json.dumps(actual, default=str) if actual is not None else "",
        "diagnostic_code": diagnostic_code,
        "diagnostic_codes": json.dumps(diagnostic_codes),
        "diagnostics": json.dumps(diagnostics or {}, default=str),
        "error": str(error) if error else "",
    }


def ordered_reference(sql):
    return bool(re.search(r"\bORDER\s+BY\b", sql, re.IGNORECASE))


def main():
    benchmark = load_benchmark()
    results = []
    api_latencies = []

    print(f"\nSQLPilot evaluation: {len(benchmark)} questions\n")

    for index, item in enumerate(benchmark, start=1):
        print(f"[{index}/{len(benchmark)}] {item['id']}: {item['question']}")

        try:
            expected = execute_reference(item["reference_sql"])
        except Exception as error:
            diagnostics = {
                "summary": f"Reference query failed: {error}",
                "diagnostic_codes": ["REFERENCE_ERROR"],
            }
            print(f"  REFERENCE_ERROR: {error}")
            results.append(result_record(
                item,
                "REFERENCE_ERROR",
                diagnostic_code="REFERENCE_ERROR",
                diagnostics=diagnostics,
                error=error,
            ))
            continue

        response, latency, api_error = ask_sqlpilot(item["question"])
        api_latencies.append(latency)

        if api_error:
            diagnostics = {
                "summary": f"SQLPilot request failed: {api_error}",
                "diagnostic_codes": ["API_ERROR"],
            }
            status = "API_ERROR"
            diagnostic_code = "API_ERROR"
            generated_sql = ""
            explanation = ""
            actual_columns = []
            actual = []
            error = api_error
        else:
            response_valid, response, response_diagnostics = validate_response(response)
            generated_sql = response.get("sql", "") if isinstance(response, dict) else ""
            explanation = response.get("explanation", "") if isinstance(response, dict) else ""
            actual_columns = response.get("columns", []) if isinstance(response, dict) else []
            actual = response.get("rows", []) if isinstance(response, dict) else []
            error = ""

            if not response_valid:
                diagnostics = {
                    **response_diagnostics,
                    "diagnostic_codes": ["INVALID_RESPONSE"],
                }
                status = "INVALID_RESPONSE"
                diagnostic_code = "INVALID_RESPONSE"
            else:
                is_match, diagnostics = compare_results(
                    expected,
                    actual,
                    actual_columns,
                    ordered_reference(item["reference_sql"]),
                )
                status = "PASS" if is_match else "WRONG_RESULT"
                diagnostic_code = (
                    diagnostics["diagnostic_codes"][0]
                    if diagnostics["diagnostic_codes"] else ""
                )

        print(
            f"  {status} | Latency: {latency}s | "
            f"Expected rows: {len(expected)} | "
            f"Actual rows: {len(actual) if isinstance(actual, list) else 'invalid'}"
        )
        if status not in ("PASS",):
            print(f"  DIAGNOSTIC [{diagnostic_code}]: {diagnostics['summary']}")

        expected_columns = list(expected[0]) if expected else []
        results.append(result_record(
            item,
            status,
            latency=latency,
            generated_sql=generated_sql,
            explanation=explanation,
            expected=expected,
            actual=actual,
            expected_columns=expected_columns,
            actual_columns=actual_columns,
            diagnostic_code=diagnostic_code,
            diagnostics=diagnostics,
            error=error,
        ))

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "id", "category", "question", "status", "accuracy", "latency_seconds",
        "generated_sql", "explanation", "reference_sql", "expected_columns",
        "actual_columns", "expected_rows", "actual_rows", "diagnostic_code",
        "diagnostic_codes", "diagnostics", "error",
    ]
    with open(OUTPUT, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(results)

    passed = sum(row["status"] == "PASS" for row in results)
    wrong_results = sum(row["status"] == "WRONG_RESULT" for row in results)
    api_errors = sum(row["status"] == "API_ERROR" for row in results)
    invalid_responses = sum(row["status"] == "INVALID_RESPONSE" for row in results)
    reference_errors = sum(row["status"] == "REFERENCE_ERROR" for row in results)
    api_attempts = len(api_latencies)
    # Success means SQLPilot returned a structurally valid, evaluable result.
    # Accuracy is measured only on valid responses, so API failures do not lower it.
    api_successes = passed + wrong_results
    evaluated_results = passed + wrong_results
    # Latency includes successful, failed, and malformed API responses.
    api_success_rate = 100 * api_successes / api_attempts if api_attempts else 0
    result_accuracy = 100 * passed / evaluated_results if evaluated_results else 0
    average_api_latency = (
        sum(api_latencies) / api_attempts if api_attempts else 0
    )
    diagnostic_counts = Counter(
        code
        for row in results
        for code in json.loads(row["diagnostic_codes"])
    )

    print("\n" + "=" * 45)
    print("SQLPILOT EVALUATION SUMMARY")
    print("=" * 45)
    print(f"Total questions           : {len(results)}")
    print(f"API execution success rate: {api_success_rate:.2f}% ({api_successes}/{api_attempts})")
    print(f"Result accuracy           : {result_accuracy:.2f}% ({passed}/{evaluated_results})")
    print(f"Wrong-result count        : {wrong_results}")
    print(f"API error count           : {api_errors}")
    print(f"Invalid-response count    : {invalid_responses}")
    print(f"Reference-error count     : {reference_errors}")
    print(f"Average API latency       : {average_api_latency:.3f}s")
    if diagnostic_counts:
        print("Diagnostic counts         :")
        for code, count in sorted(diagnostic_counts.items()):
            print(f"  {code}: {count}")
    print(f"Results saved             : {OUTPUT}")


if __name__ == "__main__":
    main()
