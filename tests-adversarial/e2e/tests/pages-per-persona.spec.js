// Page-per-persona positive E2E coverage (task 9).
//
// What this proves
// ----------------
// For every (page, persona) cell in the manifest where the persona appears
// in `page.accessible_to`, this spec generates one Playwright test that:
//   1. Navigates to `page.route` as that persona (using the storage-state
//      the global setup wrote at run start).
//   2. Asserts the URL is the route (or a permitted post-redirect — see
//      `lib/page-assertions.js` for the per-page redirect handling on
//      /signin and /findings/:id).
//   3. Asserts a stable text marker (almost always the page's <h1>) is
//      visible within 10s.
//   4. Takes a screenshot to `${E2E_REPORT_DIR}/artifacts/<test-id>.png`
//      as evidence.
//   5. Emits a per-test row into `${RUN_DIR}/e2e/results.json` via the
//      `harness-result` annotation the reporter at
//      `e2e/reporters/results-reporter.js` consumes.
//
// Coverage matrix half this fills
// -------------------------------
// This spec covers the AC6 "positive" diagonal: every Y cell. Task 10's
// `negative-gating.spec.js` covers the N cells (AccessDenied behavior).
// Together they sum to every (page, persona) cell — 60 across 15 pages
// and 4 personas.
//
// Project parametrization pattern
// -------------------------------
// Playwright tests run once per project. We want each (page, persona) pair
// to execute exactly once. The simplest pattern, per task prompt §"Cross
// checks", is:
//   - Generate one `test()` per (page, persona) pair where persona is in
//     accessible_to.
//   - At runtime, `test.skip(testInfo.project.name !== persona)` so the
//     test only actually runs under the project whose name matches the
//     persona; the other 4 project enumerations are skipped immediately.
// This produces `(positives) × 5` test enumerations in `--list` output —
// only `(positives)` of them actually execute.
//
// Test id convention
// ------------------
// `e2e.page.<page-id>.<persona-id>` — the test's TITLE is the id. The
// reporter at e2e/reporters/results-reporter.js does NOT synthesise an id
// from path/line. Keep the title stable across runs (spec §7.3, the diff
// section breaks if ids change).

import { test, expect } from '@playwright/test'
import path from 'node:path'
import fs from 'node:fs'
import { pages, personaIds } from '../lib/manifest.js'
import { PAGE_ASSERTIONS } from '../lib/page-assertions.js'

// Resolve the per-run artifacts dir at spec-load time. Mirrors the path
// playwright.config.js uses (outputDir is `${RUN_DIR}/e2e/artifacts`), so
// the screenshots we write here land alongside Playwright's own.
const RUN_DIR = process.env.RUN_DIR
  || path.resolve(__dirname, '..', '..', 'test-reports', '_local')
const ARTIFACTS_DIR = path.join(RUN_DIR, 'e2e', 'artifacts')

// Ensure the artifacts dir exists once at spec-load time. mkdirSync is
// idempotent; doing it here keeps the test bodies focused on assertions.
fs.mkdirSync(ARTIFACTS_DIR, { recursive: true })

// Build the (page, persona) pair list ONCE at spec-load time. Reading the
// manifest inside the loop would be wasteful and would block deterministic
// test enumeration in `playwright test --list`.
//
// Synthetic page entries (e.g. `spa-root` for Block D bundle scans) are
// harness-only sentinels with no React route to navigate to — they're skipped
// here so this positive-gating sweep stays focused on real pages. The
// bundle-secrets / bundle-tabnabbing specs cover the synthetic targets.
const PAIRS = []
for (const page of pages()) {
  if (page.synthetic === true) continue
  for (const persona of page.accessible_to) {
    PAIRS.push({
      pageId: page.id,
      pageRoute: page.route,
      persona,
      testId: `e2e.page.${page.id}.${persona}`,
    })
  }
}

