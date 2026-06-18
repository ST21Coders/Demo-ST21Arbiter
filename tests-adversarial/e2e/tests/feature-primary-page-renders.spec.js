// Positive: each persona's primary page renders real content (features layer).
//
// What this proves
// ----------------
// For each persona we navigate to that persona's documented primary page
// and assert the page-specific heading/marker is visible. This is a focused
// version of pages-per-persona.spec.js — it only exercises ONE page per
// persona (the one the demo flow shows first) so a flaky chart-heavy
// secondary page doesn't mask the "primary page works" signal.
//
// Persona → primary page mapping (from PersonaContext.jsx access order):
//   - CISO     → /dashboard  (Governance Dashboard)
//   - SOC      → /findings   (Conflict Findings)
//   - GRC      → /governance (Governance & Compliance)
//   - Employee → /personas   (Personas & User Flows)
//
// Test ids: features.primary-page-renders.<persona>.

import { test } from '../fixtures.js'
import path from 'node:path'
import fs from 'node:fs'
import { personaIds } from '../lib/manifest.js'
import { PAGE_ASSERTIONS } from '../lib/page-assertions.js'

const RUN_DIR = process.env.RUN_DIR
  || path.resolve(__dirname, '..', '..', 'test-reports', '_local')
const ARTIFACTS_DIR = path.join(RUN_DIR, 'features', 'artifacts')

fs.mkdirSync(ARTIFACTS_DIR, { recursive: true })

// Persona → (manifest page id, route) primary page. Routes match
// ui/src/contexts/PersonaContext.jsx::PERSONAS[*].access[0] and the
// manifest's `pages[*].route` for the same id.
const PRIMARY = {
  ciso: { pageId: 'dashboard', route: '/' },
  soc: { pageId: 'findings', route: '/findings' },
  grc: { pageId: 'governance', route: '/governance' },
  employee: { pageId: 'personas', route: '/personas' },
}

// Hard sanity check at spec-load time: every persona must have a primary
// entry and a matching PAGE_ASSERTIONS row. Catches drift between this spec
// and lib/page-assertions.js at collection time.
{
  const known = new Set(personaIds())
  for (const persona of Object.keys(PRIMARY)) {
    if (!known.has(persona)) {
      throw new Error(
        `feature-primary-page-renders.spec.js: persona '${persona}' is not ` +
        'in the manifest. Update PRIMARY or the manifest.',
      )
    }
    if (!PAGE_ASSERTIONS[PRIMARY[persona].pageId]) {
      throw new Error(
        `feature-primary-page-renders.spec.js: no PAGE_ASSERTIONS entry for ` +
        `page id '${PRIMARY[persona].pageId}'.`,
      )
    }
  }
}

test.describe('features: primary page renders per persona', () => {
  for (const persona of personaIds()) {
    const { pageId, route } = PRIMARY[persona]
    const testId = `features.primary-page-renders.${persona}`
    const pageAssertion = PAGE_ASSERTIONS[pageId]

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
          target_id: pageId,
          persona,
          evidence_path: evidencePath,
        }),
      })

      await page.goto(route)
      await page.waitForLoadState('domcontentloaded')

      // Persona-specific heading / marker. PAGE_ASSERTIONS is the single
      // source of truth for "what does this page's main marker look like";
      // delegating to it means a heading rename only updates one file.
      await pageAssertion.assert(page, persona)

      await page.screenshot({
        path: path.join(ARTIFACTS_DIR, `${testId}.png`),
        fullPage: true,
      })
    })
  }
})
