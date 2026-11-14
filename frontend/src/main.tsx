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
};

type QueryResult = AskResponse & {
  question: string;
  datasetLabel: string;
  runAt: Date;
};

type OnlineColumn = { name: string; type: string };
type OnlineTable = { name: string; columns: OnlineColumn[] };

const STORAGE_KEY = 'sqlpilot.uploaded-datasets.v1';
const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
const ONLINE_RETAIL = 'online-retail';

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

function App() {
  const [uploadedDatasets, setUploadedDatasets] = React.useState(readSavedDatasets);
  const [selectedDatasetId, setSelectedDatasetId] = React.useState(ONLINE_RETAIL);
  const [onlineSchema, setOnlineSchema] = React.useState('');
  const [apiStatus, setApiStatus] = React.useState<'checking' | 'online' | 'offline'>('checking');
  const [question, setQuestion] = React.useState('What were the top 10 products by revenue?');
  const [uploading, setUploading] = React.useState(false);
  const [uploadMessage, setUploadMessage] = React.useState('');
  const [uploadError, setUploadError] = React.useState('');
  const [asking, setAsking] = React.useState(false);
  const [queryError, setQueryError] = React.useState('');
  const [result, setResult] = React.useState<QueryResult | null>(null);
  const [copied, setCopied] = React.useState(false);
  const fileInput = React.useRef<HTMLInputElement>(null);

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

    fetch('/api/schema')
      .then(async (response) => {
        const payload = await readJson(response);
        if (!response.ok) throw new Error(getErrorMessage(payload, 'Could not load Online Retail schema.'));
        const schema = payload && typeof payload === 'object' && 'schema' in payload
          ? (payload as { schema: unknown }).schema
          : '';
        if (active && typeof schema === 'string') setOnlineSchema(schema);
      })
      .catch(() => {
        // The health indicator and query error give the user a visible connection state.
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

  async function ask(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const trimmedQuestion = question.trim();
    if (trimmedQuestion.length < 3) return;

    setAsking(true);
    setQueryError('');
    setCopied(false);
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
        question: trimmedQuestion,
        datasetLabel: isOnlineRetail ? 'Online Retail' : selectedUpload?.dataset_name ?? 'Uploaded dataset',
        runAt: new Date(),
      });
    } catch (error) {
      setQueryError(error instanceof Error
        ? error.message
        : 'Could not reach SQLPilot. Check that the API is running.');
    } finally {
      setAsking(false);
    }
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
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="#top" aria-label="SQLPilot home">
          <span className="brand-mark" aria-hidden="true"><span /></span>
          <span className="brand-name">SQLPilot</span>
          <span className="brand-context">DATA WORKSPACE</span>
        </a>
        <div className={`connection connection-${apiStatus}`}>
          <span className="connection-dot" />
          <span>{apiStatus === 'checking' ? 'Connecting to API' : apiStatus === 'online' ? 'API connected' : 'API unavailable'}</span>
        </div>
      </header>

      <main id="top" className="workspace">
        <section className="page-heading">
          <div>
            <p className="eyebrow">NATURAL LANGUAGE · POSTGRESQL</p>
            <h1>Ask your data.</h1>
            <p className="page-intro">Explore a dataset in plain English. Review the SQLPilot generated and the rows it returned.</p>
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
              <select id="dataset-select" value={selectedDatasetId} onChange={(event) => chooseDataset(event.target.value)}>
                <option value={ONLINE_RETAIL}>Online Retail · built in</option>
                {uploadedDatasets.map((dataset) => (
                  <option key={dataset.dataset_id} value={dataset.dataset_id}>{dataset.dataset_name} · uploaded</option>
                ))}
              </select>

              <div className="upload-box">
                <div className="upload-symbol" aria-hidden="true">↑</div>
                <div className="upload-copy"><strong>Bring your own CSV</strong><span>UTF-8 · up to 10 MiB</span></div>
                <input
                  ref={fileInput}
                  id="csv-upload"
                  className="visually-hidden"
                  type="file"
                  accept=".csv,text/csv,application/csv"
                  disabled={uploading}
                  onChange={(event) => void uploadCsv(event.target.files?.[0])}
                />
                <label htmlFor="csv-upload" className={`upload-button${uploading ? ' disabled' : ''}`}>{uploading ? 'Uploading…' : 'Browse'}</label>
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
              </div>
            </section>

            {queryError && <section className="query-error" role="alert"><span className="error-mark">!</span><div><strong>Query didn’t complete</strong><p>{queryError}</p><span>Check the dataset and question, then try again.</span></div></section>}

            {asking && <section className="card progress-card" aria-live="polite"><span className="progress-spinner" /><div><strong>Working through your question</strong><p>Generating SQL, checking it, then fetching the result rows.</p></div><span className="progress-stage">RUNNING</span></section>}

            {result ? (
              <section className="result-section">
                <div className="results-title-row"><div><p className="section-kicker">03 / RESULT</p><h2>Query result</h2></div><span className="run-badge"><span /> COMPLETED</span></div>
                <div className="card answer-card">
                  <div className="answer-header"><span className="answer-icon" aria-hidden="true">✳</span><div><p>SQLPILOT’S EXPLANATION</p><strong>{result.datasetLabel}</strong></div><time>{result.runAt.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</time></div>
                  <div className="answer-body"><p className="asked-question"><span>YOU ASKED</span>{result.question}</p><p className="explanation">{result.explanation}</p></div>
                </div>

                <div className="card sql-card">
                  <div className="sql-header"><div><span className="sql-icon">&lt;/&gt;</span><div><p>GENERATED SQL</p><span>Read-only PostgreSQL query</span></div></div><button type="button" className="copy-button" onClick={() => void copySql()} disabled={!result.sql}>{copied ? 'Copied ✓' : 'Copy SQL'}</button></div>
                  {result.sql ? <pre className="sql-code"><code>{result.sql}</code></pre> : <p className="no-sql">No SQL was generated. See the explanation above for more detail.</p>}
                </div>

                <div className="card data-card">
                  <div className="data-header"><div><p>RESULT ROWS</p><span>{result.row_count.toLocaleString()} {result.row_count === 1 ? 'row' : 'rows'} returned</span></div><span className="table-indicator"><span /> QUERY OUTPUT</span></div>
                  {result.rows.length > 0 && result.columns.length > 0 ? (
                    <div className="result-table-scroll"><table className="result-table"><thead><tr>{result.columns.map((column) => <th key={column}>{prettyName(column)}</th>)}</tr></thead><tbody>{result.rows.map((row, index) => <tr key={index}>{result.columns.map((column) => <td key={column}>{formatValue(row[column])}</td>)}</tr>)}</tbody></table></div>
                  ) : <div className="no-rows"><span>∅</span><strong>No rows to display</strong><p>{result.sql ? 'The query ran successfully and returned no matching records.' : 'This question did not produce a SQL query.'}</p></div>}
                  {result.rows.length > 0 && <div className="table-foot">Showing {result.rows.length.toLocaleString()} of {result.row_count.toLocaleString()} returned {result.row_count === 1 ? 'row' : 'rows'}</div>}
                </div>
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
