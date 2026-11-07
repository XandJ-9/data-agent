import { useCallback, useEffect, useMemo, useState } from 'react';
import { ApiClient, ApiError, errorText } from './api';
import type { Auth, Session } from './types';
import { usePolling } from './usePolling';
import { Chat } from './Chat';
import { Settings } from './Settings';

export function App() {
  const [auth, setAuth] = useState<Auth | null>(null);
  const [checking, setChecking] = useState(true);
  const [error, setError] = useState('');
  const api = useMemo(() => new ApiClient(() => setAuth(null)), []);
  const accept = useCallback((value: Auth) => { api.setToken(value.csrf_token); setAuth(value); }, [api]);
  useEffect(() => {
    const controller = new AbortController();
    api.request<Auth>('/auth/me', { signal: controller.signal }).then(value => {
      if (!controller.signal.aborted) accept(value);
    }).catch(cause => {
      if (!controller.signal.aborted && !(cause instanceof ApiError && cause.status === 401)) setError(errorText(cause));
    }).finally(() => { if (!controller.signal.aborted) setChecking(false); });
    return () => controller.abort();
  }, [api, accept]);
  if (checking) return <div className="splash">正在打开工作台…</div>;
  if (!auth) return <Login api={api} accept={accept} initialError={error} />;
  return <Workspace key={auth.user.id} api={api} auth={auth} logout={() => { api.setToken(''); setAuth(null); }} />;
}
function Login({ api, accept, initialError }: { api: ApiClient; accept: (auth: Auth) => void; initialError: string }) {
  const [error, setError] = useState(initialError);
  const [busy, setBusy] = useState(false);
  return <main className="login"><section className="login-story"><div className="brand"><span className="brand-mark">D</span> DATA AGENT</div><div><span className="eyebrow">你的独立 AI 工作空间</span><h1>从一个问题，<br />开始深入思考。</h1><p>让对话持续，让知识积累。<br />在专属工作台中，与 AI 一起推进工作。</p></div><small>对话 · 记忆 · 技能</small></section><section className="login-form"><form onSubmit={async event => {
    event.preventDefault(); if (busy) return;
    const form = new FormData(event.currentTarget); setBusy(true); setError('');
    try { accept(await api.request<Auth>('/auth/login', { method: 'POST', body: { username: String(form.get('username')).trim(), password: form.get('password') } })); }
    catch (cause) { setError(errorText(cause)); } finally { setBusy(false); }
  }}><span className="eyebrow">欢迎回来</span><h2>登录工作台</h2><p className="muted">请联系管理员创建账号，再使用账号密码登录。</p><label>用户名<input name="username" autoComplete="username" required autoFocus maxLength={200} /></label><label>密码<input name="password" type="password" autoComplete="current-password" required /></label>{error && <p role="alert" className="error">{error}</p>}<button className="primary" disabled={busy}>{busy ? '正在登录…' : '登录工作台 →'}</button><small className="muted">账号与会话由独立 Data Agent 服务管理。</small></form></section></main>;
}
function Workspace({ api, auth, logout }: { api: ApiClient; auth: Auth; logout: () => void }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [view, setView] = useState('chat');
  const [mobile, setMobile] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const load = useCallback((signal: AbortSignal) => api.request<{ items: Session[] }>('/sessions', { signal }), [api]);
  const sessions = usePolling(load, 10000);
  async function create() {
    setBusy(true); setError('');
    try { const session = await api.request<Session>('/sessions', { method: 'POST', body: {} }); setSelected(session.id); setView('chat'); setMobile(false); sessions.refresh(); }
    catch (cause) { setError(errorText(cause)); } finally { setBusy(false); }
  }
  return <div className="workspace"><aside className={`sidebar ${mobile ? 'open' : ''}`}><div className="brand"><span className="brand-mark">D</span> Data Agent<button className="mobile-only close" onClick={() => setMobile(false)} aria-label="关闭导航">×</button></div><button className="new-session" disabled={busy} onClick={() => void create()}>＋ 新建会话</button><div className="section-label">工作空间</div><nav aria-label="工作空间">{[['chat','对话','◈'],['memory','记忆','◇'],['skills','Skills','⌘'],['feishu','飞书连接','↗']].map(([id,label,icon]) => <button key={id} className={view === id ? 'nav-item active' : 'nav-item'} onClick={() => { setView(id); setMobile(false); }}><span>{icon}</span>{label}</button>)}</nav><div className="section-label">最近会话 <span>{sessions.data?.items.length ?? '—'}</span></div><div className="session-list">{sessions.loading && !sessions.data && <p className="muted">正在加载…</p>}{sessions.error && <p role="alert" className="error">{sessions.error}<button onClick={sessions.refresh}>重试</button></p>}{sessions.data?.items.length === 0 && <p className="muted">还没有会话，开始第一次对话吧。</p>}{sessions.data?.items.map(session => <button key={session.id} className={`session ${selected === session.id && view === 'chat' ? 'selected' : ''}`} onClick={() => { setSelected(session.id); setView('chat'); setMobile(false); }} title={session.title}>{session.title || '新会话'}</button>)}</div><footer className="account"><span className="avatar">{auth.user.username.slice(0,1).toUpperCase()}</span><span>{auth.user.username}<small>个人工作空间</small></span><button aria-label="退出登录" title="退出登录" disabled={busy} onClick={async () => { setBusy(true); try { await api.request('/auth/logout', { method: 'POST' }); logout(); } catch (cause) { setError(errorText(cause)); } finally { setBusy(false); } }}>退出</button></footer></aside><main className="main"><header className="topbar"><button className="mobile-only" aria-label="打开导航" onClick={() => setMobile(true)}>☰</button><span>工作台 <span className="separator">/</span> {view === 'chat' ? '对话' : view === 'memory' ? '记忆' : view === 'skills' ? 'Skills' : '飞书连接'}</span><span className="private-label">专属工作空间</span></header>{error && <div role="alert" className="error banner">{error}</div>}{view === 'chat' ? selected ? <Chat key={selected} api={api} sessionId={selected} title={sessions.data?.items.find(item => item.id === selected)?.title || '新会话'} /> : <section className="welcome"><span className="hero-mark">D</span><span className="eyebrow">想法，从这里展开</span><h1>今天，我们一起解决什么？</h1><p>创建一段对话，描述你的目标。<br />执行进度与回答会保存在会话中，随时回来继续。</p><button className="primary" disabled={busy} onClick={() => void create()}>开启新会话 →</button><div className="welcome-cards"><article><span>01</span><h3>专注对话</h3><p>用自然语言描述问题，逐步探索答案。</p></article><article><span>02</span><h3>持续记忆</h3><p>查看与维护助手记住的上下文。</p></article><article><span>03</span><h3>连接飞书</h3><p>绑定账号，让对话在不同入口延续。</p></article></div></section> : <Settings key={view} api={api} view={view} sessionId={selected} />}</main></div>;
}
