/**
 * End to end, headless: the sign-in through a provider (the sign-in tests of the shared module), Plex, Jellyfin and
 * their catalogues. The built interface is served by the real backend, as in the container. Own data folder and own
 * ports (the backend on 28620, the stand-in sign-in provider on 28621, the stand-in Plex on 28622, the stand-in Jellyfin
 * on 28623, the stand-in Music Assistant on 28624), so that a running development server (8620, 5622) is never measured.
 *
 *   npx playwright install chromium     once
 *   npm run e2e                         build, start the backend and the stand-in, test
 */
import { defineConfig } from '@playwright/test'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const PORT = Number(process.env.E2E_PORT ?? 28620)
const PROVIDER_PORT = Number(process.env.E2E_PROVIDER_PORT ?? 28621)
const PLEX_PORT = Number(process.env.E2E_PLEX_PORT ?? 28622)
const JELLYFIN_PORT = Number(process.env.E2E_JELLYFIN_PORT ?? 28623)
const MA_PORT = Number(process.env.E2E_MA_PORT ?? 28624)

/** A fresh data folder per run. Made here and not in a global set-up: the server starts first. The workers load this
 * file again and inherit the folder through the environment. */
function dataDir(): string {
  if (process.env.E2E_DATA_DIR) return process.env.E2E_DATA_DIR
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'nexmix-e2e-'))
  process.env.E2E_DATA_DIR = dir
  return dir
}

const DATA_DIR = dataDir()
/** Where the operator's session is kept for the tests (global-setup.ts signs in once). */
export const SIGNED_IN = path.join(DATA_DIR, 'e2e-operator.json')
process.env.E2E_STATE = SIGNED_IN
process.env.E2E_PROVIDER = `http://127.0.0.1:${PROVIDER_PORT}`
process.env.E2E_PLEX = `http://127.0.0.1:${PLEX_PORT}`
process.env.E2E_JELLYFIN = `http://127.0.0.1:${JELLYFIN_PORT}`
process.env.E2E_MA = `http://127.0.0.1:${MA_PORT}`
// The project's venv on the development machines, the system Python elsewhere.
const venv = path.join('.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
const python = fs.existsSync(path.join('..', 'backend', venv)) ? venv : 'python'

export default defineConfig({
  testDir: './e2e',
  globalSetup: './e2e/global-setup.ts',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: 'list',
  // Every test in Chromium; the playlists, the suggestions, the guests, Jellyfin, the mirrors and Music Assistant also in
  // Firefox and WebKit.
  projects: [
    { name: 'chromium', use: { browserName: 'chromium' } },
    { name: 'firefox', use: { browserName: 'firefox' }, testMatch: /(playlists|suggestions|guests|jellyfin|mirrors|music_assistant).spec.ts/ },
    { name: 'webkit', use: { browserName: 'webkit' }, testMatch: /(playlists|suggestions|guests|jellyfin|mirrors|music_assistant).spec.ts/ },
  ],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    headless: true,
    locale: 'en-US',
    storageState: SIGNED_IN,
  },
  webServer: [
    {
      command: `${python} -m uvicorn app.main:app --host 127.0.0.1 --port ${PORT} --no-proxy-headers --no-server-header`,
      cwd: path.join('..', 'backend'),
      url: `http://127.0.0.1:${PORT}/api/v1/health`,
      reuseExistingServer: false,
      timeout: 90_000,
      env: {
        NEXMIX_DATA_DIR: DATA_DIR,
        NEXMIX_FRONTEND_DIST: path.resolve('dist'),
        // The background runs: the catalogue is fetched by its job, as in the container.
        NEXMIX_DISABLE_BACKGROUND: '0',
        // plex.tv and Plex's sign-in page are the stand-in (plex.spec.ts); nothing reaches the real ones.
        NEXMIX_PLEX_TV_URL: `http://127.0.0.1:${PLEX_PORT}`,
        NEXMIX_PLEX_AUTH_URL: `http://127.0.0.1:${PLEX_PORT}/auth`,
        NEXMIX_ARGON2_TIME: '1',
        NEXMIX_ARGON2_MEMORY_KIB: '8192',
        NEXMIX_ARGON2_PARALLELISM: '1',
        // Never GitHub from a test: port 9 refuses at once.
        NEXMIX_UPDATE_URL: 'http://127.0.0.1:9/releases/latest',
        // The code the first account brings; global-setup.ts sends it.
        NEXMIX_SETUP_TOKEN: 'e2e-setup-code',
        NEXMIX_COOKIE_SUFFIX: '_e2e',
      },
    },
    // A stand-in plex.tv and Plex Media Server with a made-up library (backend/tests/fakeplex).
    {
      command: `${python} -m tests.fakeplex.serve --port ${PLEX_PORT}`,
      cwd: path.join('..', 'backend'),
      url: `http://127.0.0.1:${PLEX_PORT}/health`,
      reuseExistingServer: false,
      timeout: 30_000,
    },
    // A stand-in Jellyfin with a made-up library (backend/tests/fakejellyfin): Quick Connect, catalogue, covers, sound.
    {
      command: `${python} -m tests.fakejellyfin.serve --port ${JELLYFIN_PORT}`,
      cwd: path.join('..', 'backend'),
      url: `http://127.0.0.1:${JELLYFIN_PORT}/health`,
      reuseExistingServer: false,
      timeout: 30_000,
    },
    // A stand-in Music Assistant with a made-up library behind a real websocket (backend/tests/fakema).
    {
      command: `${python} -m tests.fakema.serve --port ${MA_PORT}`,
      cwd: path.join('..', 'backend'),
      url: `http://127.0.0.1:${MA_PORT}/health`,
      reuseExistingServer: false,
      timeout: 30_000,
    },
    // A stand-in sign-in provider (nexoidc.spec.ts): signs in at once whoever POST /oidc/next named.
    {
      command: 'node e2e/fake-provider.mjs',
      url: `http://127.0.0.1:${PROVIDER_PORT}/health`,
      reuseExistingServer: false,
      timeout: 20_000,
      env: { FAKE_PROVIDER_PORT: String(PROVIDER_PORT) },
    },
  ],
})
