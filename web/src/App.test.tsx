import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';
import { App } from './App';

const responses: Record<string, { body?: unknown; status: number }> = {};
const calls: { path: string; init?: RequestInit }[] = [];
let fetchResolver: ((init: Response) => void) | null = null;

function jsonResponse(body: unknown, status = 200) {
  return new Response(body === undefined ? null : JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}
function set(path: string, body: unknown, status = 200) { responses[path] = { body, status }; }
const baseFetch = vi.fn(async (_url: string | URL, init?: RequestInit) => {
  const path = String(_url).replace(/^.*\/api\/v1/, '');
  calls.push({ path, init });
  if (fetchResolver) { const resolve = fetchResolver; fetchResolver = null; return resolve(jsonResponse(responses[path]?.body ?? {}, responses[path]?.status ?? 200)); }
  return jsonResponse(responses[path]?.body ?? {}, responses[path]?.status ?? 200);
});

function loginResponses() {
  Object.assign(responses, {});
  set('/auth/me', { detail: '请登录' }, 401);
  set('/auth/login', { user: { id: 'u1', username: 'ada' }, csrf_token: 'csrf-1' });
  set('/sessions', { items: [{ id: 's1', title: '销售分析', created_at: '2026-01-01T00:00:00Z' }, { id: 's2', title: '库存会话', created_at: '2026-01-02T00:00:00Z' }] });
  set('/sessions/s1/messages', { items: [{ id: 1, role: 'user', content: '你好', created_at: '2026-01-01T00:00:00Z', run_id: null }], runs: [], next_before_id: null });
  set('/sessions/s2/messages', { items: [{ id: 2, role: 'assistant', content: '库存正常', created_at: '2026-01-02T00:00:00Z', run_id: null }], runs: [], next_before_id: null });
  set('/memory', { revision: 3, entries: [{ id: 'm1', text: '偏好中文', source_message_ids: [1], updated_at: '2026-01-01T00:00:00Z' }] });
}

beforeEach(() => {
  calls.length = 0; fetchResolver = null;
  for (const key of Object.keys(responses)) delete responses[key];
  set('/auth/me', { detail: '请登录' }, 401);
  localStorage.clear();
  vi.stubGlobal('fetch', baseFetch);
});

async function login() {
  render(<App />);
  await userEvent.type(await screen.findByLabelText('用户名'), 'ada');
  await userEvent.type(screen.getByLabelText('密码'), 'secret');
  await userEvent.click(screen.getByRole('button', { name: /登录工作台/ }));
  await screen.findByText('销售分析');
}
async function openSession() {
  await userEvent.click(await screen.findByRole('button', { name: /销售分析/ }));
  await screen.findByText('对话空间');
}

it('login sends credentials and stores CSRF for later writes', async () => {
  loginResponses();
  await login();
  const loginCall = calls.find(call => call.path === '/auth/login')!;
  expect(loginCall.init!.credentials).toBe('same-origin');
  expect((loginCall.init!.headers as Headers).get('X-CSRF-Token')).toBeNull();
  await userEvent.click(screen.getByRole('button', { name: '＋ 新建会话' }));
  const createCall = calls.find(call => call.path === '/sessions' && call.init?.method === 'POST')!;
  expect((createCall.init!.headers as Headers).get('X-CSRF-Token')).toBe('csrf-1');
  expect(JSON.parse(String(createCall.init!.body)).client_message_id).toBeUndefined();
});

it('shows login error and admin hint, then recovers', async () => {
  render(<App />);
  await userEvent.type(await screen.findByLabelText('用户名'), 'ada');
  await userEvent.type(screen.getByLabelText('密码'), 'bad');
  set('/auth/login', { detail: '用户名或密码不正确' }, 401);
  await userEvent.click(screen.getByRole('button', { name: /登录工作台/ }));
  expect(await screen.findByRole('alert')).toHaveTextContent('用户名或密码不正确');
  expect(screen.getByText(/请联系管理员/)).toBeInTheDocument();
  set('/auth/login', { user: { id: 'u1', username: 'ada' }, csrf_token: 'csrf-1' });
  set('/sessions', { items: [] });
  await userEvent.click(screen.getByRole('button', { name: /登录工作台/ }));
  expect(await screen.findByText('＋ 新建会话')).toBeInTheDocument();
});

it('keeps the newly selected session when a stale poll resolves late', async () => {
  loginResponses();
  await login();
  let releaseStale!: (value: Response) => void;
  const stale = new Promise<Response>(resolve => { releaseStale = resolve; });
  vi.stubGlobal('fetch', vi.fn(async (url: string | URL, init?: RequestInit) => {
    const path = String(url).replace(/^.*\/api\/v1/, '');
    calls.push({ path, init });
    if (path === '/sessions/s1/messages' && !responses.__served1) { responses.__served1 = { body: {}, status: 200 }; return stale; }
    return jsonResponse(responses[path]?.body ?? {}, responses[path]?.status ?? 200);
  }));
  await userEvent.click(await screen.findByRole('button', { name: '销售分析' }));
  await userEvent.click(await screen.findByRole('button', { name: '库存会话' }));
  expect(await screen.findByText('库存正常')).toBeInTheDocument();
  releaseStale(jsonResponse(responses['/sessions/s1/messages'].body));
  await waitFor(() => expect(calls.filter(call => call.path === '/sessions/s1/messages').length).toBeGreaterThan(0));
  expect(screen.getByText('库存正常')).toBeInTheDocument();
  expect(screen.queryByText('你好')).not.toBeInTheDocument();
});

it('cancels a running run and reflects the terminal status', async () => {
  loginResponses();
  set('/sessions/s1/messages', {
    items: [
      { id: 1, role: 'user', content: '分析销量', created_at: '2026-01-01T00:00:00Z', run_id: null },
      { id: 2, role: 'assistant', content: '分析中…', created_at: '2026-01-01T00:00:01Z', run_id: 'r1' },
    ],
    runs: [{ id: 'r1', status: 'running', error_code: null, created_at: '2026-01-01T00:00:00Z', deliveries: [] }],
    next_before_id: null,
  });
  await login();
  await openSession();
  await screen.findByText('分析销量');
  const cancel = await screen.findByRole('button', { name: '取消执行' });
  expect(screen.getByText('执行中')).toBeInTheDocument();
  set('/sessions/s1/messages', {
    items: [
      { id: 1, role: 'user', content: '分析销量', created_at: '2026-01-01T00:00:00Z', run_id: null },
      { id: 2, role: 'assistant', content: '分析中…', created_at: '2026-01-01T00:00:01Z', run_id: 'r1' },
    ],
    runs: [{ id: 'r1', status: 'cancelled', error_code: null, created_at: '2026-01-01T00:00:00Z', deliveries: [] }],
    next_before_id: null,
  });
  await userEvent.click(cancel);
  await waitFor(() => expect(calls.some(call => call.path === '/runs/r1/cancel')).toBe(true));
  expect(await screen.findByText('已取消')).toBeInTheDocument();
});

it('surfaces failed runs as a model configuration problem and blocks sending', async () => {
  loginResponses();
  set('/sessions/s1/messages', {
    items: [
      { id: 1, role: 'user', content: '分析销量', created_at: '2026-01-01T00:00:00Z', run_id: null },
      { id: 2, role: 'assistant', content: '分析失败', created_at: '2026-01-01T00:00:01Z', run_id: 'r1' },
    ],
    runs: [{ id: 'r1', status: 'failed', error_code: 'model_config_unavailable', created_at: '2026-01-01T00:00:00Z', deliveries: [] }],
    next_before_id: null,
  });
  await login();
  await openSession();
  expect(await screen.findByText(/模型配置不可用/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /发送/ })).toBeDisabled();
});

