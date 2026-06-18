// Task 8 acceptance smoke test.
//
// Confirms the storageState-injection path works end to end:
//   1. globalSetup wrote a valid ciso.json (otherwise the project's
//      `storageState` load would have thrown before this point).
//   2. The injected `arbiter.tokens` sessionStorage blob is recognised by the
//      SPA's `useAuth.js::isAuthenticated()` (otherwise the SPA would have
//      redirected to /signin or the Cognito Hosted UI).
//   3. The CISO persona has access to `/governance` (per PERSONAS access in
//      ui/src/contexts/PersonaContext.jsx) so the page renders rather than
//      showing <AccessDenied />.
//
// Scoped to the `ciso` project only — running it across all 4 personas would
// duplicate the per-persona coverage that task 9 implements. The `no-auth`
// project is not selected here because there's no storageState to verify.

import { test, expect } from '../fixtures.js'

test.describe.configure({ mode: 'parallel' })

test('CISO storage-state loads and /governance renders @smoke', async ({ page }, testInfo) => {
  // Only run under the `ciso` project. The other persona projects are
  // covered by task 9's pages-per-persona spec.
  test.skip(testInfo.project.name !== 'ciso', 'task-8 smoke is ciso-only')

  // Direct navigation to /governance. If the storage-state injection didn't
  // take, useAuth.js::isAuthenticated() returns false and the SPA redirects
  // to /signin (which then bounces to the Cognito Hosted UI). Asserting the
  // URL is the cleanest way to detect that failure mode.
  await page.goto('/governance')

  // Give the SPA a moment to evaluate auth + route. networkidle is the
  // canonical Playwright wait for SPA boot completion; the SPA fires no
  // long-poll requests so it settles quickly.
  await page.waitForLoadState('networkidle')

  // 1. We did not get bounced to /signin.
  expect(page.url()).not.toContain('/signin')
  // 2. We did not get bounced to the Cognito Hosted UI either.
  expect(page.url()).not.toContain('/login')
  expect(page.url()).not.toContain('amazoncognito.com')

  // 3. The Governance page header rendered. Source: ui/src/pages/Governance.jsx
  // — "Governance & Compliance" is the <h1> in the page header.
  await expect(page.getByText('Governance & Compliance')).toBeVisible({ timeout: 10_000 })
})