// Hard sanity check at spec-load time. If the manifest grows or shrinks the
// total should change here AND in the plan/spec. The expected total today
// is 39 — sum of len(accessible_to) across the 15 pages in the manifest.
// We do not assert the exact number here (that would couple the spec to a
// manifest revision); we only assert it's non-empty and every pair has a
// known page assertion.
if (PAIRS.length === 0) {
  throw new Error(
    'pages-per-persona.spec.js: no (page, persona) pairs were generated ' +
    'from the manifest. Check src/coverage/manifest.json — it should have ' +
    'at least one page with a non-empty accessible_to array.',
  )
}

// Every page id we generate a test for must have a PAGE_ASSERTIONS entry.
// Catch a manifest-add-without-assertion-add at collection time, not at
// run time (which would silently mark the test as failed and confuse the
// implementer).
{
  const knownPersonas = new Set(personaIds())
  for (const pair of PAIRS) {
    if (!PAGE_ASSERTIONS[pair.pageId]) {
      throw new Error(
        `pages-per-persona.spec.js: no PAGE_ASSERTIONS entry for page id ` +
        `'${pair.pageId}'. Add one to e2e/lib/page-assertions.js.`,
      )
    }
    if (!knownPersonas.has(pair.persona)) {
      throw new Error(
        `pages-per-persona.spec.js: pair targets unknown persona ` +
        `'${pair.persona}' for page '${pair.pageId}'. Manifest drift?`,
      )
    }
  }
}

test.describe('positive page-per-persona coverage', () => {
  // One test per (page, persona) pair where persona is in accessible_to.
  // Each test only actually executes under the matching project — the other
  // four project enumerations are skipped at runtime.
  for (const pair of PAIRS) {
    const { pageId, pageRoute, persona, testId } = pair
    const pageAssertion = PAGE_ASSERTIONS[pageId]

    test(testId, async ({ page }, testInfo) => {
      // Skip on the non-matching projects. This keeps `--list` enumeration
      // complete (so the operator can see every parametrisation) while
      // ensuring each (page, persona) pair runs exactly once across the
      // whole project matrix.
      test.skip(
        testInfo.project.name !== persona,
        `${testId} runs under the '${persona}' project only`,
      )

      // Attach the harness-result annotation BEFORE the assertions run so
      // the row lands in results.json even if a later step throws (the
      // reporter reads `test.annotations` at onTestEnd, not from the
      // browser context). The evidence_path is relative to RUN_DIR so the
      // report.html links resolve when the report is forwarded.
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

      // 1. Navigate. Playwright resolves `pageRoute` against `baseURL`
      //    from playwright.config.js (TARGET_BASE_URL or the spec §8
      //    default CloudFront URL).
      await page.goto(pageRoute)

      // 2. Let the SPA settle. The personas all hit cold pages on first
      //    nav; the SPA's effect-driven mocks/API calls fire on mount.
      //    `domcontentloaded` is faster than `networkidle` and good
      //    enough for header assertions (we don't need full chart paint).
      await page.waitForLoadState('domcontentloaded')

      // 3. Sanity check: we did NOT bounce to the Cognito Hosted UI
      //    (which would mean storage-state injection failed) and we did
      //    NOT land on AccessDenied (which would mean the manifest
      //    `accessible_to` got out of sync with the SPA's gating).
      expect(page.url()).not.toContain('amazoncognito.com')
      // AccessDenied has a stable "Access restricted" header (see
      // ui/src/App.jsx::AccessDenied). If we ever see it on a positive
      // cell, the manifest is wrong.
      await expect(
        page.getByText('Access restricted'),
      ).toHaveCount(0, { timeout: 1_000 })

      // 4. Per-page stable assertion (heading or shell marker). See
      //    lib/page-assertions.js for the rationale per page.
      await pageAssertion.assert(page, persona)

      // 5. Screenshot evidence. `path` is absolute so a CI workdir
      //    change doesn't move the file underneath us. We screenshot
      //    the full page (not just viewport) so chart-heavy pages have
      //    a useful artifact when a future regression is investigated.
      await page.screenshot({
        path: path.join(ARTIFACTS_DIR, `${testId}.png`),
        fullPage: true,
      })
    })
  }
})
