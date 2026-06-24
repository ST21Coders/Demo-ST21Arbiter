// Cognito Hosted UI smoke + unauthenticated-gating coverage (task 11a).
//
// What this proves
// ----------------
// This spec runs under the `no-auth` Playwright project — every browser
// context starts with a blank storageState (no `arbiter.tokens` in
// sessionStorage), so the SPA's `useAuth.js::isAuthenticated()` returns
// false and the SPA's `<RequireAuth>` wrapper redirects every guarded
// route to `/signin`.
//
// We exercise three independent surfaces in this layer, intentionally
// scoped to "the gate is alive" rather than "a full Hosted UI login
// happens." A full login would have to type real credentials into the
// Cognito Hosted UI form; the task prompt explicitly says we don't put
// credentials in code, so we stop at "the redirect fires" + "the Hosted
// UI is reachable."
//
// 1. `e2e.signin.hosted-ui-redirects-to-cognito`
//    Navigates directly to /signin. ARBITER's SignIn.jsx renders a LOCAL
//    page (the "Sign in with Cognito" button) — it does NOT auto-redirect
//    to the Hosted UI on mount. The button click is what kicks off the
//    Hosted UI redirect (signIn() in useAuth.js sets window.location).
//    We assert the local page rendered and the button is visible — the
//    actual button click would navigate off-domain to the Cognito Hosted
//    UI, and we don't follow it (see header above).
//
// 2. `e2e.signin.unauthenticated-redirect-from-protected-route`
//    Navigates to /dashboard with no storageState. The SPA's RequireAuth
//    wrapper at ui/src/App.jsx::69 returns `<Navigate to="/signin">` when
//    !isAuthenticated(). We assert the URL ends up on /signin — proving
//    the gate gates.
//
// 3. `e2e.signin.cognito-domain-reachable`
//    Smoke check that the Cognito Hosted UI domain itself is reachable.
//    Uses `request.get()` from the Playwright fixture (a bare HTTP client,
//    not a browser context), follows no redirects, and accepts 200, 302,
//    or 400 (the Hosted UI's normal "no params, redirect to login form"
//    or "missing client_id" shapes — all prove the server is alive).
//    The domain is resolved via env override (preferred) or by fetching
//    the deployed SPA's bundle and parsing the `VITE_COGNITO_DOMAIN`
//    value baked into the compiled JS. If neither path resolves, the
//    test FAILS with a clear message (per W1 reviewer feedback) — a
//    self-skipping smoke is worse than a missing one.
//
// Project scoping
// ---------------
// All three tests scope themselves to the `no-auth` project via
// `test.skip(testInfo.project.name !== 'no-auth')`. The 4 persona projects'
// project enumerations get skipped at runtime.
//
// Coverage-matrix participation
// -----------------------------
// These tests intentionally do NOT emit `harness-result` annotations.
// Earlier drafts of this spec did, with `target_kind: 'page'`,
// `target_id: 'signin'`, `persona: null` — but the harness's
// `_validate_result` (`src/coverage/builder.py`) requires every page-
// targeted result to carry a persona, so the rows would have raised
// `MissingPersonaError` and aborted the entire `load_results →
// build_matrix` step. The `signin` page is already covered by the
// positive `pages-per-persona.spec.js` for each of the four personas
// (those rows DO carry a persona), so dropping the annotations here
// loses no coverage signal. These tests still run as real Playwright
// tests — they appear in the built-in Playwright HTML/JSON reporters,
// fail loudly on regression, and capture screenshots/artifacts. They
// just don't contribute rows to the coverage matrix.
//
// Module system: ESM-style imports to match the other spec files
// (pages-per-persona.spec.js, negative-gating.spec.js).

import { test, expect } from '../fixtures.js'
import { request as playwrightRequest } from '@playwright/test'
import path from 'node:path'
import fs from 'node:fs'

const RUN_DIR = process.env.RUN_DIR
  || path.resolve(__dirname, '..', '..', 'test-reports', '_local')
const ARTIFACTS_DIR = path.join(RUN_DIR, 'e2e', 'artifacts')

fs.mkdirSync(ARTIFACTS_DIR, { recursive: true })

