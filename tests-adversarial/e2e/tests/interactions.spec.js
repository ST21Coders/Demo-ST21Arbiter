// Per-page primary-interaction E2E coverage (task 11c, deferred from task 9).
//
// What this proves
// ----------------
// For each CISO-accessible page in the manifest, this spec runs ONE primary
// interaction (defined in `e2e/fixtures/interactions.json`) and asserts:
//   - the interaction's expected_signal fires (a URL change, a new visible
//     element, or simply no uncaught console error), AND
//   - no console error fired during the interaction.
//
// Why CISO-only
// -------------
// CISO has the broadest access of the four personas (universally accessible
// to all pages plus the CISO-only pages — see the manifest). Running each
// interaction once under CISO gives full per-page coverage without
// duplicating across personas. The persona-specific gating is already
// covered by the positive (task 9) and negative (task 10) specs; this layer
// is about the interaction primitives, not access control.
//
// Selector strategy
// -----------------
// Selectors are loose by design — role-based or text-based — so the test
// survives Tailwind class shuffles and minor copy edits. If a more precise
// selector is needed, add a `selector_test_id` field to the interactions
// fixture and have this spec prefer it.
//
// Signal types
// ------------
//   `url`            — the URL pathname/search must contain the
//                      `expected_url_contains` substring after the click.
//   `no-console-error` — the click did NOT produce an uncaught console
//                      error. The most basic signal; useful for read-only
//                      pages where the click toggles a panel or expands a
//                      row but produces no other observable.
//
// Skipped pages
// -------------
// Pages with `interaction_id: null` in the fixture are skipped at runtime
// with the reason recorded in results.json (via the `skipped_reason` field
// the reporter passes through). This makes the gap explicit in the coverage
// matrix — see `interactions.json` for the per-page rationale.
//
// Test id convention
// ------------------
// `e2e.interaction.<page-id>.<interaction-id>` per the task-11c spec.
// For null-interaction pages we still emit a `skipped` row with the test id
// `e2e.interaction.<page-id>.none` so the matrix shows the skipped cell.
//
// Default severity
// ----------------
// The fixture's per-interaction `severity` value (default 'medium') is
// applied. FAIL = the assertion threw OR a console error fired.
//
// Module system: ESM-style imports.

import { test, expect } from '../fixtures.js'
import path from 'node:path'
import fs from 'node:fs'

const RUN_DIR = process.env.RUN_DIR
  || path.resolve(__dirname, '..', '..', 'test-reports', '_local')
const ARTIFACTS_DIR = path.join(RUN_DIR, 'e2e', 'artifacts')

fs.mkdirSync(ARTIFACTS_DIR, { recursive: true })

// Load the interactions fixture synchronously at spec-load time so
// `playwright test --list` can enumerate every per-page parametrisation.
const FIXTURE_PATH = path.resolve(
  __dirname, '..', 'fixtures', 'interactions.json',
)
const fixture = JSON.parse(fs.readFileSync(FIXTURE_PATH, 'utf-8'))
const INTERACTIONS = fixture.interactions || {}

// Load the manifest the same way other specs do, via the shared helper.
// Doing this inside the spec keeps the page list in lockstep with the
// rest of the suite — if the manifest grows, drift in this fixture is
// caught by the runtime guard below.
import { pages } from '../lib/manifest.js'

// Build the entry list at spec-load time. For each page CISO can access,
// look up the interaction by page id and produce one test entry. Pages
// without an interactions.json entry trigger a loud guard (manifest grew
// but the fixture wasn't updated).
const ENTRIES = []
for (const page of pages()) {
  if (!page.accessible_to.includes('ciso')) continue
  const entry = INTERACTIONS[page.id]
  if (entry === undefined) {
    // Missing entry — surface at collection time, not at run time.
    throw new Error(
      `interactions.spec.js: no entry for page id '${page.id}' in ` +
      `e2e/fixtures/interactions.json. Add one (or set interaction_id: null ` +
      `with a documented 'reason' to skip the page explicitly).`,
    )
  }
  ENTRIES.push({
    pageId: page.id,
    pageRoute: page.route,
    pageLabel: page.label,
    interaction: entry,
  })
}

