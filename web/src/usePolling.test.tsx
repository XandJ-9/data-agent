import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { usePolling } from './usePolling';
afterEach(() => { cleanup(); vi.useRealTimers(); });
it('ignores old session responses even when abort is ignored', async () => {
  let resolveOld!: (value: string) => void;
  const oldLoad = vi.fn(() => new Promise<string>(resolve => { resolveOld = resolve; }));
  const newLoad = vi.fn(async () => 'new session');
  const { result, rerender } = renderHook(({ load }) => usePolling(load), { initialProps: { load: oldLoad as (signal: AbortSignal) => Promise<string> } });
  rerender({ load: newLoad });
  await act(async () => {});
  expect(result.current.data).toBe('new session');
  await act(async () => resolveOld('stale session'));
  expect(result.current.data).toBe('new session');
});
it('does not poll again after unmount and aborts pending requests', async () => {
  vi.useFakeTimers(); let signal!: AbortSignal;
  const load = vi.fn(async (value: AbortSignal) => { signal = value; return 'ok'; });
  const { unmount } = renderHook(() => usePolling(load));
  await act(async () => {}); unmount();
  expect(signal.aborted).toBe(true);
  await act(async () => vi.advanceTimersByTime(10000));
  expect(load).toHaveBeenCalledTimes(1);
});
it('recovers after polling failure', async () => {
  vi.useFakeTimers();
  const load = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue('recovered');
  const { result } = renderHook(() => usePolling(load));
  await act(async () => {}); expect(result.current.error).toBe('offline');
  await act(async () => vi.advanceTimersByTime(3000));
  expect(result.current.data).toBe('recovered'); expect(result.current.error).toBe('');
});