it('shows delivery unknown retry with explicit duplicate confirmation', async () => {
  loginResponses();
  set('/sessions/s1/messages', {
    items: [{ id: 1, role: 'assistant', content: '分析完成', created_at: '2026-01-01T00:00:00Z', run_id: 'r1' }],
    runs: [{ id: 'r1', status: 'completed', error_code: null, created_at: '2026-01-01T00:00:00Z', deliveries: [{ id: 'd1', status: 'unknown' }] }],
    next_before_id: null,
  });
  await login();
  await openSession();
  await screen.findByText(/结果未知/);
  await userEvent.click(screen.getByRole('button', { name: '重试投递' }));
  expect(await screen.findByText(/可能产生重复消息/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: /仍然重试/ }));
  const retry = calls.find(call => call.path === '/deliveries/d1/retry')!;
  expect(JSON.parse(String(retry.init!.body))).toEqual({ confirm_duplicate_risk: true });
});

it('keeps draft edits and explains memory revision conflicts', async () => {
  loginResponses();
  await login();
  await userEvent.click(screen.getByRole('button', { name: /记忆/ }));
  await screen.findByText('偏好中文');
  await userEvent.click(screen.getByRole('button', { name: '编辑记忆' }));
  const box = screen.getByLabelText(/记忆 m1/);
  await userEvent.clear(box);
  await userEvent.type(box, '偏好中文，简体');
  set('/memory', { detail: '记忆版本已变化，请重新加载' }, 409);
  await userEvent.click(screen.getByRole('button', { name: '保存修改' }));
  expect(await screen.findByText(/记忆已在其他位置更新/)).toBeInTheDocument();
  expect(screen.getByLabelText(/记忆 m1/)).toHaveValue('偏好中文，简体');
  expect(screen.getByRole('button', { name: '保存修改' })).toBeDisabled();
  expect((calls.find(call => call.init?.method === 'PUT')!.init!.headers as Headers).get('X-CSRF-Token')).toBe('csrf-1');
});

