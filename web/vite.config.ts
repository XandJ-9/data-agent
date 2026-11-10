import { loadEnv } from 'vite';
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  /** VITE_BASE_PATH from web/.env sets the deploy subpath, e.g. /data-agent/ */
  const base = loadEnv(mode, process.cwd(), '').VITE_BASE_PATH || '/';
  const proxyPath = base === '/' ? '/api' : `${base}api`;

  return {
    base,
    plugins: [react()],
    server: {
      port: 5174,
      strictPort: true,
      proxy: {
        [proxyPath]: {
          target: 'http://127.0.0.1:8100',
          // Strip the base path before forwarding so the backend always sees
          // requests starting at /api/v1/ regardless of the SPA subpath.
          rewrite: (path: string) => (base === '/' ? path : path.replace(base, '')),
          configure: (proxy) => {
            proxy.on('proxyReq', (proxyReq) => {
              // Rewrite Origin to match the backend's default trusted origin so
              // no DATA_AGENT_PUBLIC_ORIGIN env var is needed in dev.
              proxyReq.setHeader('origin', 'http://127.0.0.1:8100');
            });
          },
        },
      },
    },
    test: { environment: 'jsdom', globals: true, clearMocks: true, unstubGlobals: true, setupFiles: ['src/setup.ts'] },
  };
});