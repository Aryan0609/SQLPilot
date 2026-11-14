import React from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';

type DatasetColumn = {
  name: string;
  original_name: string;
  postgres_type: string;
};

type UploadedDataset = {
  dataset_id: string;
  dataset_name: string;
  schema: { name: string; table_name: string; columns: DatasetColumn[] };
  row_count: number;
  column_count: number;
};

type AskResponse = {
  sql: string;
  explanation: string;
  columns: string[];
  rows: Record<string, unknown>[];
  row_count: number;
  follow_up_questions: string[];
};

type QueryResult = AskResponse & {
  question: string;
  datasetLabel: string;
  runAt: Date;
};

type OnlineColumn = { name: string; type: string };
type OnlineTable = { name: string; columns: OnlineColumn[] };

const STORAGE_KEY = 'sqlpilot.uploaded-datasets.v1';
const THEME_STORAGE_KEY = 'sqlpilot.theme.v1';
const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
const ONLINE_RETAIL = 'online-retail';

type Theme = 'dark' | 'light';

function readSavedTheme(): Theme {
  try {
    return localStorage.getItem(THEME_STORAGE_KEY) === 'light' ? 'light' : 'dark';
  } catch {
    return 'dark';
  }
}

function readSavedDatasets(): UploadedDataset[] {
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
    if (!Array.isArray(saved)) return [];
    return saved.filter((item): item is UploadedDataset => (
      typeof item?.dataset_id === 'string'
      && typeof item?.dataset_name === 'string'
      && typeof item?.row_count === 'number'
      && typeof item?.column_count === 'number'
      && typeof item?.schema?.name === 'string'
      && Array.isArray(item?.schema?.columns)
    ));
  } catch {
    return [];
  }
}

function getErrorMessage(payload: unknown, fallback: string): string {
  if (payload && typeof payload === 'object' && 'detail' in payload) {
    const detail = (payload as { detail?: unknown }).detail;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
      return detail.map((item) => {
        if (item && typeof item === 'object' && 'msg' in item) {
          return String((item as { msg: unknown }).msg);
        }
        return String(item);
      }).join(' ');
    }
  }
  return fallback;
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function parseOnlineSchema(schema: string): OnlineTable[] {
  const tables: OnlineTable[] = [];
  let current: OnlineTable | undefined;

  for (const line of schema.split('\n')) {
    const tableMatch = line.match(/^([a-zA-Z_][\w]*):$/);
    if (tableMatch) {
      current = { name: tableMatch[1], columns: [] };
      tables.push(current);
      continue;
    }

    const columnMatch = line.match(/^\s+-\s+(.+?)\s+(.+?)\s+(?:NULL|NOT NULL)$/);
    if (current && columnMatch) {
      current.columns.push({ name: columnMatch[1], type: columnMatch[2] });
    }
  }

  return tables;
}

function prettyName(name: string): string {
  return name.replaceAll('_', ' ');
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'number') {
    return new Intl.NumberFormat(undefined, { maximumFractionDigits: 4 }).format(value);
  }
  return String(value);
}

type ChartPoint = { label: string; value: number };
type ChartInfo = { dimension: string; measure: string; points: ChartPoint[] };
type KpiInfo = { label: string; value: string };
type RecentQuestion = { datasetId: string; question: string };

function isFullRowRequest(question: string): boolean {
  const text = question.toLowerCase();
  const action = /\b(show|list|display|inspect|view|see|return|give me|find)\b/;
  const records = /\b(rows?|records?|entries|transactions?|line items?)\b/;
  const entities = /\b(customers?|products?|orders?|invoices?)\b/;
  const analytical = /\b(top|rank|highest|lowest|average|mean|sum|total|revenue|sales|count|compare|group|per|by)\b/;
  return (action.test(text) && records.test(text))
    || (/\b(all|complete|full|matching)\b/.test(text) && (records.test(text) || entities.test(text)))
    || (action.test(text) && entities.test(text) && !analytical.test(text));
}

