import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
export default defineConfig({
  plugins: [react()],
  server: {
    // Dev proxy: the API requires the browser Origin to match the backend's trusted origin
    // exactly, so rewrite it here (localhost vs 127.0.0.1 are different origins to the guard).
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8100',
        // Dev-only: normalize the browser Origin to the backend's trusted origin so both
        // localhost and 127.0.0.1 work in development. The API's own exact-Origin check stays.
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq) => {
            const trusted = 'http://localhost:5173';
            if (proxyReq.getHeader('origin') !== trusted) proxyReq.setHeader('origin', trusted);
          });
        },
      },
    },
  },
  test: { environment: 'jsdom', globals: true, clearMocks: true, unstubGlobals: true, setupFiles: ['src/setup.ts'] },
});
