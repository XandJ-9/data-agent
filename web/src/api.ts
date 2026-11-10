/** Resolved at build time from Vite's `base` config; always "/" in dev. */
const API_BASE = `${import.meta.env.BASE_URL.replace(/\/+$/, '')}/api/v1`;

/** Build a full API URL for the given path (e.g. "/sessions/abc/messages"). */
export const apiUrl = (path: string) => `${API_BASE}${path}`;

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
interface Options { method?: string; body?: unknown; signal?: AbortSignal }
export class ApiClient {
  private token = '';
  constructor(private onUnauthorized: () => void) {}
  setToken(token: string) { this.token = token; }
  async request<T>(path: string, options: Options = {}): Promise<T> {
    const method = options.method ?? 'GET';
    const headers = new Headers({ Accept: 'application/json' });
    if (options.body !== undefined) headers.set('Content-Type', 'application/json');
    if (method !== 'GET' && this.token) headers.set('X-CSRF-Token', this.token);
    let response: Response;
    try {
      response = await fetch(apiUrl(path), { method, headers, credentials: 'same-origin', signal: options.signal, body: options.body === undefined ? undefined : JSON.stringify(options.body) });
    } catch (error) {
      if (options.signal?.aborted) throw error;
      throw new ApiError(0, '无法连接服务，请检查网络后重试。');
    }
    if (response.status === 401) { this.token = ''; this.onUnauthorized(); }
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      throw new ApiError(response.status, typeof payload?.detail === 'string' ? payload.detail : `请求失败（${response.status}），请重试。`);
    }
    if (response.status === 204) return undefined as T;
    const text = await response.text();
    if (!text) return undefined as T;
    try { return JSON.parse(text) as T; }
    catch { throw new ApiError(502, '服务返回了无法识别的数据，请联系管理员。'); }
  }
}
export const errorText = (error: unknown) => error instanceof Error ? error.message : '操作失败，请重试。';
export const pathId = (id: string) => encodeURIComponent(id);
