import { Fragment, useCallback, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import 'katex/dist/katex.min.css';
import { ApiClient, apiUrl, errorText, pathId } from './api';
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
  const runById = new Map(runs.map(r => [r.id, r]));
  // 没有对应 assistant 消息的活跃 run —— 夹在用户消息与助手回复之间内联展示
  const orphanRuns = runs.filter(run => activeRun(run.status) && !messages.some(m => m.role === 'assistant' && m.run_id === run.id));
  const lastUserMsgIdx = [...messages].reverse().findIndex(m => m.role === 'user');
  const orphanAfterIdx = lastUserMsgIdx === -1 ? -1 : messages.length - 1 - lastUserMsgIdx;
  useEffect(() => { bottom.current?.scrollIntoView?.({ behavior: 'smooth', block: 'end' }); }, [query.data?.items.at(-1)?.id, orphanRuns.length]);
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
  return <section className="chat"><div className="chat-heading"><div><span className="eyebrow">对话空间</span>{editingTitle ? <form className="inline-edit" onSubmit={event => { event.preventDefault(); if (draftTitle.trim()) { onRename(draftTitle.trim()); } setEditingTitle(false); }}><input value={draftTitle} onChange={event => setDraftTitle(event.target.value)} maxLength={200} autoFocus onBlur={() => { if (draftTitle.trim()) onRename(draftTitle.trim()); setEditingTitle(false); }} onKeyDown={event => { if (event.key === 'Escape') { setDraftTitle(title); setEditingTitle(false); } }} /></form> : <h1 className="editable-title" onClick={() => { setDraftTitle(title); setEditingTitle(true); }} title="点击编辑标题">{title}</h1>}</div>{archived && <span className="status archived">已归档 · 只读</span>}</div>{archived && <div className="archived-banner">此会话已归档，内容只读。你可以恢复归档以继续对话。</div>}<div className="conversation"><div className="conversation-inner">{next != null && <button className="load-more" disabled={paging} onClick={() => void loadOlder()}>{paging ? '正在加载…' : '加载更早的消息'}</button>}{query.loading && !query.data && <p className="muted">正在读取会话…</p>}{query.error && <div className="error" role="alert">{query.error}<button onClick={query.refresh}>重新连接</button></div>}{!query.loading && messages.length === 0 && <div className="empty-chat"><span className="hero-mark small">D</span><h2>把问题交给我</h2><p>说明你的目标、背景和期望的输出，我们一起推进。</p></div>}{messages.map((message, idx) => <Fragment key={message.id}><article className={`message ${message.role === 'user' ? 'user' : 'assistant'}`}><div className="message-meta"><span className="avatar">{message.role === 'user' ? '我' : 'D'}</span><strong>{message.role === 'user' ? '你' : message.role === 'assistant' ? 'Data Agent' : '系统'}</strong><time>{dateLabel(message.created_at)}</time></div><div className="message-body markdown">{message.role === 'assistant' && message.run_id && <RunDetail runId={message.run_id} run={runById.get(message.run_id)} api={api} refresh={query.refresh} />}<ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]} components={{ a: props => <a {...props} target="_blank" rel="noopener noreferrer" />, img: ({ alt }) => <span className="muted">[图片：{alt || '外部图片'}]</span> }}>{message.content}</ReactMarkdown></div></article>{/* 紧接最后一条用户消息之后内联展示进行中的任务 */}{idx === orphanAfterIdx && orphanRuns.map(run => <RunCard key={run.id} run={run} api={api} refresh={query.refresh} />)}</Fragment>)}
      <div ref={bottom} /></div></div>{!archived && <div className="composer-area">{error && <p className="error" role="alert">{error}</p>}<form className="composer" onSubmit={event => { event.preventDefault(); void send(); }}><label className="sr-only" htmlFor="message">输入消息</label><textarea id="message" value={text} readOnly={busy} onChange={event => { setText(event.target.value); setUncertain(false); }} placeholder="描述你的问题，或继续刚才的思考…" rows={3} maxLength={50000} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(); } }} /><div className="composer-footer"><span>{active ? '当前任务结束后可继续发送' : 'Enter 发送 · Shift + Enter 换行'}</span><button className="primary" disabled={busy || active || !text.trim() || !query.data}>{busy ? '提交中…' : uncertain ? '重试发送 ↑' : '发送 ↑'}</button></div></form><p className="composer-note">AI 回答可能存在偏差，请核实重要信息。</p></div>}</section>;
}

