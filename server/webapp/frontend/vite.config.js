import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      // All backend API routes are mounted under /api (server/main.py) so they
      // never collide with the React page routes (/conversations, /files,
      // /gmail, /actions, /onlyoffice...). Pages are served by Vite's own SPA
      // fallback. The OnlyOffice docserver container calls /onlyoffice/* and
      // /wopi/* directly on port 8000 (host.docker.internal), not through Vite.
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/docbuilder': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/wopi': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/health': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
});
