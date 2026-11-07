import { useCallback, useEffect, useState } from 'react';
import { errorText } from './api';

// One request at a time; cleanup also guards transports that ignore abort.
export function usePolling<T>(load: (signal: AbortSignal) => Promise<T>, interval = 3000) {
  const [state, setState] = useState<{ data?: T; error: string; loading: boolean }>({ error: '', loading: true });
  const [version, setVersion] = useState(0);
  const refresh = useCallback(() => setVersion(value => value + 1), []);
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    setState(previous => ({ ...previous, error: '', loading: true }));
    async function tick() {
      try {
        const data = await load(controller.signal);
        if (alive) setState({ data, error: '', loading: false });
      } catch (error) {
        if (alive) setState(previous => ({ ...previous, error: errorText(error), loading: false }));
      } finally {
        if (alive && interval > 0) timer = setTimeout(tick, interval);
      }
    }
    void tick();
    return () => { alive = false; controller.abort(); clearTimeout(timer); };
  }, [load, interval, version]);
  return { ...state, refresh };
}
