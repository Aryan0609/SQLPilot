import React from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';

type Result = { sql:string; explanation:string; columns:string[]; rows:Record<string,unknown>[]; row_count:number };
function App(){
  const [question,setQuestion]=React.useState('What were the top 10 products by revenue?');
  const [result,setResult]=React.useState<Result|null>(null); const [error,setError]=React.useState(''); const [loading,setLoading]=React.useState(false);
  async function ask(e:React.FormEvent){e.preventDefault();setLoading(true);setError('');setResult(null);try{const r=await fetch('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question})});const data=await r.json();if(!r.ok)throw new Error(data.detail||'Request failed');setResult(data);}catch(e){setError(e instanceof Error?e.message:'Could not reach SQLPilot.');}finally{setLoading(false)}}
  return <main className="shell"><header><div className="brand"><span className="mark">S</span><span>SQLPilot</span><span className="badge">RETAIL ANALYST</span></div><span className="status"><i/> Local workspace</span></header><section className="hero"><p className="eyebrow">ASK YOUR DATA</p><h1>Good questions.<br/><em>Clear answers.</em></h1><p className="sub">Explore real retail transactions with plain English. SQLPilot writes and safely runs the query for you.</p>
  <form onSubmit={ask}><label htmlFor="question">What would you like to know?</label><textarea id="question" value={question} onChange={e=>setQuestion(e.target.value)} rows={3} placeholder="e.g. Which countries had the highest sales last quarter?"/><div className="formfoot"><span>Powered by local Ollama</span><button disabled={loading||question.trim().length<3}>{loading?'Thinking…':'Ask SQLPilot'} <b>↗</b></button></div></form>
  <div className="suggestions"><span>TRY ASKING</span>{['Monthly revenue trend','Top customers by spend','Best selling products'].map(q=><button key={q} onClick={()=>setQuestion(q)}>{q} <b>↗</b></button>)}</div></section>
  {error&&<section className="panel error">{error}<p>Check that Docker services are running and the Ollama model has been downloaded.</p></section>}
  {result&&<section className="results"><div className="answer panel"><div className="paneltop"><span>THE ANSWER</span><span>{result.row_count} rows</span></div><p>{result.explanation}</p><details><summary>View SQL</summary><pre>{result.sql}</pre></details></div><div className="tablewrap panel"><div className="paneltop"><span>QUERY RESULTS</span><span>Showing {result.rows.length} rows</span></div>{result.rows.length?<div className="scroll"><table><thead><tr>{result.columns.map(c=><th key={c}>{c.replaceAll('_',' ')}</th>)}</tr></thead><tbody>{result.rows.map((row,i)=><tr key={i}>{result.columns.map(c=><td key={c}>{String(row[c]??'—')}</td>)}</tr>)}</tbody></table></div>:<p>No matching rows.</p>}</div></section>}
  <footer><span>Built for curious analysts.</span><span>UCI Online Retail · CC BY 4.0</span></footer></main>
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
