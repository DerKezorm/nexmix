import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Where the proxy points. It can be redirected via NEXMIX_API.
const apiTarget = process.env.NEXMIX_API || 'http://127.0.0.1:8620'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    rollupOptions: {
      output: {
        // Libraries change rarely. In their own file, they stay cached in the browser after an update.
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined
          if (/node_modules\/(react|react-dom|react-router|react-router-dom|scheduler|i18next|react-i18next|lucide-react|@fontsource-variable|cookie|set-cookie-parser|html-parse-stringify|void-elements|use-sync-external-store)\//.test(id)) return 'vendor'
          return undefined
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    css: false,
    setupFiles: ['src/test/setup.ts'],
    // The waits in the tests give up with a message of their own after 10 s; this is only the last resort.
    testTimeout: 30_000,
    // The end-to-end tests run in Playwright (`npm run e2e`), not here.
    exclude: ['node_modules/**', 'dist/**', 'e2e/**'],
  },
  server: {
    // Fixed port: if it is taken, Vite aborts instead of silently falling back to another one.
    port: 5622,
    strictPort: true,
    proxy: {
      '/api': { target: apiTarget, changeOrigin: false },
    },
  },
  preview: {
    port: 5622,
    strictPort: true,
  },
})
