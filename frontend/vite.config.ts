import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [react()],
  build: {
    target: 'es2022',
    sourcemap: true,
    reportCompressedSize: true,
  },
  server: {
    port: 4173,
    strictPort: true,
    proxy: {
      '/api': {
        target: process.env.PM_API_ORIGIN ?? 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
    },
  },
  preview: {
    port: 4173,
    strictPort: true,
    proxy: {
      '/api': {
        target: process.env.PM_API_ORIGIN ?? 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
    },
  },
  test: {
    environment: 'jsdom',
    include: ['./tests/**/*.test.ts', './tests/**/*.test.tsx'],
    exclude: ['./tests/e2e/**'],
    setupFiles: ['./tests/setup.ts'],
    globals: true,
    css: true,
  },
});
