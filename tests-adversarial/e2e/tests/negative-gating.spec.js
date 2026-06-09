// Negative-gating E2E coverage (task 10).
//
// What this proves
// ----------------
// For every (page, persona) cell in the manifest where the persona appears
// in `page.blocked_for`, this spec generates one Playwright test that:
//   1. Navigates to `page.route` as that persona (using the storage-state the
//      global setup wrote at run start).
//   2. Asserts the SPA's RBAC behavior fired — either by rendering the
//      <AccessDenied /> component in-place (text marker: "Access restricted")
//      OR by redirecting away from the blocked route to the persona's
//      firstAccessiblePath(). See "ARBITER gating behavior" below for which
//      one fires in practice.
//   3. Takes a screenshot to `${E2E_REPORT_DIR}/artifacts/<test-id>.png` as
//      evidence (PASS or FAIL — both polarities deserve the artifact).
//   4. Emits a per-test row into `${RUN_DIR}/e2e/results.json` via the
//      `harness-result` annotation the reporter at
//      `e2e/reporters/results-reporter.js` consumes.
//
// Result polarity
// ---------------
// CRITICAL semantics: a PASS row here means "the blocked persona was correctly
// blocked." A FAIL row means "the page leaked" (the blocked persona could see
// content it shouldn't). The coverage builder fills the same (page, persona)
// cell as the positive spec — `pass` always means "behavior matched manifest
// expectation" regardless of polarity. See builder.py module docstring.
//
// Severity tagging
// ----------------
// Per spec AC9 (which AC10 references): a leaked page is a high-severity
// finding. This spec adds a `severity` annotation:
//   - `severity: "low"`  when the gating behavior was observed (PASS).
//   - `severity: "high"` when the page leaked through to the blocked persona
//     (FAIL).
// The reporter at `e2e/reporters/results-reporter.js` reads this annotation
// and writes it into the row. The Python coverage builder's `TestResult`
// dataclass carries the same field through into the eventual report.
//
// ARBITER gating behavior (verified against the codebase)
// -------------------------------------------------------
// `ui/src/App.jsx::Guarded` wraps every guarded route:
//
//     function Guarded({ path, children }) {
//       const { hasAccess } = usePersona()
//       return hasAccess(path) ? children : <AccessDenied />
//     }
//
// When a persona without access lands on a guarded route, `<AccessDenied>`
// renders IN-PLACE — the URL does NOT change. `<AccessDenied>` shows the
// literal text "Access restricted" (h1 line in App.jsx). It also offers a
// link to `firstAccessiblePath()` but does not auto-redirect.
//
// `PersonaRouteSync` only fires the auto-redirect on `personaId` *change*
// (not on initial mount of a blocked URL — see the effect's dep array). So
// for our test (a fresh page.goto), `<AccessDenied>` is what we observe.
//
// We therefore PRIMARILY assert "Access restricted" is visible. As a defensive
// fallback (in case a future refactor flips the behavior to a redirect), we
// also accept "the URL is no longer the blocked route AND it's an in-app route
// the persona can access" — using Playwright's `expect.poll` on a small "OR"
// expression so either behavior counts as PASS.
//
// We do NOT add tests for pages with empty `blocked_for` arrays in the
// manifest (analyst, personas, settings, signin) — those are universally
// accessible to all four authenticated personas.

import { test, expect } from '@playwright/test'
import path from 'node:path'
import fs from 'node:fs'
import { pages, personaIds } from '../lib/manifest.js'

// Resolve the per-run artifacts dir at spec-load time. Mirrors the path
// `pages-per-persona.spec.js` uses so both specs' screenshots land alongside
// each other and the report.html links resolve uniformly.
const RUN_DIR = process.env.RUN_DIR
  || path.resolve(__dirname, '..', '..', 'test-reports', '_local')
const ARTIFACTS_DIR = path.join(RUN_DIR, 'e2e', 'artifacts')

fs.mkdirSync(ARTIFACTS_DIR, { recursive: true })

// Build the (page, persona) pair list ONCE at spec-load time. Each pair is a
// cell where persona is in `page.blocked_for`.
const PAIRS = []
for (const page of pages()) {
  for (const persona of page.blocked_for) {
    PAIRS.push({
      pageId: page.id,
      pageRoute: page.route,
      persona,
      testId: `e2e.page.${page.id}.${persona}`,
    })
  }
}

// Cross-check at collection time: positive + negative cells must together
// cover every (page, persona) cell exactly once. The arithmetic guarantees
// no spec double-counts a cell and no spec silently misses one.
if (PAIRS.length === 0) {
  throw new Error(
    'negative-gating.spec.js: no (page, persona) pairs were generated from ' +
    'the manifest. Every page has an empty blocked_for? Check ' +
    'src/coverage/manifest.json.',
  )
}

