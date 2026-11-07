import { beforeEach, expect, it, vi } from 'vitest';
import { ApiClient, ApiError } from './api';
const fetchMock = vi.fn();
beforeEach(() => vi.stubGlobal('fetch', fetchMock));
it('uses cookie credentials and CSRF only for writes', async () => {
  fetchMock.mockImplementation(async () => new Response('{}'));
  const api = new ApiClient(() => {}); api.setToken('csrf-example');
  await api.request('/sessions'); await api.request('/sessions', { method: 'POST', body: { title: '测试' } });
  expect(fetchMock.mock.calls[0][1].credentials).toBe('same-origin');
  expect(fetchMock.mock.calls[0][1].headers.get('X-CSRF-Token')).toBeNull();
  expect(fetchMock.mock.calls[1][1].headers.get('X-CSRF-Token')).toBe('csrf-example');
});
it('expires auth on 401 and clears token', async () => {
  const expired = vi.fn(); const api = new ApiClient(expired); api.setToken('old');
  fetchMock.mockResolvedValue(new Response('{"detail":"请登录"}', { status: 401 }));
  await expect(api.request('/memory')).rejects.toBeInstanceOf(ApiError);
  expect(expired).toHaveBeenCalledOnce();
  fetchMock.mockResolvedValue(new Response('{}')); await api.request('/auth/login', { method: 'POST', body: {} });
  expect(fetchMock.mock.calls[1][1].headers.get('X-CSRF-Token')).toBeNull();
});
it('preserves conflict detail and supports empty responses', async () => {
  const api = new ApiClient(() => {});
  fetchMock.mockResolvedValueOnce(new Response('{"detail":"版本冲突"}', { status: 409 }));
  await expect(api.request('/memory')).rejects.toMatchObject({ status: 409, message: '版本冲突' });
  fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
  await expect(api.request('/auth/logout', { method: 'POST' })).resolves.toBeUndefined();
});