it('toggles a skill off with CSRF header', async () => {
  loginResponses();
  set('/skills', { items: [{ name: 'sql-analysis', description: 'SQL 分析', version: '1.0.0', enabled: true }] });
  await login();
  await userEvent.click(screen.getByRole('button', { name: /Skills/ }));
  const toggle = await screen.findByRole('switch', { name: 'sql-analysis 技能' });
  expect(toggle).toHaveAttribute('aria-checked', 'true');
  set('/skills/sql-analysis', {});
  await userEvent.click(toggle);
  await waitFor(() => expect(calls.some(call => call.path === '/skills/sql-analysis' && call.init?.method === 'PUT')).toBe(true));
  const put = calls.find(call => call.path === '/skills/sql-analysis')!;
  expect((put.init!.headers as Headers).get('X-CSRF-Token')).toBe('csrf-1');
  expect(JSON.parse(String(put.init!.body))).toEqual({ enabled: false });
});

it('creates a Feishu binding code and unbinds with confirmation', async () => {
  loginResponses();
  set('/bindings/feishu', { enabled: true, binding: null });
  await login();
  await userEvent.click(screen.getByRole('button', { name: /飞书连接/ }));
  expect(await screen.findByText(/发送给管理员/)).toBeInTheDocument();
  set('/bindings/feishu/codes', { code: 'AB12CD', expires_at: '2026-12-31T00:00:00Z' });
  await userEvent.click(screen.getByRole('button', { name: '生成绑定码' }));
  expect(await screen.findByText('AB12CD')).toBeInTheDocument();
  set('/bindings/feishu', { enabled: true, binding: { id: 'b1', external_user_id: 'ou-9', session_id: 's1' } });
  await userEvent.click(screen.getByRole('button', { name: '刷新连接状态' }));
  expect(await screen.findByText('ou-9')).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '解除绑定' }));
  await userEvent.click(await screen.findByRole('button', { name: '确认解除' }));
  await waitFor(() => expect(calls.some(call => call.path === '/bindings/feishu' && call.init?.method === 'DELETE')).toBe(true));
});