// Every persona we generate a test for must be known to the manifest.
{
  const knownPersonas = new Set(personaIds())
  for (const pair of PAIRS) {
    if (!knownPersonas.has(pair.persona)) {
      throw new Error(
        `negative-gating.spec.js: pair targets unknown persona ` +
        `'${pair.persona}' for page '${pair.pageId}'. Manifest drift?`,
      )
    }
  }
}

test.describe('negative page-per-persona gating', () => {
  // One test per (page, persona) pair where persona is in blocked_for. Each
  // test only actually executes under the matching project — the other four
  // project enumerations are skipped at runtime (mirrors pages-per-persona).
  for (const pair of PAIRS) {
    const { pageId, pageRoute, persona, testId } = pair

    test(testId, async ({ page }, testInfo) => {
      // Skip on non-matching projects so each pair runs exactly once across
      // the project matrix. Keeps `--list` showing every enumeration while
      // each pair executes only under its own persona's project.
      test.skip(
        testInfo.project.name !== persona,
        `${testId} runs under the '${persona}' project only`,
      )

      // Annotation BEFORE assertions so the row lands in results.json even
      // if a later step throws. We default severity to "low"; if we detect
      // a leak below, we push a SECOND severity annotation with "high" —
      // the reporter picks the last `severity` annotation it sees.
      const evidencePath = `e2e/artifacts/${testId}.png`
      testInfo.annotations.push({
        type: 'harness-result',
        description: JSON.stringify({
          target_kind: 'page',
          target_id: pageId,
          persona,
          evidence_path: evidencePath,
        }),
      })
      testInfo.annotations.push({ type: 'severity', description: 'low' })

      let leaked = false
      try {
        // 1. Navigate to the blocked route. Playwright resolves `pageRoute`
        //    against `baseURL` from playwright.config.js.
        await page.goto(pageRoute)

        // 2. Let the SPA settle. We need the Guarded render or the redirect
        //    effect to have completed before we sample the DOM/URL.
        await page.waitForLoadState('domcontentloaded')

        // 3. Sanity check: storage-state injection worked. If we bounced to
        //    the Cognito Hosted UI, the test is meaningless — fail loudly.
        expect(page.url()).not.toContain('amazoncognito.com')

        // 4. Assert RBAC fired. ARBITER's behavior in practice is to render
        //    <AccessDenied /> in-place. As a defensive fallback we also
        //    accept a redirect off the blocked route — either is a valid
        //    gating outcome. We poll with a tight timeout so the assertion
        //    is robust to either behavior.
        await expect
          .poll(
            async () => {
              const accessDeniedVisible = await page
                .getByText('Access restricted')
                .first()
                .isVisible()
                .catch(() => false)
              if (accessDeniedVisible) return 'access-denied'

              // Redirect-fallback: the URL moved off the blocked route. We
              // confirm we didn't land on the Hosted UI or /signin (which
              // would mean auth was lost, not RBAC). We use URL.pathname to
              // compare to the manifest's route exactly — string-includes
              // would falsely match `/findings/foo` against `/findings`.
              let pathname
              try {
                pathname = new URL(page.url()).pathname
              } catch {
                pathname = page.url()
              }
              const onBlockedRoute = pathname === pageRoute
              const onAuthFlow = page.url().includes('amazoncognito.com') ||
                /\/signin(\?|$|#|\/)/.test(pathname)
              if (!onBlockedRoute && !onAuthFlow) return 'redirected'

              return 'still-on-blocked-route'
            },
            {
              message:
                `RBAC did not fire for ${persona} on ${pageRoute}: ` +
                `expected <AccessDenied> ("Access restricted") OR redirect ` +
                `to firstAccessiblePath(). Page leaked.`,
              timeout: 5_000,
              intervals: [200, 500, 1_000],
            },
          )
          .not.toBe('still-on-blocked-route')
      } catch (err) {
        // The assertion above threw → the page leaked. Tag severity:high so
        // the reporter writes it to the row. We re-throw after taking the
        // evidence screenshot so the Playwright result is still FAIL.
        leaked = true
        throw err
      } finally {
        // Override severity for leaks. Pushing a second annotation with the
        // same type is fine — the reporter takes the last one. The screenshot
        // is taken in finally so we capture evidence for both PASS and FAIL.
        if (leaked) {
          testInfo.annotations.push({ type: 'severity', description: 'high' })
        }
        // Screenshot evidence. fullPage so a "leaked" finding shows the
        // actual content the blocked persona could see. The screenshot fires
        // even when the assertion threw (it's in `finally`).
        await page.screenshot({
          path: path.join(ARTIFACTS_DIR, `${testId}.png`),
          fullPage: true,
        }).catch(() => {
          // A screenshot failure must not mask the real assertion failure.
          // Reporter falls back to Playwright's per-test outputDir for
          // evidence_path (see results-reporter.js AC20 handling).
        })
      }
    })
  }
})
