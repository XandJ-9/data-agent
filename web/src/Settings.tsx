import { useCallback, useState } from 'react';
import { ApiClient, ApiError, errorText, pathId } from './api';
import { usePolling } from './usePolling';
import { dateLabel, type Memory, type Skill, type Binding } from './types';

export function Settings({ api, view, sessionId }: { api: ApiClient; view: string; sessionId: string | null }) {
  const title = view === 'memory' ? '让助手更了解你' : view === 'skills' ? '为工作选择合适的技能' : '让对话连接到飞书';
  const description = view === 'memory' ? '管理持久记忆。你可以修改内容，也可以随时让助手忘记。' : view === 'skills' ? '查看服务提供的技能，并决定哪些技能可以用于你的任务。' : '绑定你的飞书账号，将飞书入口关联到当前会话。';
  return <section className="settings"><span className="eyebrow">{view === 'memory' ? 'MEMORY' : view === 'skills' ? 'SKILLS' : 'CONNECTION'}</span><h1>{title}</h1><p className="muted">{description}</p>{view === 'memory' ? <MemoryPanel api={api} /> : view === 'skills' ? <SkillsPanel api={api} /> : <FeishuPanel api={api} sessionId={sessionId} />}</section>;
}
function MemoryPanel({ api }: { api: ApiClient }) {
  const load = useCallback((signal: AbortSignal) => api.request<Memory>('/memory', { signal }), [api]);
  const query = usePolling(load, 0);
  const [draft, setDraft] = useState<Memory | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [conflict, setConflict] = useState(false);
  const [forget, setForget] = useState<string | null>(null);
  const [saved, setSaved] = useState<Memory | null>(null);
  const memory = saved ?? query.data;
  async function update(entryId?: string) {
    const source = draft ?? memory;
    if (!source) return;
    setBusy(true); setError('');
    try {
      const result = await api.request<Memory>(entryId ? '/memory/forget' : '/memory', { method: entryId ? 'POST' : 'PUT', body: entryId ? { expected_revision: source.revision, entry_id: entryId } : { expected_revision: source.revision, entries: source.entries.map(({ id, text, source_message_ids }) => ({ id, text: text.trim(), source_message_ids })) } });
      setSaved(result); setDraft(null); setForget(null); setConflict(false);
    } catch (cause) { setError(errorText(cause)); if (cause instanceof ApiError && cause.status === 409) setConflict(true); }
    finally { setBusy(false); }
  }
  return <div className="panel"><div className="panel-heading"><h2>个人记忆</h2><span className="muted">版本 {memory?.revision ?? '—'}</span><button disabled={busy} onClick={() => { if (draft && !window.confirm('重新加载将丢弃尚未保存的编辑，是否继续？')) return; setDraft(null); setSaved(null); setConflict(false); setForget(null); setError(''); query.refresh(); }}>重新加载</button></div>{query.loading && !memory && <p>正在读取记忆…</p>}{query.error && <p className="error" role="alert">{query.error}</p>}{error && <p className="error" role="alert">{error}</p>}{conflict && <p className="conflict" role="alert">记忆已在其他位置更新。当前草稿已保留，请先复制需要保留的内容，再点击“重新加载”获取最新版本后编辑。</p>}{memory?.entries.length === 0 && <div className="empty-state">尚无记忆。对话中积累的持久记忆将在这里显示。</div>}{(draft ?? memory)?.entries.map(entry => <article key={entry.id} className="memory-entry">{draft ? <label>记忆内容<textarea aria-label={`记忆 ${entry.id}`} value={entry.text} disabled={busy} onChange={event => setDraft(previous => previous ? { ...previous, entries: previous.entries.map(item => item.id === entry.id ? { ...item, text: event.target.value } : item) } : null)} /></label> : <p>{entry.text}</p>}<div className="entry-meta"><small>更新于 {dateLabel(entry.updated_at)}{entry.source_message_ids.length > 0 && ` · 来源消息 ${entry.source_message_ids.join('、')}`}</small>{!draft && <button disabled={busy} onClick={() => setForget(entry.id)}>忘记</button>}</div>{forget === entry.id && <div className="confirmation"><p>确认忘记这条记忆？此操作不会删除原会话消息。</p><button className="danger" disabled={busy || conflict} onClick={() => void update(entry.id)}>确认忘记</button><button disabled={busy} onClick={() => setForget(null)}>保留</button></div>}</article>)}{memory && memory.entries.length > 0 && <div className="actions">{draft ? <><button className="primary" disabled={busy || conflict || draft.entries.some(entry => !entry.text.trim())} onClick={() => void update()}>{busy ? '保存中…' : '保存修改'}</button><button disabled={busy} onClick={() => setDraft(null)}>取消编辑</button></> : <button disabled={busy || conflict} onClick={() => { setDraft(memory); setForget(null); }}>编辑记忆</button>}</div>}</div>;
}
function SkillsPanel({ api }: { api: ApiClient }) {
  const load = useCallback((signal: AbortSignal) => api.request<{ items: Skill[] }>('/skills', { signal }), [api]);
  const query = usePolling(load, 0);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState('');
  return <div className="skill-grid">{query.loading && <p>正在读取技能…</p>}{(query.error || error) && <p className="error" role="alert">{query.error || error}<button onClick={query.refresh}>刷新</button></p>}{query.data?.items.length === 0 && <div className="panel empty-state">管理员尚未安装可用技能。</div>}{query.data?.items.map(skill => <article key={skill.name} className="panel skill-card"><div className="panel-heading"><span className="skill-symbol">⌘</span><button role="switch" aria-label={`${skill.name} 技能`} aria-checked={skill.enabled} className={`toggle ${skill.enabled ? 'on' : ''}`} disabled={busy !== null || query.loading} onClick={async () => { setBusy(skill.name); setError(''); try { await api.request(`/skills/${pathId(skill.name)}`, { method: 'PUT', body: { enabled: !skill.enabled } }); query.refresh(); } catch (cause) { setError(errorText(cause)); } finally { setBusy(null); } }}><span /></button></div><h2>{skill.name}</h2><p className="muted">{skill.description}</p><small>v{skill.version} · {skill.enabled ? '已启用' : '未启用'}</small></article>)}</div>;
}
function FeishuPanel({ api, sessionId }: { api: ApiClient; sessionId: string | null }) {
  const load = useCallback((signal: AbortSignal) => api.request<Binding>('/bindings/feishu', { signal }), [api]);
  const query = usePolling(load, 5000);
  const [code, setCode] = useState<{ code: string; expires_at: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [confirm, setConfirm] = useState(false);
  async function action(path: string, method: string, body?: unknown) {
    setBusy(true); setError('');
    try { await api.request(path, { method, body }); setConfirm(false); setCode(null); query.refresh(); }
    catch (cause) { setError(errorText(cause)); } finally { setBusy(false); }
  }
  const binding = query.data?.binding;
  return <div className="panel feishu-panel"><div className="panel-heading"><h2>飞书账号</h2><span className="status">{binding ? '已绑定' : '未绑定'}</span></div>{(query.error || error) && <p className="error" role="alert">{query.error || error}</p>}{!query.data && query.loading && <p>正在读取连接状态…</p>}{query.data && !query.data.enabled ? <p className="muted">飞书连接尚未启用，请联系管理员配置服务。</p> : query.data && <>{binding ? <><dl><dt>飞书用户</dt><dd>{binding.external_user_id}</dd><dt>关联会话</dt><dd>{binding.session_id || '尚未关联'}</dd></dl><button className="primary" disabled={busy || !sessionId || binding.session_id === sessionId} onClick={() => void action('/bindings/feishu/session', 'PUT', { session_id: sessionId })}>{sessionId && binding.session_id === sessionId ? '已关联当前会话' : '关联当前会话'}</button>{!sessionId && <p className="muted">请先从侧栏选择或创建一个会话，再进行关联。</p>}<hr /><button className="danger" disabled={busy} onClick={() => setConfirm(true)}>解除绑定</button>{confirm && <div className="confirmation"><p>确认解除当前飞书账号的绑定？</p><button className="danger" disabled={busy} onClick={() => void action('/bindings/feishu', 'DELETE')}>确认解除</button><button disabled={busy} onClick={() => setConfirm(false)}>取消</button></div>}</> : <><p className="muted">生成一次性绑定码，然后将绑定码发送给管理员配置的飞书机器人。绑定成功后，此处会自动更新。</p><button className="primary" disabled={busy} onClick={async () => { setBusy(true); setError(''); try { setCode(await api.request('/bindings/feishu/codes', { method: 'POST' })); } catch (cause) { setError(errorText(cause)); } finally { setBusy(false); } }}>{busy ? '正在生成…' : code ? '重新生成绑定码' : '生成绑定码'}</button>{code && <div className="binding-code"><small>一次性绑定码</small><strong>{code.code}</strong><p>{Date.parse(code.expires_at) <= Date.now() ? '绑定码已过期，请重新生成。' : `有效期至 ${dateLabel(code.expires_at)}`}</p></div>}</>}</>}<button className="refresh-link" disabled={busy} onClick={query.refresh}>刷新连接状态</button></div>;
}
