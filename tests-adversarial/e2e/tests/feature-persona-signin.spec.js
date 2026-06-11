// Positive sign-in flow per persona (features layer, browser side).
//
// What this proves
// ----------------
// For each persona project (ciso, soc, grc, employee), navigate to `/` with
// the storage-state injected by global-setup and assert:
//   1. The URL is NOT /signin and not the Cognito Hosted UI (storage-state
//      injection actually worked).
//   2. The SPA's authenticated shell rendered (nav landmark visible).
//   3. No "Error" page header is showing.
//
// This is the positive twin of the negative-gating spec — every persona
// should be able to land on an in-app route after auth. A FAIL here means
// the storage-state setup broke or the SPA's auth flow regressed.
//
// Test ids: features.signin-flow.<persona> (4 total, one per project).
//
// Why the per-project guard
// -------------------------
// Same shape as pages-per-persona.spec.js: one test() per persona pair, with
// test.skip on the non-matching project so each pair runs exactly once across
// the project matrix. This keeps `--list` output deterministic.

import { test, expect } from '@playwright/test'
import path from 'node:path'
import fs from 'node:fs'
import { personaIds } from '../lib/manifest.js'
import { assertAuthenticatedShellAlive } from '../lib/page-assertions.js'

const RUN_DIR = process.env.RUN_DIR
  || path.resolve(__dirname, '..', '..', 'test-reports', '_local')
const ARTIFACTS_DIR = path.join(RUN_DIR, 'features', 'artifacts')

// Ensure the artifacts dir exists once at spec-load time. mkdirSync is
// idempotent; doing it here keeps test bodies focused on assertions.
fs.mkdirSync(ARTIFACTS_DIR, { recursive: true })

test.describe('features: sign-in flow per persona', () => {
  for (const persona of personaIds()) {
    const testId = `features.signin-flow.${persona}`

    test(testId, async ({ page }, testInfo) => {
      test.skip(
        testInfo.project.name !== persona,
        `${testId} runs under the '${persona}' project only`,
      )

      const evidencePath = `features/artifacts/${testId}.png`
      testInfo.annotations.push({
        type: 'harness-result',
        description: JSON.stringify({
          target_kind: 'page',
          // We anchor this to the signin page id so the coverage matrix
          // shows the auth flow as exercised positively for each persona.
          target_id: 'signin',
          persona,
          evidence_path: evidencePath,
        }),
      })

      // Navigate to the root and let the SPA settle. With storage-state
      // pre-populated, the SignIn route's <Navigate to="/"> redirects us
      // into the persona's first accessible page.
      await page.goto('/')
      await page.waitForLoadState('domcontentloaded')

      // Sanity: we did NOT bounce to the Cognito Hosted UI (storage-state
      // injection worked) and we are NOT sitting on /signin.
      expect(page.url()).not.toContain('amazoncognito.com')
      expect(page.url()).not.toMatch(/\/signin(\?|$|#)/)

      // SPA shell is alive — same assertion the negative-gating spec uses
      // to confirm the authenticated chrome rendered.
      await assertAuthenticatedShellAlive(page)

      await page.screenshot({
        path: path.join(ARTIFACTS_DIR, `${testId}.png`),
        fullPage: true,
      })
    })
  }
})