// Resolve the Cognito Hosted UI domain at test runtime.
//
// The SPA reads it from `VITE_COGNITO_DOMAIN` at build time and exposes it
// via the `COGNITO.domain` constant in `ui/src/config.js` (line 23). The
// deployed bundle's compiled JS therefore contains the literal domain
// string. We try, in order:
//   1. `COGNITO_DOMAIN` env var — the canonical operator-pinned override
//      (documented in `.env.example`). Takes any of:
//        dev-arbiter.auth.us-east-1.amazoncognito.com
//        https://dev-arbiter.auth.us-east-1.amazoncognito.com
//        https://dev-arbiter.auth.us-east-1.amazoncognito.com/
//      Stripped to bare hostname.
//   2. `TARGET_COGNITO_DOMAIN` env var — accepted for backwards-compat
//      with earlier drafts of this spec.
//   3. Fetch the deployed `/index.html` from TARGET_BASE_URL, pull the
//      bundled JS asset references, fetch each, and grep for an
//      `*.auth.<region>.amazoncognito.com` literal (Vite inlines
//      `import.meta.env.VITE_COGNITO_DOMAIN` as a string literal at
//      build time — see ui/src/config.js line 23).
//
// If all three fail, the test FAILS with a clear message naming what was
// tried — per W1 reviewer feedback, a self-skipping smoke is worse than
// a missing one because it gives a false-green signal in CI.
function envCognitoDomain() {
  const raw = process.env.COGNITO_DOMAIN || process.env.TARGET_COGNITO_DOMAIN
  if (!raw) return null
  // Strip any scheme/path the operator may have pasted in.
  return raw.replace(/^https?:\/\//, '').replace(/\/$/, '')
}

// Vite inlines `import.meta.env.VITE_COGNITO_DOMAIN` as a string literal
// at build time. The compiled bundle therefore contains a literal like
// `"dev-arbiter.auth.us-east-1.amazoncognito.com"`. We fetch the SPA's
// index.html, extract every `/assets/*.js` reference, fetch each, and
// regex for the Cognito-domain shape.
async function scrapeCognitoDomainFromBundle(requestContext, baseURL) {
  // 1. Fetch index.html.
  let indexResp
  try {
    indexResp = await requestContext.get(baseURL, {
      timeout: 10_000,
      failOnStatusCode: false,
    })
  } catch {
    return null
  }
  if (!indexResp.ok()) return null
  const indexHtml = await indexResp.text()

  // 2. Pull every `/assets/*.js` URL from script src attributes.
  const assetMatches = [...indexHtml.matchAll(/src="([^"]*\.js)"/g)]
  const assetPaths = assetMatches.map((m) => m[1])
  if (assetPaths.length === 0) return null

  // 3. Fetch each asset (cap at 6 to keep the smoke fast) and grep the
  //    body for the Cognito-domain shape. The match is intentionally
  //    permissive on the subdomain segment (the prefix is whatever the
  //    operator picked when creating the user pool domain — e.g.
  //    "dev-arbiter") and strict on the suffix `.auth.<region>.amazoncognito.com`.
  const COGNITO_DOMAIN_RE = /([a-z0-9-]+\.auth\.[a-z0-9-]+\.amazoncognito\.com)/i
  for (const assetPath of assetPaths.slice(0, 6)) {
    const url = new URL(assetPath, baseURL).toString()
    let assetResp
    try {
      assetResp = await requestContext.get(url, {
        timeout: 10_000,
        failOnStatusCode: false,
      })
    } catch {
      continue
    }
    if (!assetResp.ok()) continue
    const body = await assetResp.text()
    const m = body.match(COGNITO_DOMAIN_RE)
    if (m) return m[1].toLowerCase()
  }
  return null
}

test.describe('Cognito Hosted UI gate', () => {
  test('e2e.signin.hosted-ui-redirects-to-cognito', async ({ page }, testInfo) => {
    test.skip(
      testInfo.project.name !== 'no-auth',
      'no-auth project only (blank storageState required)',
    )

    const testId = 'e2e.signin.hosted-ui-redirects-to-cognito'
    // No harness-result annotation: see header comment ("Coverage-matrix
    // participation"). This test runs under Playwright's built-in
    // reporters and contributes nothing to the coverage matrix.

    try {
      // Direct navigation to /signin with NO storage-state. SignIn.jsx's
      // first guard is `if (isAuthenticated()) return <Navigate to='/' />`.
      // Without a token, that returns false → the local SignIn page renders.
      await page.goto('/signin')
      await page.waitForLoadState('domcontentloaded')

      // ARBITER's behavior (per ui/src/pages/SignIn.jsx, verified at task
      // 11 build time): renders a LOCAL page with a "Sign in with Cognito"
      // button. Does NOT auto-redirect on mount. So we assert the local
      // button is visible. If a future refactor flips to auto-redirect,
      // this assertion fails — which is the right signal (test author
      // should re-read SignIn.jsx and update accordingly).
      await expect(
        page.getByRole('button', { name: /sign in with cognito/i }),
      ).toBeVisible({ timeout: 10_000 })

      // Belt-and-suspenders: the URL is still /signin (no auto-redirect).
      expect(page.url()).toContain('/signin')
      // We did NOT bounce to amazoncognito.com (which would imply
      // auto-redirect was wired).
      expect(page.url()).not.toContain('amazoncognito.com')
    } finally {
      await page.screenshot({
        path: path.join(ARTIFACTS_DIR, `${testId}.png`),
        fullPage: true,
      }).catch(() => {})
    }
  })

  test('e2e.signin.unauthenticated-redirect-from-protected-route', async ({ page }, testInfo) => {
    test.skip(
      testInfo.project.name !== 'no-auth',
      'no-auth project only (blank storageState required)',
    )

    const testId = 'e2e.signin.unauthenticated-redirect-from-protected-route'
    // No harness-result annotation: see header comment.

    try {
      // Navigate to a guarded route. /dashboard is wrapped in <RequireAuth>
      // via the catch-all `path="/*"` route in App.jsx (line 191-197). With
      // no token, RequireAuth returns `<Navigate to="/signin" replace />`.
      await page.goto('/dashboard')
      await page.waitForLoadState('domcontentloaded')

      // Poll for the URL to settle on /signin. We give the redirect up to
      // 5 seconds (well above what a client-side Navigate takes).
      await expect.poll(
        () => {
          try {
            return new URL(page.url()).pathname
          } catch {
            return page.url()
          }
        },
        {
          message: 'expected unauthenticated /dashboard to redirect to /signin',
          timeout: 5_000,
          intervals: [100, 250, 500],
        },
      ).toBe('/signin')
    } finally {
      await page.screenshot({
        path: path.join(ARTIFACTS_DIR, `${testId}.png`),
        fullPage: true,
      }).catch(() => {})
    }
  })

  test('e2e.signin.cognito-domain-reachable', async ({ page }, testInfo) => {
    test.skip(
      testInfo.project.name !== 'no-auth',
      'no-auth project only (blank storageState required)',
    )

    const testId = 'e2e.signin.cognito-domain-reachable'
    // No harness-result annotation: see header comment.

    // Resolve the Cognito Hosted UI domain. Order:
    //   1. COGNITO_DOMAIN / TARGET_COGNITO_DOMAIN env override
    //      (operator-pinned, see .env.example).
    //   2. Scrape the deployed SPA's bundled JS (VITE_COGNITO_DOMAIN is
    //      inlined as a string literal at Vite build time).
    let domain = envCognitoDomain()
    let resolutionSource = 'env'

    // Build a bare HTTP request context for both the scrape (if needed)
    // and the reachability check. Reusing one context keeps connections
    // warm and gives the scrape + check identical timeouts.
    const requestContext = await playwrightRequest.newContext()
    try {
      if (!domain) {
        const baseURL = testInfo.project.use.baseURL
        if (baseURL) {
          domain = await scrapeCognitoDomainFromBundle(requestContext, baseURL)
          resolutionSource = 'bundle-scrape'
        }
        void page // page fixture unused on the fallback path
      }

      // FAIL (not skip) if we still don't have a domain. A self-skipping
      // smoke gives a false-green signal in CI — per W1 reviewer feedback.
      if (!domain) {
        throw new Error(
          'could not resolve Cognito Hosted UI domain. Tried in order: ' +
          '(1) COGNITO_DOMAIN env var, (2) TARGET_COGNITO_DOMAIN env var, ' +
          '(3) scraping VITE_COGNITO_DOMAIN from the deployed SPA bundle at ' +
          `TARGET_BASE_URL. Set COGNITO_DOMAIN in tests-adversarial/.env ` +
          '(see .env.example for the lookup — value lives in ' +
          'ui/src/config.js::COGNITO.domain at build time).',
        )
      }

      // /login is the Hosted UI's main page. A bare GET with no params
      // typically yields 400 (missing client_id) — but the SERVER is up,
      // which is all this smoke is checking. We accept 200, 302, AND 400
      // as "domain reachable" — anything else (DNS failure, 5xx, timeout)
      // is a FAIL. (Reviewer W2 explicitly OK'd this accept window.)
      const resp = await requestContext.get(`https://${domain}/login`, {
        maxRedirects: 0,
        timeout: 10_000,
        failOnStatusCode: false,
      })

      const status = resp.status()
      // Write evidence file: the response status + headers, JSON.
      const evidence = {
        domain,
        resolution_source: resolutionSource,
        status,
        headers: resp.headers(),
      }
      fs.writeFileSync(
        path.join(ARTIFACTS_DIR, `${testId}.json`),
        JSON.stringify(evidence, null, 2) + '\n',
        'utf-8',
      )

      // The Hosted UI is alive if it responds with 2xx/3xx OR a 400
      // (parameter-validation rejection, which still proves the server is
      // serving). Anything else fails.
      const reachable = (status >= 200 && status < 400) || status === 400
      expect(
        reachable,
        `Cognito domain ${domain} returned HTTP ${status} on GET /login; ` +
        `expected 200, 302, or 400 (alive).`,
      ).toBe(true)
    } finally {
      await requestContext.dispose()
    }
  })
})
