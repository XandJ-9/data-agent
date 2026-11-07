import { useCallback, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ApiClient, errorText, pathId } from './api';
import { activeRun, dateLabel, statusLabel, type Message, type Messages, type Run, type Step } from './types';
import { usePolling } from './usePolling';

export function Chat({ api, sessionId, title, archived, onRename }: { api: ApiClient; sessionId: string; title: string; archived: boolean; onRename: (title: string) => void }) {
  const endpoint = `/sessions/${pathId(sessionId)}/messages`;
  const load = useCallback((signal: AbortSignal) => api.request<Messages>(endpoint, { signal }), [api, endpoint]);
  const query = usePolling(load);
  const [older, setOlder] = useState<Message[]>([]);
  const [cursor, setCursor] = useState<number | null | undefined>(undefined);
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [paging, setPaging] = useState(false);
  const [error, setError] = useState('');
  const [uncertain, setUncertain] = useState(false);
  const [editingTitle, setEditingTitle] = useState(false);
  const [draftTitle, setDraftTitle] = useState(title);
  const pending = useRef<{ text: string; id: string } | null>(null);
  const alive = useRef(true);
  const bottom = useRef<HTMLDivElement>(null);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const messages = [...new Map([...older, ...(query.data?.items ?? [])].map(message => [message.id, message])).values()].sort((a,b) => a.id - b.id);
  const next = cursor === undefined ? query.data?.next_before_id : cursor;
  const runs = query.data?.runs ?? [];
  const active = runs.some(run => activeRun(run.status));
  const visibleRuns = runs.filter(run => run.status !== 'succeeded' || run.deliveries.length > 0);
  useEffect(() => { bottom.current?.scrollIntoView?.({ behavior: 'smooth', block: 'end' }); }, [query.data?.items.at(-1)?.id]);
  async function send() {
    if (!text.trim() || busy || active) return;
    const draft = pending.current?.text === text.trim() ? pending.current : { text: text.trim(), id: crypto.randomUUID() };
    pending.current = draft; setBusy(true); setError('');
    try {
      await api.request(endpoint, { method: 'POST', body: { text: draft.text, client_message_id: draft.id } });
      if (alive.current) { setText(''); pending.current = null; setUncertain(false); query.refresh(); }
    } catch (cause) { if (alive.current) { setError(`${errorText(cause)} 若消息已被接收，重试相同内容不会重复提交。`); setUncertain(true); } }
    finally { if (alive.current) setBusy(false); }
  }
  async function loadOlder() {
    if (next == null || paging) return;
    setPaging(true); setError('');
    try {
      const page = await api.request<Messages>(`${endpoint}?before_id=${next}`);
      if (alive.current) { setOlder(previous => [...page.items, ...previous]); setCursor(page.next_before_id ?? null); }
    } catch (cause) { if (alive.current) setError(errorText(cause)); }
    finally { if (alive.current) setPaging(false); }
  }
  return <section className="chat"><div className="chat-heading"><div><span className="eyebrow">对话空间</span>{editingTitle ? <form className="inline-edit" onSubmit={event => { event.preventDefault(); if (draftTitle.trim()) { onRename(draftTitle.trim()); } setEditingTitle(false); }}><input value={draftTitle} onChange={event => setDraftTitle(event.target.value)} maxLength={200} autoFocus onBlur={() => { if (draftTitle.trim()) onRename(draftTitle.trim()); setEditingTitle(false); }} onKeyDown={event => { if (event.key === 'Escape') { setDraftTitle(title); setEditingTitle(false); } }} /></form> : <h1 className="editable-title" onClick={() => { setDraftTitle(title); setEditingTitle(true); }} title="点击编辑标题">{title}</h1>}</div><span className={`status ${active ? 'running' : ''}`}>{archived ? '已归档 · 只读' : active ? '任务进行中' : '随时开始'}</span></div>{archived && <div className="archived-banner">此会话已归档，内容只读。你可以恢复归档以继续对话。</div>}<div className="conversation"><div className="conversation-inner">{next != null && <button className="load-more" disabled={paging} onClick={() => void loadOlder()}>{paging ? '正在加载…' : '加载更早的消息'}</button>}{query.loading && !query.data && <p className="muted">正在读取会话…</p>}{query.error && <div className="error" role="alert">{query.error}<button onClick={query.refresh}>重新连接</button></div>}{!query.loading && messages.length === 0 && <div className="empty-chat"><span className="hero-mark small">D</span><h2>把问题交给我</h2><p>说明你的目标、背景和期望的输出，我们一起推进。</p></div>}{messages.map(message => <article key={message.id} className={`message ${message.role === 'user' ? 'user' : 'assistant'}`}><div className="message-meta"><span className="avatar">{message.role === 'user' ? '我' : 'D'}</span><strong>{message.role === 'user' ? '你' : message.role === 'assistant' ? 'Data Agent' : '系统'}</strong><time>{dateLabel(message.created_at)}</time></div><div className="message-body markdown">{message.role === 'assistant' && message.run_id && <RunSteps runId={message.run_id} active={activeRun(runs.find(run => run.id === message.run_id)?.status ?? '')} api={api} />}<ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: props => <a {...props} target="_blank" rel="noopener noreferrer" />, img: ({ alt }) => <span className="muted">[图片：{alt || '外部图片'}]</span> }}>{message.content}</ReactMarkdown></div></article>)}{visibleRuns.length > 0 && <div className="runs" aria-label="执行记录">{visibleRuns.map(run => <RunCard key={run.id} run={run} api={api} refresh={query.refresh} />)}</div>}<div ref={bottom} /></div></div>{!archived && <div className="composer-area">{error && <p className="error" role="alert">{error}</p>}<form className="composer" onSubmit={event => { event.preventDefault(); void send(); }}><label className="sr-only" htmlFor="message">输入消息</label><textarea id="message" value={text} readOnly={busy} onChange={event => { setText(event.target.value); setUncertain(false); }} placeholder="描述你的问题，或继续刚才的思考…" rows={3} maxLength={50000} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(); } }} /><div className="composer-footer"><span>{active ? '当前任务结束后可继续发送' : 'Enter 发送 · Shift + Enter 换行'}</span><button className="primary" disabled={busy || active || !text.trim() || !query.data}>{busy ? '提交中…' : uncertain ? '重试发送 ↑' : '发送 ↑'}</button></div></form><p className="composer-note">AI 回答可能存在偏差，请核实重要信息。</p></div>}</section>;
}
function RunCard({ run, api, refresh }: { run: Run; api: ApiClient; refresh: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [confirm, setConfirm] = useState<string | null>(null);
  async function action(path: string, body?: unknown) {
    setBusy(true); setError('');
    try { await api.request(path, { method: 'POST', body }); setConfirm(null); refresh(); }
    catch (cause) { setError(errorText(cause)); } finally { setBusy(false); }
  }
  return <article className="run-card">{run.status !== 'succeeded' && <div className="run-line"><span className={`status ${run.status}`}>{statusLabel(run.status)}</span><time>{dateLabel(run.created_at)}</time>{activeRun(run.status) && <button disabled={busy} onClick={() => void action(`/runs/${pathId(run.id)}/cancel`)}>取消执行</button>}</div>}{run.status !== 'succeeded' && <RunSteps runId={run.id} active={activeRun(run.status)} api={api} />}{run.status === 'failed' && <p className="error">执行失败。{run.error_code && /model|config|provider|api_key/i.test(run.error_code) ? '模型配置不可用，请联系管理员检查模型配置。' : '请检查问题内容，或联系管理员。'}{run.error_code && <small>错误代码：{run.error_code}</small>}</p>}{run.status === 'interrupted' && <p className="muted">执行已中断，可重新发送问题发起新任务。</p>}{run.deliveries.map(delivery => <div key={delivery.id} className="delivery"><span>飞书投递 · {statusLabel(delivery.status)}</span>{['failed','unknown'].includes(delivery.status) && <button disabled={busy} onClick={() => delivery.status === 'unknown' ? setConfirm(delivery.id) : void action(`/deliveries/${pathId(delivery.id)}/retry`, { confirm_duplicate_risk: false })}>重试投递</button>}{confirm === delivery.id && <div className="confirmation" role="alert"><p>上次投递结果未知，消息可能已送达。再次投递可能产生重复消息。</p><button className="danger" disabled={busy} onClick={() => void action(`/deliveries/${pathId(delivery.id)}/retry`, { confirm_duplicate_risk: true })}>确认可能重复，仍然重试</button><button disabled={busy} onClick={() => setConfirm(null)}>暂不重试</button></div>}</div>)}{error && <p className="error" role="alert">{error}</p>}</article>;
}

function preview(text: string, limit = 500) {
  if (text.length <= limit) return text;
  return `${text.slice(0, limit)}…`;
}

function pretty(text: string) {
  try { return JSON.stringify(JSON.parse(text), null, 2); } catch { return text; }
}

function RunSteps({ runId, active, api }: { runId: string; active: boolean; api: ApiClient }) {
  const load = useCallback((signal: AbortSignal) =>
    api.request<{ items: Step[] }>(`/runs/${pathId(runId)}/steps`, { signal }), [api, runId]);
  const query = usePolling(load, active ? 2000 : 0);
  const steps = query.data?.items ?? [];
  if (!steps.length) return null;
  return <details className="run-steps-box" open={active}><summary>执行过程 · {steps.length} 步</summary><div className="run-steps">{steps.map(step => (
    <div key={step.seq} className={`step ${step.kind}`}>
      {step.kind === 'assistant'
        ? <>
          {step.reasoning && <details className="step-reasoning" open={active}><summary>思考过程</summary><p>{step.reasoning}</p></details>}
          {step.content && <p className="muted">{step.content}</p>}
        </>
        : <details><summary>{step.kind === 'tool_call' ? '工具调用' : '工具结果'} · {step.name}</summary><pre>{step.kind === 'tool_call' ? pretty(step.content) : preview(step.content)}</pre></details>}
    </div>))}</div></details>;
}
