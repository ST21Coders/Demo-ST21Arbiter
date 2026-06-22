// Shared Playwright test fixture for the deployed-env E2E layer.
//
// Playwright 1.60.0's storageState restore explicitly clears
// `sessionStorage` and only repopulates `localStorage`. The SPA's
// `useAuth.js` reads its Cognito tokens from `sessionStorage[arbiter.tokens]`
// (production-correct: tab-scoped sessions, not cross-tab). Because of that
// mismatch, the per-persona storage-state JSONs written by
// `scripts/emit_storage_states.py` contain the token blob under
// `origins[0].sessionStorage[]`, but Playwright drops it when it loads the
// context. Every authenticated test then sees `isAuthenticated() === false`
// and bounces to `/signin`.
//
// This fixture re-injects the sessionStorage entries via
// `context.addInitScript()` so they land in the page before any SPA code
// runs. The script fires on every navigation, which is what we want.
//
// Usage: every spec imports `test` and `expect` from here instead of
// directly from `@playwright/test`. The `no-auth` project receives an empty
// entries list and skips the seeding.
//
// Module system: CommonJS.

const { test: base, expect } = require('@playwright/test')
const fs = require('node:fs')
const path = require('node:path')

const STORAGE_DIR = path.resolve(__dirname, 'storage-states')

function loadSessionStorageEntries(projectName) {
  const file = path.join(STORAGE_DIR, `${projectName}.json`)
  if (!fs.existsSync(file)) return []
  try {
    const state = JSON.parse(fs.readFileSync(file, 'utf-8'))
    const origin = (state.origins || [])[0] || {}
    return Array.isArray(origin.sessionStorage) ? origin.sessionStorage : []
  } catch {
    return []
  }
}

const test = base.extend({
  context: async ({ context }, use, testInfo) => {
    const entries = loadSessionStorageEntries(testInfo.project.name)
    if (entries.length > 0) {
      await context.addInitScript((items) => {
        for (const { name, value } of items) {
          try { window.sessionStorage.setItem(name, value) } catch { /* ignore */ }
        }
      }, entries)
    }
    await use(context)
  },
})

module.exports = { test, expect }