/** 嵌入助手消息内的运行详情：状态 + 步骤 + 投递信息 */
function RunDetail({ runId, run, api, refresh }: { runId: string; run: Run | undefined; api: ApiClient; refresh: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [confirm, setConfirm] = useState<string | null>(null);
  const isActive = run ? activeRun(run.status) : false;

  async function action(path: string, body?: unknown) {
    setBusy(true); setError('');
    try { await api.request(path, { method: 'POST', body }); setConfirm(null); refresh(); }
    catch (cause) { setError(errorText(cause)); } finally { setBusy(false); }
  }

  const load = useCallback((signal: AbortSignal) =>
    api.request<{ items: Step[] }>(`/runs/${pathId(runId)}/steps`, { signal }), [api, runId]);
  const query = usePolling(load, isActive ? 500 : 0);
  const steps = query.data?.items ?? [];

  if (!run && !steps.length) return null;

  const thinkingSteps = steps.filter(s => s.kind !== 'text');

  return <div className="run-detail">
    {run && <div className="run-status-line">
      <span className={`run-badge ${run.status}`}>{statusLabel(run.status)}</span>
      <time>{dateLabel(run.created_at)}</time>
      {isActive && <button className="cancel-btn" disabled={busy} onClick={() => action(`/runs/${pathId(runId)}/cancel`)}>取消执行</button>}
    </div>}
    {run?.status === 'failed' && <p className="error">执行失败。{run.error_code && /model|config|provider|api_key/i.test(run.error_code) ? '模型配置不可用，请联系管理员检查模型配置。' : '请检查问题内容，或联系管理员。'}{run.error_code && <small>错误代码：{run.error_code}</small>}</p>}
    {run?.status === 'interrupted' && <p className="muted">执行已中断，可重新发送问题发起新任务。</p>}
    {thinkingSteps.length > 0 && <details className="run-steps-box" open={isActive}><summary>思考过程 · {thinkingSteps.length} 步</summary><div className="run-steps">{thinkingSteps.map(step => (
      <div key={step.seq} className={`step ${step.kind}`}>
        {step.kind === 'assistant'
          ? <>
            {step.reasoning && <details className="step-reasoning" open={isActive}><summary><span className="step-icon">💭</span> 推理</summary><p>{step.reasoning}</p></details>}
            {step.content && <p className="muted">{step.content}</p>}
          </>
          : <details><summary><span className="step-icon">{step.kind === 'tool_call' ? '🔧' : '📋'}</span> {step.kind === 'tool_call' ? '工具调用' : '工具结果'} · {step.name}</summary><pre>{step.kind === 'tool_call' ? pretty(step.content) : preview(step.content)}</pre></details>}
      </div>))}</div></details>}
    {(run?.deliveries ?? []).map(delivery => <div key={delivery.id} className="delivery"><span>飞书投递 · {statusLabel(delivery.status)}</span>{['failed','unknown'].includes(delivery.status) && <button disabled={busy} onClick={() => delivery.status === 'unknown' ? setConfirm(delivery.id) : action(`/deliveries/${pathId(delivery.id)}/retry`, { confirm_duplicate_risk: false })}>重试投递</button>}{confirm === delivery.id && <div className="confirmation" role="alert"><p>上次投递结果未知，消息可能已送达。再次投递可能产生重复消息。</p><button className="danger" disabled={busy} onClick={() => action(`/deliveries/${pathId(delivery.id)}/retry`, { confirm_duplicate_risk: true })}>确认可能重复，仍然重试</button><button disabled={busy} onClick={() => setConfirm(null)}>暂不重试</button></div>}</div>)}
    {error && <p className="error" role="alert">{error}</p>}
  </div>;
}

/** 内联的进行中任务 —— SSE 流式推送思考步骤与最终回复文本 */
function RunCard({ run, api, refresh }: { run: Run; api: ApiClient; refresh: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [steps, setSteps] = useState<Step[]>([]);
  const [done, setDone] = useState(false);
  const isActive = activeRun(run.status);

  // SSE 流式接收步骤
  useEffect(() => {
    if (!isActive) return;
    setSteps([]); setDone(false);
    const es = new EventSource(apiUrl(`/runs/${pathId(run.id)}/steps/stream`));
    es.onmessage = (event) => {
      try { setSteps(prev => [...prev, JSON.parse(event.data)]); }
      catch { /* malformed event, skip */ }
    };
    es.addEventListener('done', (event) => {
      try { const d = JSON.parse(event.data); if (d.status) setDone(true); }
      catch { setDone(true); }
      es.close();
      refresh();
    });
    es.onerror = () => { es.close(); refresh(); };
    return () => es.close();
  }, [isActive, run.id, refresh]);

  // 如果 SSE 还没连上，先用 REST 拉取已有步骤
  useEffect(() => {
    if (!isActive || steps.length > 0) return;
    const controller = new AbortController();
    api.request<{ items: Step[] }>(`/runs/${pathId(run.id)}/steps`, { signal: controller.signal })
      .then(data => { if (!controller.signal.aborted) setSteps(data.items); })
      .catch(() => {});
    return () => controller.abort();
  }, [isActive, run.id, steps.length, api]);

  // 实时拼接流式文本块
  const streamingText = steps.filter(s => s.kind === 'text').map(s => s.content).join('');
  // 实时拼接推理过程
  const streamingReasoning = steps.filter(s => s.kind === 'reasoning').map(s => s.content).join('');
  // 非流式步骤（工具调用 / 工具结果 / 旧版 assistant 事件）
  const thinkingSteps = steps.filter(s => s.kind !== 'text' && s.kind !== 'reasoning');

  return <article className="message assistant">
    <div className="message-meta">
      <span className="avatar thinking-avatar">{isActive ? '⟳' : 'D'}</span>
      <strong>Data Agent</strong>
      <time>{dateLabel(run.created_at)}</time>
      <span className={`run-badge ${run.status}`}>{done ? '已完成' : statusLabel(run.status)}</span>
    </div>
    <div className="message-body">
      {error && <p className="error" role="alert">{error}</p>}
      {run.status === 'failed' && <p className="error">执行失败。{run.error_code && /model|config|provider|api_key/i.test(run.error_code) ? '模型配置不可用，请联系管理员检查模型配置。' : '请检查问题内容，或联系管理员。'}{run.error_code && <small>错误代码：{run.error_code}</small>}</p>}
      {streamingReasoning && <details className="run-steps-box" open={isActive}><summary><span className="step-icon">💭</span> 推理过程</summary><p className="step-reasoning-text">{streamingReasoning}{isActive && <span className="stream-cursor">▊</span>}</p></details>}
      {thinkingSteps.length > 0
        ? <details className="run-steps-box" open={isActive && !streamingText && !streamingReasoning}><summary>思考过程 · {thinkingSteps.length} 步</summary><div className="run-steps">{thinkingSteps.map(step => (
            <div key={step.seq} className={`step ${step.kind}`}>
              {step.kind === 'assistant'
                ? <>
                  {step.reasoning && <details className="step-reasoning"><summary><span className="step-icon">💭</span> 推理记录</summary><p>{step.reasoning}</p></details>}
                  {step.content && <p className="muted">{step.content}</p>}
                </>
                : <details open={isActive}><summary><span className="step-icon">{step.kind === 'tool_call' ? '🔧' : '📋'}</span> {step.kind === 'tool_call' ? '工具调用' : '工具结果'} · {step.name}</summary><pre>{step.kind === 'tool_call' ? pretty(step.content) : preview(step.content)}</pre></details>}
            </div>))}</div></details>
        : isActive && !done && !streamingText && !streamingReasoning && <div className="thinking-block"><p className="thinking-text"><span className="thinking-dots"><span>·</span><span>·</span><span>·</span></span> 正在思考，请稍候…</p></div>}
      {streamingText && <div className={`markdown streaming-text${isActive ? ' live' : ''}`}><ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]}>{streamingText}</ReactMarkdown>{isActive && <span className="stream-cursor">▊</span>}</div>}
      {done && steps.length === 0 && <p className="muted">任务已完成，等待回复…</p>}
      {isActive && <button className="cancel-link" disabled={busy} onClick={async () => {
        setBusy(true); setError('');
        try { await api.request(`/runs/${pathId(run.id)}/cancel`, { method: 'POST' }); refresh(); }
        catch (cause) { setError(errorText(cause)); setBusy(false); }
      }}>取消此任务</button>}
    </div>
  </article>;
}

function preview(text: string, limit = 500) {
  if (text.length <= limit) return text;
  return `${text.slice(0, limit)}…`;
}

function pretty(text: string) {
  try { return JSON.stringify(JSON.parse(text), null, 2); } catch { return text; }
}