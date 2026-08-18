import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

const frontendVersion = process.env.PM_FRONTEND_VERSION ?? process.env.npm_package_version ?? 'development';
const frontendRevision = process.env.PM_FRONTEND_REVISION ?? 'not supplied';
const frontendBuildTime = process.env.PM_FRONTEND_BUILD_TIME ?? 'not supplied';
const frontendAssetId = process.env.PM_FRONTEND_ASSET_ID ?? `${frontendVersion}-${frontendRevision.slice(0, 12)}`;

export default defineConfig({
  plugins: [react()],
  define: {
    __PM_BUILD__: JSON.stringify({ version: frontendVersion, revision: frontendRevision, buildTime: frontendBuildTime, assetId: frontendAssetId }),
  },
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