function chartInfo(result: QueryResult): ChartInfo | null {
  if (isFullRowRequest(result.question) || result.rows.length < 2 || result.rows.length > 12) return null;

  const numericColumns = result.columns.filter((column) => result.rows.some((row) => (
    typeof row[column] === 'number' && Number.isFinite(row[column])
  )));
  if (!numericColumns.length) return null;
  const dimensionColumns = result.columns.filter((column) => (
    !numericColumns.includes(column)
    || (numericColumns.length > 1 && column === numericColumns[0])
  ));
  if (!dimensionColumns.length) return null;

  const measure = numericColumns.length > 1
    ? numericColumns[numericColumns.length - 1]
    : numericColumns[0];
  const dimension = dimensionColumns[0];
  const points = result.rows.map((row) => ({
    label: formatValue(row[dimension]),
    value: Number(row[measure]),
  }));
  if (points.some((point) => !Number.isFinite(point.value) || point.value < 0)) return null;
  if (points.every((point) => point.value === 0)) return null;
  return { dimension, measure, points };
}

function ResultChart({ result }: { result: QueryResult }) {
  const chart = chartInfo(result);
  if (!chart) return null;
  const maximum = Math.max(...chart.points.map((point) => point.value));

  return (
    <section className="card chart-card" aria-label={`${prettyName(chart.measure)} by ${prettyName(chart.dimension)}`}>
      <div className="chart-header">
        <div><p>AT A GLANCE</p><strong>{prettyName(chart.measure)} by {prettyName(chart.dimension)}</strong></div>
        <span>{chart.points.length} values</span>
      </div>
      <div className="chart-bars" role="img" aria-label={`${prettyName(chart.measure)} values across ${prettyName(chart.dimension)}`}>
        {chart.points.map((point, index) => (
          <div className="chart-row" key={`${point.label}-${index}`}>
            <span className="chart-label" title={point.label}>{point.label}</span>
            <span className="chart-track"><i style={{ width: `${Math.max(2, (point.value / maximum) * 100)}%` }} /></span>
            <strong>{formatValue(point.value)}</strong>
          </div>
        ))}
      </div>
    </section>
  );
}

function resultKpis(result: QueryResult): KpiInfo[] {
  if (isFullRowRequest(result.question) || result.rows.length !== 1) return [];
  const row = result.rows[0];
  return result.columns
    .filter((column) => typeof row[column] === 'number' && Number.isFinite(row[column]))
    .slice(0, 4)
    .map((column) => ({ label: prettyName(column), value: formatValue(row[column]) }));
}

function ResultKpis({ result }: { result: QueryResult }) {
  const kpis = resultKpis(result);
  if (!kpis.length) return null;

  return (
    <section className="kpi-grid" aria-label="Key result values">
      {kpis.map((kpi) => (
        <article className="card kpi-card" key={kpi.label}>
          <span>{kpi.label}</span>
          <strong>{kpi.value}</strong>
        </article>
      ))}
    </section>
  );
}

