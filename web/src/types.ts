export interface Auth { user: { id: string; username: string }; csrf_token: string; is_admin?: boolean }
export interface Session { id: string; title: string; created_at: string; archived_at?: string }
export interface Message { id: number; role: string; content: string; created_at: string; run_id: string | null }
export interface Delivery { id: string; status: string }
export interface Run { id: string; status: string; error_code: string | null; created_at: string; deliveries: Delivery[] }
export interface Step { seq: number; kind: string; name: string | null; content: string; reasoning: string | null; created_at: string }
export interface Messages { items: Message[]; runs: Run[]; next_before_id?: number | null }
export interface MemoryEntry { id: string; text: string; source_message_ids: number[]; updated_at: string }
export interface Memory { revision: number; entries: MemoryEntry[] }
export interface Skill { name: string; description: string; version: string; enabled: boolean }
export interface Binding { enabled: boolean; binding: null | { id: string; external_user_id: string; session_id: string | null } }
export interface McpServer { name: string; transport: string; command: string | null; args: string[]; env: Record<string, string>; url: string | null; headers: Record<string, string>; enabled: boolean }
export const activeRun = (status: string) => status === 'queued' || status === 'running';
export const statusLabel = (status: string) => ({ queued: '排队中', running: '执行中', completed: '已完成', succeeded: '已完成', failed: '失败', cancelled: '已取消', interrupted: '已中断', pending: '待投递', sending: '投递中', sent: '已送达', delivered: '已送达', unknown: '结果未知' }[status] ?? status);
export const dateLabel = (value: string) => new Date(value).toLocaleString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