test.describe('per-page primary interactions (CISO)', () => {
  for (const entry of ENTRIES) {
    const { pageId, pageRoute, interaction } = entry
    const interactionId = interaction.interaction_id || 'none'
    const testId = `e2e.interaction.${pageId}.${interactionId}`
    const severity = interaction.severity || 'medium'

    test(testId, async ({ page }, testInfo) => {
      test.skip(
        testInfo.project.name !== 'ciso',
        `${testId} runs under the 'ciso' project only`,
      )

      const evidencePath = `e2e/artifacts/${testId}.png`

      // Null interaction: page is intentionally not exercised. Emit a
      // skipped row with the documented reason, then let Playwright's
      // test.skip mark the test skipped.
      if (interaction.interaction_id === null) {
        const reason = interaction.reason || 'no interaction defined'
        testInfo.annotations.push({
          type: 'harness-result',
          description: JSON.stringify({
            target_kind: 'page',
            target_id: pageId,
            persona: 'ciso',
            evidence_path: evidencePath,
            skipped_reason: reason,
          }),
        })
        test.skip(true, reason)
        return
      }

      testInfo.annotations.push({
        type: 'harness-result',
        description: JSON.stringify({
          target_kind: 'page',
          target_id: pageId,
          persona: 'ciso',
          evidence_path: evidencePath,
        }),
      })
      testInfo.annotations.push({ type: 'severity', description: severity })

      // Collect console errors during the test. Pageerror covers uncaught
      // JS exceptions; console.error covers logged-but-not-thrown.
      const consoleErrors = []
      page.on('pageerror', (err) => {
        consoleErrors.push(`pageerror: ${err.message}`)
      })
      page.on('console', (msg) => {
        if (msg.type() === 'error') {
          // React internal noise filter: jsdom-isms aren't relevant on
          // a deployed CloudFront, but some third-party scripts (analytics,
          // recharts ResizeObserver warnings) log .error. We keep these for
          // now; if a specific message becomes noisy and unfixable, add a
          // suppression list.
          consoleErrors.push(`console.error: ${msg.text()}`)
        }
      })

      let assertionError = null
      try {
        // 1. Navigate.
        await page.goto(pageRoute)
        await page.waitForLoadState('domcontentloaded')

        // 2. Sanity — we didn't bounce off-domain or to /signin.
        expect(page.url()).not.toContain('amazoncognito.com')
        await expect(page.getByText('Access restricted'))
          .toHaveCount(0, { timeout: 1_000 })

        // 3. Locate the interactive element. Prefer text, then role.
        let target
        if (interaction.selector_text) {
          target = page.getByText(
            new RegExp(interaction.selector_text, 'i'),
          ).first()
        } else if (interaction.selector_role) {
          target = page.getByRole(interaction.selector_role).first()
        } else {
          throw new Error(
            `interaction '${interactionId}' for page '${pageId}' has ` +
            `no selector_text or selector_role`,
          )
        }

        // Wait for the target to mount (charts on dashboard, recharts on
        // analytics pages can take a beat). 5s budget.
        await expect(target).toBeVisible({ timeout: 5_000 })

        // 4. Click.
        await target.click()

        // 5. Assert the expected signal.
        const signal = interaction.expected_signal
        if (signal === 'url') {
          // URL change. Use expect.poll because Playwright's click does
          // not always settle the URL by the time it returns.
          await expect.poll(
            () => page.url(),
            {
              message:
                `expected URL to contain '${interaction.expected_url_contains}' ` +
                `after click on '${pageId}'`,
              timeout: 5_000,
            },
          ).toContain(interaction.expected_url_contains)
        } else if (signal === 'no-console-error') {
          // Settle: a brief wait so any post-click handler can run.
          await page.waitForTimeout(500)
          // Assertion is the consoleErrors check below.
        } else {
          throw new Error(
            `unknown expected_signal '${signal}' for ${pageId}.${interactionId}`,
          )
        }

        // 6. No console errors fired during the test, regardless of
        //    signal type.
        expect(
          consoleErrors,
          `${pageId}.${interactionId}: ${consoleErrors.length} console ` +
          `error(s) fired during the interaction:\n  ` +
          consoleErrors.join('\n  '),
        ).toHaveLength(0)
      } catch (err) {
        assertionError = err
      } finally {
        await page.screenshot({
          path: path.join(ARTIFACTS_DIR, `${testId}.png`),
          fullPage: true,
        }).catch(() => {})
        if (assertionError) {
          throw assertionError
        }
      }
    })
  }
})
