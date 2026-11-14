import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  return {
    plugins: [react()],
    server: {
      host: '0.0.0.0',
      proxy: { '/api': env.API_PROXY_TARGET || 'http://localhost:8000' },
      watch: { usePolling: true, interval: 500 },
    },
  };
});
