// Block D — Probe 5: tabnabbing on external links (checklist item #59).
//
// Authenticated CISO sweep over a handful of page routes; every
// `<a target="_blank">` whose href hostname differs from the SPA host must
// carry both `rel="noopener"` AND `rel="noreferrer"`. Missing either token
// is a tabnabbing exposure (the opened tab can navigate `window.opener`).
//
// Why split from bundle-secrets.spec.js: probes 1-4 hit static assets and
// run under the `no-auth` project. Probe 5 needs an authenticated SPA so
// the page chrome / link-bearing content renders — runs under the `ciso`
// project. Keeping them separate makes the project-scoping declarative.
//
// Page sweep
// ----------
// We don't iterate every manifest page — most ARBITER routes have no
// outbound links. The sweep covers Dashboard + Settings + Integrations,
// which between them render every external-link surface the SPA ships
// today (CloudWatch links from Dashboard, doc links from Settings, vendor
// console links from Integrations). New pages with external links should
// be added to this list.

import { test, expect } from '@playwright/test'
import {
  extractTargetBlankAnchors,
  relProtectsAgainstTabnabbing,
} from '../lib/bundle-scanner.js'
import path from 'node:path'
import fs from 'node:fs'

const RUN_DIR = process.env.RUN_DIR
  || path.resolve(__dirname, '..', '..', 'test-reports', '_local')
const ARTIFACTS_DIR = path.join(RUN_DIR, 'e2e', 'artifacts')
fs.mkdirSync(ARTIFACTS_DIR, { recursive: true })

// Routes to sweep. Keep this small — the goal is to spot a regression where
// someone adds an external link without the rel guards, not to enumerate
// every page. Order matches the SPA nav order.
const SWEEP_ROUTES = [
  { route: '/', pageId: 'dashboard' },
  { route: '/settings', pageId: 'settings' },
  { route: '/integrations', pageId: 'integrations' },
]

test.describe('Block D — tabnabbing sweep @tabnabbing', () => {
  test.skip(
    ({}, testInfo) => testInfo.project.name !== 'ciso',
    'tabnabbing probe walks authenticated CISO pages',
  )

  for (const { route, pageId } of SWEEP_ROUTES) {
    const testId = `e2e.bundle.tabnabbing.${pageId}`

    test(testId, async ({ page }, testInfo) => {
      await page.goto(route)
      // Wait for the SPA to settle. domcontentloaded is enough for the
      // anchor-tag rendering we care about; networkidle would slow the
      // sweep without changing the result for static link markup.
      await page.waitForLoadState('domcontentloaded')

      const html = await page.content()
      const baseURL = testInfo.project.use.baseURL
      const spaHostname = baseURL ? new URL(baseURL).hostname : ''

      // Collect every `<a target="_blank">` then narrow to external hosts.
      const all = extractTargetBlankAnchors(html)
      const external = []
      for (const a of all) {
        let host
        try {
          host = new URL(a.href, baseURL || 'https://example.com').hostname
        } catch {
          // Malformed href — skip it. The browser would refuse to open it
          // anyway, so it's not a tabnabbing surface.
          continue
        }
        if (!spaHostname || host.toLowerCase() === spaHostname.toLowerCase()) {
          continue
        }
        external.push(a)
      }

      const unprotected = external.filter(
        (a) => !relProtectsAgainstTabnabbing(a.rel),
      )

      const evidenceFilename = `${testId}.json`
      const evidencePath = `e2e/artifacts/${evidenceFilename}`
      fs.writeFileSync(
        path.join(ARTIFACTS_DIR, evidenceFilename),
        JSON.stringify(
          {
            probe: 'tabnabbing',
            page_id: pageId,
            route,
            spa_hostname: spaHostname,
            external_links_found: external.length,
            unprotected,
          },
          null,
          2,
        ),
        'utf-8',
      )

      // Persona-targeted page row — tabnabbing is a per-page rendering
      // exposure, so we tag the actual page the link came from rather than
      // the synthetic spa-root sentinel. This also keeps the (page, persona)
      // cell for `dashboard.ciso` etc. usefully filled even when bundle
      // probes are the only e2e signal in play.
      testInfo.annotations.push({
        type: 'harness-result',
        description: JSON.stringify({
          target_kind: 'page',
          target_id: pageId,
          persona: 'ciso',
          evidence_path: evidencePath,
          ...(external.length === 0
            ? { skipped_reason: 'no external target=_blank links on this page' }
            : {}),
        }),
      })

      if (external.length === 0) {
        test.skip(true, 'no external target=_blank links on this page')
        return
      }

      if (unprotected.length > 0) {
        testInfo.annotations.push({ type: 'severity', description: 'medium' })
      }

      expect(
        unprotected,
        `external target=_blank links missing noopener/noreferrer: `
        + `${JSON.stringify(unprotected, null, 2)}`,
      ).toEqual([])
    })
  }
})
