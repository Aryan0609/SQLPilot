# SQLPilot architecture

SQLPilot has two query paths: the built-in UCI Online Retail database and user-uploaded CSV datasets. Both produce a PostgreSQL `SELECT` that SQLPilot validates before executing with the read-only database connection. The paths use separate prompts and table scopes so an uploaded dataset cannot query the retail tables or another upload.

## Components and request flow

### Built-in Online Retail

1. The React frontend loads `GET /api/schema` to show the live retail schema.
2. A question is sent to `POST /api/ask`.
3. FastAPI introspects the retail schema through SQLAlchemy and sends that schema, the question, and the retail business rules to the configured local Ollama model.
4. The model returns JSON containing SQL and an explanation. SQLGlot parses and checks the SQL, pins unqualified retail tables to `public`, and inserts or caps the row limit.
5. The API executes the query through `READONLY_DATABASE_URL`, applies a transaction-local statement timeout, and returns the SQL, explanation, columns, and rows.

### Uploaded CSV

1. The frontend sends a selected `.csv` file as multipart form data to `POST /api/datasets/upload`.
2. The API enforces the 10 MiB limit, decodes UTF-8, checks the CSV structure and 100-column limit, and rejects malformed rows and duplicate headers. It removes trailing columns only when their headers are blank and every value in those columns is empty or whitespace. Blank headers containing data are rejected with their column positions, and empty blank headers between named columns are rejected.
3. Empty cells under named headers are retained and inserted as SQL `NULL`. Remaining headers are converted to safe PostgreSQL identifiers. Column types are inferred from non-empty values; recognized integers, decimals, booleans, dates, and timestamps are inserted as native values. Ambiguous or mixed values remain text. The original header is retained alongside the SQL name and type.
4. In one writer transaction, the API creates a UUID-named schema (`dataset_<uuid hex>`) containing a `records` table, inserts the rows, stores dataset metadata in `public.uploaded_datasets`, and grants the reader role usage and `SELECT` on the uploaded schema and table.
5. The upload response supplies the dataset ID, schema/table name, column metadata, and row/column counts. The frontend displays this information and keeps the uploaded dataset details in browser storage so the user can select it again later.
6. A question is sent to `POST /api/datasets/{dataset_id}/ask`. The API retrieves that dataset's metadata and sends the model only its name, qualified table, column names/types, and the question; CSV row contents are not included in the prompt.
7. SQLGlot requires physical table references to use the selected dataset's schema and table. The API runs the checked query with `search_path` limited to `pg_catalog`, a transaction-local timeout, and the configured row cap, then returns the same result shape as the retail route.

Dataset rows and metadata live in PostgreSQL. Browser storage retains the returned dataset details and selection for the frontend; it is not the source of truth for query authorization. The API loads the selected dataset's schema and columns from database metadata for each query.

## SQL generation and safeguards

The retail prompt includes the live Online Retail schema and the project rules for cancellations, returns, missing customer IDs, and GBP revenue. The uploaded-dataset prompt is built from stored metadata only and instructs the model to use one fully qualified table and the listed columns. Both prompts request a JSON object with `sql` and `explanation`.

Before execution, `backend/app/sql_safety.py` uses SQLGlot's PostgreSQL parser to require one `SELECT`, reject mutating statements, `SELECT INTO`, locking reads, cross-dataset or unapproved table references, and the listed dangerous functions. Retail references are limited to the four retail tables in `public`; uploaded queries must reference the selected table in its selected schema. Query limits are added or capped at `MAX_ROWS`.

The API uses the writer connection only for database initialization, retail import, and CSV ingestion. Schema inspection, health checks, dataset metadata lookup, and query execution use `READONLY_DATABASE_URL`. The database role is granted `SELECT` access to retail tables and uploaded dataset tables. Query execution also has a transaction-local `statement_timeout`, and uploaded-dataset result fetching is capped by the configured row limit.

These checks protect the database scope and bound query execution. The generated SQL and its explanation remain visible in the UI so users can inspect the model's interpretation.

## API routes

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Check the reader database connection. |
| `GET` | `/api/schema` | Return the live built-in Online Retail schema for the frontend. |
| `POST` | `/api/ask` | Generate and run a query against Online Retail. |
| `POST` | `/api/datasets/upload` | Validate and ingest a CSV, then return its dataset metadata. |
| `POST` | `/api/datasets/{dataset_id}/ask` | Generate and run a query against one uploaded dataset. |

The frontend lets users choose Online Retail or a saved upload, previews schema and counts, suggests questions from uploaded column types, and shows the submitted question, generated SQL, explanation, and result rows.

## Manual smoke test

The automated backend tests mock Ollama and database connections. This keeps CI deterministic while exercising upload parsing, schema/type metadata, SQL validation, route selection, and result handling. Use the following steps for a live check with the full UCI Bike Sharing dataset and a running Ollama model.

1. Start the stack, pull the configured model, and import Online Retail as described in the [README setup steps](../README.md#run-with-docker-compose):

   ```sh
   docker compose up --build -d
   docker compose exec ollama ollama pull qwen2.5-coder:3b
   docker compose exec api python -m scripts.import_data --limit 10000
   ```

2. Download the UCI [Bike Sharing dataset](https://archive.ics.uci.edu/dataset/275/bike%2Bsharing%2Bdataset) and extract its daily CSV:

   ```sh
   mkdir -p /tmp/sqlpilot-bike
   curl -fL 'https://archive.ics.uci.edu/static/public/275/bike+sharing+dataset.zip' \
     -o /tmp/sqlpilot-bike/bike-sharing.zip
   unzip -p /tmp/sqlpilot-bike/bike-sharing.zip day.csv > /tmp/sqlpilot-bike/day.csv
   ```

3. Upload `day.csv` and save the returned dataset ID:

   ```sh
   upload_response=$(curl --fail-with-body -sS \
     -F 'file=@/tmp/sqlpilot-bike/day.csv;type=text/csv' \
     http://localhost:8000/api/datasets/upload)
   printf '%s\n' "$upload_response"
   dataset_id=$(printf '%s' "$upload_response" | python -c \
     'import json,sys; print(json.load(sys.stdin)["dataset_id"])')
   ```

   Confirm the response reports 731 rows and includes fields such as `dteday` (`DATE`), `season` (`BIGINT`), `temp` (`NUMERIC`), and `cnt` (`BIGINT`).

4. Ask a question through the uploaded-dataset route:

   ```sh
   curl --fail-with-body -sS \
     -H 'Content-Type: application/json' \
     -d '{"question":"What was the average daily rental count by season, ordered by season?"}' \
     "http://localhost:8000/api/datasets/${dataset_id}/ask"
   ```

   Check that the response contains a qualified `dataset_<uuid>.records` query, a concise explanation, and season-level result rows. In the UI, the upload should also appear in the dataset selector with its row count and detected schema.

5. Check that the built-in route still works by asking a question such as “What were the top 10 products by revenue?” with Online Retail selected. The request should use `POST /api/ask` and return retail results.

The model may phrase SQL and explanations differently across runs. Review the generated SQL and verify that it uses only the selected dataset columns and its qualified table.

## Existing evaluation assets

The existing benchmark references remain in `evaluation/datasets/benchmark.json` and `evaluation/results/baseline.csv`. They are separate from the route-level regression tests described above.
