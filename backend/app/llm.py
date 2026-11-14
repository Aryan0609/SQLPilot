import json
import re

import httpx

from .config import settings


async def generate_sql(question: str, schema: str) -> tuple[str, str]:

    prompt = f"""
You are SQLPilot, an expert PostgreSQL data analyst.

Your task is to convert the user's natural-language question
into exactly one correct, read-only PostgreSQL query.

DATABASE SCHEMA:
{schema}

BUSINESS CONTEXT:

- Monetary values are in GBP.
- Line-item revenue = quantity * unit_price.
- orders.is_cancelled identifies cancelled invoices.
- Completed sales must exclude cancelled invoices.
- For sales and revenue calculations, exclude order_items
  where quantity <= 0 or unit_price <= 0.
- Negative quantities represent returns or corrections.
- Customers without a known customer_id must be excluded
  from customer-specific analysis.
- Do not exclude missing customer IDs from overall sales
  or revenue calculations unless the question explicitly
  requires customer filtering.
- Do not join tables unnecessarily.
- Use the foreign-key relationships supplied in the schema.
- Do not assume that an order-item row represents an invoice.
- COUNT(DISTINCT invoice_no) counts invoices.
- COUNT(*) counts rows or line items depending on the query.

SQL GENERATION RULES:

1. Generate valid PostgreSQL syntax.
2. Use only tables and columns present in the schema.
3. Use explicit JOIN conditions.
4. Qualify column names with table aliases when ambiguous.
5. Use GROUP BY for non-aggregated selected columns.
6. Use HAVING when filtering grouped aggregate results.
7. Use CTEs when a calculation requires multiple aggregation stages.
8. For cumulative calculations:
   - First aggregate data at the required reporting level.
   - Then apply a window function to the aggregated result.
   - Never apply a window aggregate directly to raw rows
     when the query is already grouped at another level.
9. For previous-period comparisons, use LAG() over an
   appropriately ordered aggregated result.
10. For ranking, use RANK() or ROW_NUMBER() as appropriate.
11. For percentage contribution, divide the group aggregate
    by the overall aggregate using a window function or CTE.
12. Protect divisions using NULLIF(denominator, 0).
13. Do not invent columns, relationships, or business rules.
14. Do not add unnecessary joins, DISTINCT clauses, or filters.
15. Return a single SELECT statement or a WITH ... SELECT statement.
16. Do not include Markdown fences or commentary inside SQL.
17. Apply a reasonable LIMIT when the question requests a
    limited result set. The application also enforces its own
    maximum row limit.

IMPORTANT AGGREGATION EXAMPLE:

For monthly revenue and cumulative revenue, use this structure:

WITH monthly_sales AS (
    SELECT
        DATE_TRUNC('month', o.invoice_date) AS month,
        SUM(oi.quantity * oi.unit_price) AS monthly_revenue
    FROM orders o
    JOIN order_items oi
        ON o.invoice_no = oi.invoice_no
    WHERE o.is_cancelled = FALSE
      AND oi.quantity > 0
      AND oi.unit_price > 0
    GROUP BY DATE_TRUNC('month', o.invoice_date)
)
SELECT
    month,
    monthly_revenue,
    SUM(monthly_revenue) OVER (
        ORDER BY month
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS cumulative_revenue
FROM monthly_sales
ORDER BY month;

OUTPUT REQUIREMENTS:

Return valid JSON with exactly these two keys:

{{
  "sql": "The PostgreSQL query",
  "explanation": "A concise explanation of the query"
}}

USER QUESTION:
{question}
"""

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            f"{settings.ollama_base_url.rstrip('/')}/api/generate",
            json={
                "model": settings.ollama_model,
                "prompt": prompt,
                "stream": False,
                "format": "json"
            }
        )

        response.raise_for_status()

    payload = json.loads(response.json()["response"])

    sql = str(payload.get("sql", "")).strip()

    sql = re.sub(
        r"^```(?:sql)?\s*|\s*```$",
        "",
        sql,
        flags=re.IGNORECASE
    ).strip()

    explanation = str(
        payload.get(
            "explanation",
            "Query generated from the available retail schema."
        )
    )

    if not sql:
        raise ValueError("The model returned no SQL")

    return sql, explanation
