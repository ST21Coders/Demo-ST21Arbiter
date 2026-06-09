// Playwright config for the deployed-env E2E layer (task 8).
//
// Targets the deployed dev CloudFront (no `webServer` block — there is no
// local Vite to start, unlike tests-e2e/playwright.config.ts). Reads the
// target URL from TARGET_BASE_URL with the spec §8 default.
//
// Authentication
// --------------
// The four persona projects (`ciso`, `soc`, `grc`, `employee`) each load a
// pre-populated storageState JSON file from `e2e/storage-states/<persona>.json`.
// Those files are written by `e2e/global-setup.js` at run start; it shells
// out to `python3.13 -m scripts.emit_storage_states` which calls
// `src.identity.cognito_auth.fetch_all()` to obtain real Cognito IdTokens
// and serialises them into Playwright's storageState shape (with the
// SPA-specific `sessionStorage[arbiter.tokens]` blob — see useAuth.js).
//
// A fifth `no-auth` project starts with a blank storageState and is used by
// the Cognito Hosted UI spec at task 11 (the only test that needs to exercise
// the real OAuth flow rather than the injected-token shortcut).
//
// Reports
// -------
// When RUN_DIR is set (the orchestrator at task 25 sets it to the per-run
// directory under test-reports/<ts>/) reports land under ${RUN_DIR}/e2e/.
// Otherwise they land under test-reports/_local/e2e/ so a standalone
// `npm run test:e2e` invocation still produces a self-contained subreport.
//
// Module system: CommonJS. The harness's package.json doesn't set
// `"type": "module"` so .js files default to CJS — and using `require()` here
// keeps the config loadable by Playwright's loader without extra config.

const { defineConfig, devices } = require('@playwright/test')
const path = require('node:path')

const DEFAULT_BASE_URL = 'https://d5u0vv1zl3eqd.cloudfront.net/'
const BASE_URL = process.env.TARGET_BASE_URL || DEFAULT_BASE_URL

// Report root. When running standalone the orchestrator hasn't set RUN_DIR,
// so we drop into a `_local` bucket alongside per-run timestamps.
const RUN_DIR = process.env.RUN_DIR || path.resolve(__dirname, '..', 'test-reports', '_local')
const E2E_REPORT_DIR = path.join(RUN_DIR, 'e2e')

const STORAGE_DIR = path.resolve(__dirname, 'storage-states')

module.exports = defineConfig({
  testDir: './tests',
  // Each persona project loads a different storageState; running them in
  // parallel is safe (they target the deployed env, not a local server).
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  // Output dir for traces/screenshots/videos. Playwright nests per-test
  // subdirectories under this automatically.
  outputDir: path.join(E2E_REPORT_DIR, 'artifacts'),
  reporter: [
    ['list'],
    // Custom harness reporter writes the flat `results.json` the Python
    // coverage builder (`src/coverage/builder.py::load_results`) consumes.
    // See e2e/reporters/results-reporter.js for the row shape + annotation
    // contract specs use to opt rows into this file.
    [
      path.resolve(__dirname, 'reporters', 'results-reporter.js'),
      { outputFile: path.join(E2E_REPORT_DIR, 'results.json') },
    ],
    ['json', { outputFile: path.join(E2E_REPORT_DIR, 'playwright-results.json') }],
    ['html', { outputFolder: path.join(E2E_REPORT_DIR, 'playwright-report'), open: 'never' }],
  ],
  // globalSetup runs once before any test and synchronously emits the four
  // storageState files. If DEMO_PASSWORD is unset or Cognito InitiateAuth
  // fails, it throws — Playwright surfaces "globalSetup failed" with the
  // underlying message and skips the entire run.
  globalSetup: path.resolve(__dirname, 'global-setup.js'),
  use: {
    baseURL: BASE_URL,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [
    {
      name: 'ciso',
      use: {
        ...devices['Desktop Chrome'],
        storageState: path.join(STORAGE_DIR, 'ciso.json'),
      },
    },
    {
      name: 'soc',
      use: {
        ...devices['Desktop Chrome'],
        storageState: path.join(STORAGE_DIR, 'soc.json'),
      },
    },
    {
      name: 'grc',
      use: {
        ...devices['Desktop Chrome'],
        storageState: path.join(STORAGE_DIR, 'grc.json'),
      },
    },
    {
      name: 'employee',
      use: {
        ...devices['Desktop Chrome'],
        storageState: path.join(STORAGE_DIR, 'employee.json'),
      },
    },
    {
      name: 'no-auth',
      // No storageState — every browser context starts blank. The task-11
      // cognito-hosted-ui.spec.js opts into this project explicitly via
      // `test.describe.configure({ project: 'no-auth' })` (or by being the
      // only spec the project's testMatch selects).
      use: { ...devices['Desktop Chrome'] },
    },
  ],
})
