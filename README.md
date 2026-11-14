# SQLPilot

SQLPilot is a local natural-language analytics MVP for retail transactions. Ask a question, inspect the generated PostgreSQL, and view the bounded query results.

## Dataset

The import pipeline uses [UCI Online Retail (dataset 352)](https://archive.ics.uci.edu/dataset/352/online+retail), distributed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Cite: Chen, D. (2015), *Online Retail*, UCI Machine Learning Repository, DOI [10.24432/C5BW33](https://doi.org/10.24432/C5BW33). UCI provides a direct ZIP at `https://archive.ics.uci.edu/static/public/352/online+retail.zip`.

The source is one invoice-line workbook. The pipeline normalizes it into:

- `customers`: source customer ID and country (rows without a customer ID remain valid orders with a null customer).
- `orders`: invoice number, date, customer and cancellation flag (`C` invoices are cancellations).
- `products`: stock code and description.
- `order_items`: each invoice line, quantity and unit price.

### Business rules

- Revenue is calculated as `quantity * unit_price`.
- Exclude cancelled invoices (`orders.is_cancelled = TRUE`).
- Exclude order lines where `quantity <= 0` or `unit_price <= 0`.
- Overall revenue queries should not require a customer join; orders with missing customer IDs still contribute to sales.
- Customer-specific analysis excludes rows with a null customer ID.
- All monetary values are in GBP.

The downloaded source file is kept in ignored `data/raw/` and is not committed.

## Run with Docker Compose

1. Copy `.env.example` to `.env`; replace the development passwords if desired. The Compose database initialization reads `SQLPILOT_READONLY_PASSWORD` from this file and applies it to the reader role.
2. Start PostgreSQL, the API and the UI:

   ```sh
   docker compose up --build -d
   ```

   The web and API services mount their source folders and reload code changes, so
   frontend and backend edits are visible without relying on an old image layer.
   Rebuild with `--build` after changing dependencies or a Dockerfile.

3. Pull the local language model once (about 2 GB depending on the model):

   ```sh
   docker compose exec ollama ollama pull qwen2.5-coder:3b
   ```

4. Import the full dataset. This downloads the public workbook on first run:

   ```sh
   docker compose exec api python -m scripts.import_data
   ```

   For a quick smaller import, use `--limit 10000`.
5. Open [http://localhost:5173](http://localhost:5173). API docs are at [http://localhost:8000/docs](http://localhost:8000/docs).

The first Compose run initializes a PostgreSQL volume and grants the dedicated `sqlpilot_reader` role access. Removing the `postgres_data` volume resets the database and applies database initialization again: `docker compose down -v`.

## Upload a CSV dataset

Start the API and its Compose dependencies if they are not already running:

```sh
docker compose up --build -d api
```

Create a small UTF-8 CSV sample and upload it:

```sh
sample_csv="$(mktemp)"
cat > "$sample_csv" <<'CSV'
customer_id,order_total,order_date
001,12.50,2025-01-30
002,18.75,2025-01-31
CSV
curl --fail-with-body -X POST http://localhost:8000/api/datasets/upload \
  -F "file=@${sample_csv};filename=sample.csv;type=text/csv"
rm "$sample_csv"
```

The response includes the generated dataset UUID, display name, isolated PostgreSQL schema and table, normalized and original column names, inferred types, and row and column counts. Uploads are limited to 10 MiB and 100 columns.

For a full-stack smoke test with the UCI Bike Sharing `day.csv` and the uploaded-dataset query endpoint, see [Architecture and smoke tests](docs/architecture.md#manual-smoke-test).

Run backend tests from the repository root:

```sh
cd backend
python -m pip install -r requirements.txt
python -m pytest
```

## Run backend locally

Requires Python 3.12+, PostgreSQL 16+, and Ollama. Set the variables from `.env.example` in your shell or copy it to `.env` at the repository root, changing the database host from `db` to `localhost` in both URLs for local processes. The SQL schema is initialized with the writer URL; data queries use the separate reader URL.

```sh
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m app.init_db
python -m scripts.import_data --limit 10000
uvicorn app.main:app --reload
```

The React development server runs with `cd frontend && npm ci && npm run dev`; its `/api` requests are proxied to `localhost:8000`.

## Architecture and safety

- The FastAPI app introspects the live schema through SQLAlchemy and sends only that schema plus the question to Ollama.
- Schema metadata is cached for 30 seconds between asks; the UI forces a refresh on page load. API logs report schema refresh, Ollama generation, database query, and total request timings.
- SQLGlot parses a single PostgreSQL `SELECT`, rejects non-allowlisted tables, and injects or caps a `LIMIT`.
- Query execution uses only `sqlpilot_reader`, which has `SELECT` grants; a transaction-local `statement_timeout` limits query time. The API reads at most the capped result set.
- `DATABASE_URL` is reserved for initialization/import; `READONLY_DATABASE_URL` is used for all schema introspection and query execution.
- Ollama runs locally. The prompt requests structured JSON containing SQL and an explanation. Review the SQL shown in the UI for analytical correctness.

Uploaded CSV handling, per-dataset schema isolation, request flow, API routes, and a repeatable Bike Sharing smoke test are described in [docs/architecture.md](docs/architecture.md).

## Checks

```sh
cd backend && python -m pytest
cd frontend && npm ci && npm run build
```