function App() {
  const [theme, setTheme] = React.useState<Theme>(readSavedTheme);
  const [uploadedDatasets, setUploadedDatasets] = React.useState(readSavedDatasets);
  const [selectedDatasetId, setSelectedDatasetId] = React.useState(ONLINE_RETAIL);
  const [onlineSchema, setOnlineSchema] = React.useState('');
  const [schemaError, setSchemaError] = React.useState('');
  const [apiStatus, setApiStatus] = React.useState<'checking' | 'online' | 'offline'>('checking');
  const [question, setQuestion] = React.useState('What were the top 10 products by revenue?');
  const [uploading, setUploading] = React.useState(false);
  const [draggingFile, setDraggingFile] = React.useState(false);
  const [uploadMessage, setUploadMessage] = React.useState('');
  const [uploadError, setUploadError] = React.useState('');
  const [asking, setAsking] = React.useState(false);
  const [queryError, setQueryError] = React.useState('');
  const [result, setResult] = React.useState<QueryResult | null>(null);
  const [copied, setCopied] = React.useState(false);
  const [recentQuestions, setRecentQuestions] = React.useState<RecentQuestion[]>([]);
  const [requestStartedAt, setRequestStartedAt] = React.useState<number | null>(null);
  const [elapsedSeconds, setElapsedSeconds] = React.useState(0);
  const fileInput = React.useRef<HTMLInputElement>(null);

  React.useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    document.querySelector('meta[name="theme-color"]')?.setAttribute(
      'content',
      theme === 'dark' ? '#0b0e0c' : '#f6f7f4',
    );
    try {
      localStorage.setItem(THEME_STORAGE_KEY, theme);
    } catch {
      // The selected theme still applies for this session when storage is unavailable.
    }
  }, [theme]);

  React.useEffect(() => {
    if (requestStartedAt === null) return;
    const updateElapsed = () => setElapsedSeconds(Math.floor((Date.now() - requestStartedAt) / 1000));
    updateElapsed();
    const timer = window.setInterval(updateElapsed, 1000);
    return () => window.clearInterval(timer);
  }, [requestStartedAt]);

  React.useEffect(() => {
    let active = true;

    fetch('/api/health')
      .then((response) => {
        if (!response.ok) throw new Error('API unavailable');
        if (active) setApiStatus('online');
      })
      .catch(() => {
        if (active) setApiStatus('offline');
      });

    fetch('/api/schema?refresh=true')
      .then(async (response) => {
        const payload = await readJson(response);
        if (!response.ok) throw new Error(getErrorMessage(payload, 'Could not load Online Retail schema.'));
        const schema = payload && typeof payload === 'object' && 'schema' in payload
          ? (payload as { schema: unknown }).schema
          : '';
        if (active && typeof schema === 'string') {
          setOnlineSchema(schema);
          setSchemaError('');
        }
      })
      .catch(() => {
        if (active) setSchemaError('Could not load the built-in schema. Check the API connection and refresh.');
      });

    return () => { active = false; };
  }, []);

  React.useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(uploadedDatasets));
    } catch {
      // The current session remains usable if browser storage is unavailable.
    }
  }, [uploadedDatasets]);

  const selectedUpload = uploadedDatasets.find((dataset) => dataset.dataset_id === selectedDatasetId);
  const isOnlineRetail = selectedDatasetId === ONLINE_RETAIL;
  const onlineTables = React.useMemo(() => parseOnlineSchema(onlineSchema), [onlineSchema]);
  const onlineColumnCount = onlineTables.reduce((total, table) => total + table.columns.length, 0);

  function chooseDataset(datasetId: string) {
    setSelectedDatasetId(datasetId);
    setQuestion(datasetId === ONLINE_RETAIL ? 'What were the top 10 products by revenue?' : '');
    setQueryError('');
    setResult(null);
  }

  async function uploadCsv(file?: File) {
    if (!file) return;
    setUploadError('');
    setUploadMessage('');

    if (!file.name.toLowerCase().endsWith('.csv')) {
      setUploadError('Choose a .csv file to upload.');
      return;
    }
    if (file.size === 0) {
      setUploadError('This CSV is empty. Choose a file with a header and at least one row.');
      return;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      setUploadError('This file is larger than the 10 MiB upload limit.');
      return;
    }

    setUploading(true);
    setUploadMessage(`Uploading ${file.name}…`);
    try {
      const body = new FormData();
      body.append('file', file);
      const response = await fetch('/api/datasets/upload', { method: 'POST', body });
      const payload = await readJson(response);
      if (!response.ok) throw new Error(getErrorMessage(payload, 'Upload failed. Check the CSV and try again.'));

      const dataset = payload as UploadedDataset;
      if (!dataset?.dataset_id || !dataset?.schema?.columns) {
        throw new Error('The upload completed, but SQLPilot returned incomplete dataset details.');
      }
      setUploadedDatasets((current) => [
        dataset,
        ...current.filter((item) => item.dataset_id !== dataset.dataset_id),
      ]);
      setSelectedDatasetId(dataset.dataset_id);
      setQuestion('');
      setResult(null);
      setQueryError('');
      setUploadMessage(`${dataset.dataset_name} is ready to query.`);
    } catch (error) {
      setUploadMessage('');
      setUploadError(error instanceof Error
        ? error.message
        : 'Could not reach SQLPilot. Check that the API is running.');
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = '';
    }
  }

  function exampleQuestions(): string[] {
    if (isOnlineRetail || !selectedUpload) {
      return [
        'What were the top 10 products by revenue?',
        'Which countries had the highest sales?',
        'How did revenue change month by month?',
      ];
    }

    const columns = selectedUpload.schema.columns;
    const numeric = columns.find((column) => /^(BIGINT|NUMERIC|INTEGER|DECIMAL|REAL|DOUBLE)/i.test(column.postgres_type));
    const date = columns.find((column) => /^(DATE|TIMESTAMP)/i.test(column.postgres_type));
    const group = columns.find((column) => /^(TEXT|BOOLEAN)/i.test(column.postgres_type))
      ?? columns.find((column) => column !== numeric && column !== date);
    const examples: string[] = [];

    if (numeric) examples.push(`What is the average ${numeric.name}?`);
    if (group) examples.push(`How many rows are there for each value of ${group.name}?`);
    if (date) examples.push(`How many rows are there for each year in ${date.name}?`);
    if (examples.length < 3 && columns[0]) {
      examples.push(`Show the first 10 rows, including ${columns[0].name}.`);
    }

    return examples.slice(0, 3);
  }

  async function runQuestion(trimmedQuestion: string) {
    if (trimmedQuestion.length < 3) return;

    setQuestion(trimmedQuestion);
    setAsking(true);
    setRequestStartedAt(Date.now());
    setElapsedSeconds(0);
    setQueryError('');
    setCopied(false);
    setResult(null);
    try {
      const endpoint = isOnlineRetail
        ? '/api/ask'
        : `/api/datasets/${encodeURIComponent(selectedDatasetId)}/ask`;
      const response = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: trimmedQuestion }),
      });
      const payload = await readJson(response);
      if (!response.ok) throw new Error(getErrorMessage(payload, 'SQLPilot could not answer that question.'));

      const data = payload as AskResponse;
      setResult({
        ...data,
        follow_up_questions: Array.isArray(data.follow_up_questions) ? data.follow_up_questions : [],
        question: trimmedQuestion,
        datasetLabel: isOnlineRetail ? 'Online Retail' : selectedUpload?.dataset_name ?? 'Uploaded dataset',
        runAt: new Date(),
      });
      setRecentQuestions((current) => [
        { datasetId: selectedDatasetId, question: trimmedQuestion },
        ...current.filter((item) => item.datasetId !== selectedDatasetId || item.question !== trimmedQuestion),
      ].slice(0, 5));
    } catch (error) {
      setQueryError(error instanceof Error
        ? error.message
        : 'Could not reach SQLPilot. Check that the API is running.');
    } finally {
      setAsking(false);
      setRequestStartedAt(null);
    }
  }

  async function ask(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    await runQuestion(question.trim());
  }

  async function copySql() {
    if (!result?.sql) return;
    try {
      await navigator.clipboard.writeText(result.sql);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      setQueryError('Clipboard access is unavailable in this browser. You can still select and copy the SQL.');
    }
  }

  const schemaColumnCount = isOnlineRetail ? onlineColumnCount : selectedUpload?.column_count ?? 0;
  const schemaTableCount = isOnlineRetail ? onlineTables.length : (selectedUpload ? 1 : 0);

  return (
    <div className="app-shell" data-theme={theme}>
      <header className="topbar">
        <a className="brand" href="#top" aria-label="SQLPilot home">
          <span className="brand-mark" aria-hidden="true"><span /></span>
          <span className="brand-name">SQLPilot</span>
          <span className="brand-context">DATA WORKSPACE</span>
        </a>
        <div className="topbar-actions">
          <div className={`connection connection-${apiStatus}`}>
            <span className="connection-dot" />
            <span>{apiStatus === 'checking' ? 'Connecting to API' : apiStatus === 'online' ? 'API connected' : 'API unavailable'}</span>
          </div>
          <button
            type="button"
            className="theme-toggle"
            aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
            aria-pressed={theme === 'dark'}
            title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
            onClick={() => setTheme((current) => current === 'dark' ? 'light' : 'dark')}
          >
            <span aria-hidden="true">{theme === 'dark' ? '☼' : '◐'}</span>
            <span>{theme === 'dark' ? 'Dark' : 'Light'}</span>
          </button>
        </div>
      </header>

      <main id="top" className="workspace">
        <section className="page-heading">
          <div>
            <p className="eyebrow">NATURAL LANGUAGE · POSTGRESQL</p>
            <h1>Ask your data.</h1>
            <p className="page-intro">Ask a question, get a clear answer, and explore the rows behind it.</p>
          </div>
          <div className="heading-note"><span className="sparkle">✳</span><span>Local analysis<br /><strong>Your data stays in this workspace</strong></span></div>
        </section>

        <div className="workspace-grid">
          <aside className="sidebar">
            <section className="card dataset-card">
              <div className="section-heading">
                <div><p className="section-kicker">01 / SOURCE</p><h2>Choose a dataset</h2></div>
                <span className="section-icon" aria-hidden="true">◫</span>
              </div>

              <label className="field-label" htmlFor="dataset-select">ACTIVE DATASET</label>
              <select id="dataset-select" value={selectedDatasetId} disabled={asking || uploading} onChange={(event) => chooseDataset(event.target.value)}>
                <option value={ONLINE_RETAIL}>Online Retail · built in</option>
                {uploadedDatasets.map((dataset) => (
                  <option key={dataset.dataset_id} value={dataset.dataset_id}>{dataset.dataset_name} · uploaded</option>
                ))}
              </select>

              <div
                className={'upload-box' + (draggingFile ? ' is-dragging' : '')}
                onDragOver={(event) => {
                  event.preventDefault();
                  if (!uploading && !asking) setDraggingFile(true);
                }}
                onDragLeave={(event) => {
                  if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDraggingFile(false);
                }}
                onDrop={(event) => {
                  event.preventDefault();
                  setDraggingFile(false);
                  if (!uploading && !asking) void uploadCsv(event.dataTransfer.files[0]);
                }}
              >
                <div className="upload-symbol" aria-hidden="true">↑</div>
                <div className="upload-copy"><strong>Upload a CSV</strong><span>Drop a file here or browse · up to 10 MiB</span></div>
                <input
                  ref={fileInput}
                  id="csv-upload"
                  className="visually-hidden"
                  type="file"
                  accept=".csv,text/csv,application/csv"
                  disabled={uploading || asking}
                  aria-hidden="true"
                  tabIndex={-1}
                  onChange={(event) => void uploadCsv(event.target.files?.[0])}
                />
                <button
                  type="button"
                  className="upload-button"
                  disabled={uploading || asking}
                  onClick={() => fileInput.current?.click()}
                >
                  {uploading ? 'Uploading…' : 'Choose file'}
                </button>
              </div>
              {uploadMessage && <p className="inline-message success-message" role="status">{uploadMessage}</p>}
              {uploadError && <p className="inline-message error-message" role="alert">{uploadError}</p>}

              <div className="dataset-details">
                <div className="detail-title"><div><span className="detail-led" />{isOnlineRetail ? 'ONLINE RETAIL' : 'UPLOADED DATASET'}</div><span className="schema-state">{isOnlineRetail ? (onlineSchema ? 'SCHEMA READY' : 'LOADING') : 'READY'}</span></div>
                <h3>{isOnlineRetail ? 'Online Retail' : selectedUpload?.dataset_name ?? 'Select a dataset'}</h3>
                <div className="stat-grid">
                  <div className="stat"><strong>{isOnlineRetail ? schemaTableCount || '—' : selectedUpload?.row_count.toLocaleString() ?? '—'}</strong><span>{isOnlineRetail ? 'tables' : 'rows'}</span></div>
                  <div className="stat"><strong>{schemaColumnCount || '—'}</strong><span>{isOnlineRetail ? 'columns' : 'columns'}</span></div>
                </div>
                {!isOnlineRetail && selectedUpload && (
                  <p className="schema-location"><code>{selectedUpload.schema.name}.{selectedUpload.schema.table_name}</code></p>
                )}
              </div>
            </section>

            <section className="card schema-card">
              <div className="section-heading schema-heading">
                <div><p className="section-kicker">SCHEMA EXPLORER</p><h2>What’s available</h2></div>
                <span className="schema-count">{schemaColumnCount || 0} FIELDS</span>
              </div>

              {isOnlineRetail ? (
                onlineSchema ? (
                  <div className="online-schema-list">
                    {onlineTables.map((table) => (
                      <details className="online-table" key={table.name} open={onlineTables.length <= 4}>
                        <summary><span className="table-glyph">▤</span><strong>{table.name}</strong><span>{table.columns.length} fields</span></summary>
                        <div className="column-list">
                          {table.columns.map((column) => <div className="column-row" key={`${table.name}.${column.name}`}><code>{column.name}</code><span>{column.type}</span></div>)}
                        </div>
                      </details>
                    ))}
                  </div>
                ) : schemaError ? (
                  <div className="schema-empty schema-error" role="status">{schemaError}</div>
                ) : <div className="schema-empty"><span className="mini-loader" />Loading the built-in database schema…</div>
              ) : selectedUpload ? (
                <div className="column-list uploaded-columns">
                  {selectedUpload.schema.columns.map((column) => (
                    <div className="column-row" key={column.name} title={column.original_name !== column.name ? `Original CSV header: ${column.original_name}` : undefined}>
                      <code>{column.name}</code><span>{column.postgres_type}</span>
                    </div>
                  ))}
                </div>
              ) : <div className="schema-empty">Upload a CSV to preview its detected columns and types.</div>}
              {!isOnlineRetail && selectedUpload && <p className="schema-footnote">Original header names appear when they differ from the SQL column name.</p>}
            </section>
          </aside>

          <section className="main-column">
            <section className="card question-card">
              <div className="section-heading">
                <div><p className="section-kicker">02 / QUESTION</p><h2>What do you want to find out?</h2></div>
                <span className="shortcut-hint">NATURAL LANGUAGE</span>
              </div>
              <form onSubmit={ask}>
                <label className="visually-hidden" htmlFor="question">Ask a question about {isOnlineRetail ? 'Online Retail' : selectedUpload?.dataset_name ?? 'the selected dataset'}</label>
                <textarea
                  id="question"
                  value={question}
                  onChange={(event) => setQuestion(event.target.value)}
                  rows={4}
                  maxLength={1000}
                  placeholder={isOnlineRetail ? 'e.g. Which countries had the highest sales last quarter?' : 'e.g. Compare the average values across groups…'}
                />
                <div className="question-footer">
                  <span className="question-hint"><span className="hint-dot" />SQLPilot will generate and safely run a read-only query</span>
                  <div className="question-actions"><span className="character-count">{question.length}/1000</span><button className="ask-button" type="submit" disabled={asking || question.trim().length < 3 || (!isOnlineRetail && !selectedUpload)}>{asking ? <><span className="button-spinner" />Working…</> : <>Run query <span aria-hidden="true">↗</span></>}</button></div>
                </div>
              </form>

              <div className="examples">
                <div className="examples-label"><span>QUICK START</span><span className="examples-rule" /></div>
                <div className="example-list">
                  {exampleQuestions().map((example) => <button type="button" className="example-chip" key={example} onClick={() => setQuestion(example)}>{example}<span aria-hidden="true">↗</span></button>)}
                </div>
                {recentQuestions.some((item) => item.datasetId === selectedDatasetId) && (
                  <div className="recent-questions">
                    <div className="examples-label"><span>RECENT IN THIS SESSION</span><span className="examples-rule" /></div>
                    <div className="example-list">
                      {recentQuestions.filter((item) => item.datasetId === selectedDatasetId).map((item) => (
                        <button
                          type="button"
                          className="example-chip recent-question"
                          key={item.question}
                          disabled={asking}
                          onClick={() => void runQuestion(item.question)}
                        >
                          {item.question}<span aria-hidden="true">↻</span>
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </section>

            {queryError && <section className="query-error" role="alert"><span className="error-mark">!</span><div><strong>Query didn’t complete</strong><p>{queryError}</p><span>Check the dataset and question, then try again.</span></div></section>}

            {asking && (
              <section className="card progress-card" aria-live="polite">
                <span className="progress-spinner" />
                <div>
                  <strong>{elapsedSeconds >= 5 ? 'The local model is taking a moment' : 'Sending your question to the local model'}</strong>
                  <p>SQLPilot will validate the query before fetching any rows.</p>
                </div>
                <span className="progress-stage">{elapsedSeconds}s</span>
              </section>
            )}

            {result ? (
              <section className="result-section">
                <div className="results-title-row"><div><p className="section-kicker">03 / ANSWER</p><h2>Your data, explained</h2></div><span className="run-badge"><span /> READY</span></div>
                <div className="card answer-card">
                  <div className="answer-header"><span className="answer-icon" aria-hidden="true">✳</span><div><p>ANSWER</p><strong>{result.datasetLabel}</strong></div><time>{result.runAt.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</time></div>
                  <div className="answer-body"><p className="asked-question"><span>YOU ASKED</span>{result.question}</p><p className="explanation">{result.explanation}</p></div>
                </div>

                <ResultKpis result={result} />
                <ResultChart result={result} />

                <div className="card data-card">
                  <div className="data-header"><div><p>RESULT TABLE</p><span>{result.row_count.toLocaleString()} {result.row_count === 1 ? 'row' : 'rows'} returned</span></div><span className="table-indicator"><span /> QUERY OUTPUT</span></div>
                  {result.rows.length > 0 && result.columns.length > 0 ? (
                    <div className="result-table-scroll"><table className="result-table"><thead><tr>{result.columns.map((column) => <th key={column}>{prettyName(column)}</th>)}</tr></thead><tbody>{result.rows.map((row, index) => <tr key={index}>{result.columns.map((column) => <td key={column}>{formatValue(row[column])}</td>)}</tr>)}</tbody></table></div>
                  ) : <div className="no-rows"><span>∅</span><strong>No rows to display</strong><p>{result.sql ? 'The query ran successfully and returned no matching records.' : 'This question did not produce a SQL query.'}</p></div>}
                  {result.rows.length > 0 && <div className="table-foot">Showing all {result.rows.length.toLocaleString()} returned {result.rows.length === 1 ? 'row' : 'rows'}.</div>}
                </div>

                {result.follow_up_questions.length > 0 && (
                  <section className="card follow-up-card" aria-label="Suggested follow-up questions">
                    <div className="follow-up-heading"><span aria-hidden="true">↳</span><div><strong>Keep exploring</strong><p>Ask a follow-up about this dataset</p></div></div>
                    <div className="follow-up-list">
                      {result.follow_up_questions.map((suggestion) => (
                        <button
                          type="button"
                          className="follow-up-chip"
                          key={suggestion}
                          disabled={asking}
                          onClick={() => void runQuestion(suggestion)}
                        >
                          {suggestion}<span aria-hidden="true">↗</span>
                        </button>
                      ))}
                    </div>
                  </section>
                )}

                {result.sql ? (
                  <details className="card sql-disclosure">
                    <summary className="sql-disclosure-summary">
                      <span className="sql-icon">&lt;/&gt;</span>
                      <span className="sql-disclosure-copy"><strong>Generated SQL</strong><small>Read-only PostgreSQL query</small></span>
                      <span className="sql-disclosure-action">Show query <span aria-hidden="true">⌄</span></span>
                    </summary>
                    <div className="sql-disclosure-body">
                      <div className="sql-copy-row"><span>Used to produce this answer</span><button type="button" className="copy-button" onClick={() => void copySql()}>{copied ? 'Copied ✓' : 'Copy SQL'}</button></div>
                      <pre className="sql-code"><code>{result.sql}</code></pre>
                    </div>
                  </details>
                ) : (
                  <div className="no-sql">No query was needed for this response.</div>
                )}
              </section>
            ) : !asking && !queryError ? (
              <section className="card empty-state"><div className="empty-illustration" aria-hidden="true"><span className="empty-lines"><i /><i /><i /></span><span className="empty-search">⌕</span></div><p className="section-kicker">READY WHEN YOU ARE</p><h2>Your query workspace</h2><p>Ask a question to see the SQL, explanation, and result rows here.</p><div className="empty-steps"><span><b>01</b> Choose data</span><i /><span><b>02</b> Ask naturally</span><i /><span><b>03</b> Inspect SQL</span></div></section>
            ) : null}
          </section>
        </div>

        <footer className="app-footer"><span><span className="footer-mark">S</span> SQLPilot <span className="footer-separator">·</span> Local data workspace</span><span>Review generated SQL before relying on an answer.</span></footer>
      </main>
    </div>
  );
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);
