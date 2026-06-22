// Block D bundle-scan E2E probes (checklist items #25, #41, #42, #60).
//
// Four probes run against the deployed SPA's static assets — no Cognito
// required, so they live under the `no-auth` Playwright project. Probe 5
// (tabnabbing on authenticated pages, item #59) lives in the sibling spec
// `bundle-tabnabbing.spec.js` so it can run under the `ciso` project where
// the protected pages render.
//
// Probes
// ------
//   1. e2e.bundle.hardcoded-keys      — AWS / Slack / GitHub / Anthropic /
//                                       JWT regex sweep over every JS bundle.
//   2. e2e.bundle.source-maps-in-prod — HEAD each `<script src>.map` URL;
//                                       PASS if all 404/403, FAIL if any 200.
//   3. e2e.bundle.sensitive-comments  — TODO/FIXME/HTML-comment/console.log
//                                       leaks across HTML + JS bundles.
//   4. e2e.bundle.sri-on-third-party  — every cross-origin `<script>` /
//                                       `<link rel="stylesheet">` must carry
//                                       an `integrity="..."` attribute.
//
// Why a single spec for probes 1-4: they all fetch the SAME root HTML +
// linked JS bundles. Sharing the fetch saves four round-trips per run, and
// the spec becomes a clean "load once, scan many ways" narrative. Each test
// emits its own results.json row via the reporter annotation, so the
// coverage matrix still gets four distinct cells.
//
// Coverage target
// ---------------
// All four rows target the synthetic `spa-root` page entry the Block D
// manifest update added. Persona is universal — we report it as `ciso` so
// the page-cell lands somewhere visible; alternative would be to emit four
// rows per persona, but the static-asset probes are persona-agnostic so
// duplicating them four times would only add noise.

import { test, expect } from '../fixtures.js'
import {
  scanForHardcodedKeys,
  scanForSensitiveComments,
  extractScriptsAndLinks,
  scanForSriCompliance,
} from '../lib/bundle-scanner.js'
import path from 'node:path'
import fs from 'node:fs'

// Per-run artifacts dir — same convention as the other specs. We write a
// per-test JSON evidence file on FAIL so the report.html can link to a
// concrete artifact rather than a per-test Playwright trace dir (the trace
// dirs are only populated on retry, which we don't run for these probes).
const RUN_DIR = process.env.RUN_DIR
  || path.resolve(__dirname, '..', '..', 'test-reports', '_local')
const ARTIFACTS_DIR = path.join(RUN_DIR, 'e2e', 'artifacts')
fs.mkdirSync(ARTIFACTS_DIR, { recursive: true })

// Cache the SPA root HTML + linked scripts across the four probes. Playwright
// gives each test a fresh page, but the HTTP fetch helpers live in this file
// scope. We populate the cache lazily inside the first test that runs.
let _cachedFetch = null

async function fetchSpaBundle(baseURL, request) {
  if (_cachedFetch) return _cachedFetch
  // 1. Fetch the root HTML. CloudFront serves index.html at `/`.
  const rootResp = await request.get(baseURL, { failOnStatusCode: false })
  if (rootResp.status() !== 200) {
    throw new Error(
      `bundle-secrets: GET ${baseURL} returned ${rootResp.status()} — `
      + `cannot scan an unreachable SPA root.`,
    )
  }
  const html = await rootResp.text()

  // 2. Extract every script src + stylesheet href.
  const tags = extractScriptsAndLinks(html)
  const scriptUrls = tags
    .filter((t) => t.tagName === 'script')
    .map((t) => t.url)

  // 3. Fetch each JS bundle. Resolve relative URLs against baseURL so a
  //    `<script src="/assets/index-abc.js">` becomes a full URL.
  const baseObj = new URL(baseURL)
  const bundles = []
  for (const url of scriptUrls) {
    const fullUrl = new URL(url, baseObj).toString()
    try {
      const resp = await request.get(fullUrl, { failOnStatusCode: false })
      if (resp.status() === 200) {
        bundles.push({ url: fullUrl, body: await resp.text() })
      } else {
        // A non-200 here is unusual but not fatal — record the URL so the
        // probe can note "we tried" without crashing the rest of the sweep.
        bundles.push({ url: fullUrl, body: '', status: resp.status() })
      }
    } catch (err) {
      // Network errors get the same treatment — record + continue.
      bundles.push({ url: fullUrl, body: '', error: err.message })
    }
  }

  _cachedFetch = {
    html,
    tags,
    scriptUrls,
    bundles,
    baseObj,
  }
  return _cachedFetch
}

// Helper: write an evidence JSON file under the per-run artifacts dir, return
// the relative path the reporter should record. Filename is the test_id so
// duplicate runs overwrite cleanly.
function writeEvidence(testId, payload) {
  const filename = `${testId}.json`
  const absPath = path.join(ARTIFACTS_DIR, filename)
  fs.writeFileSync(absPath, JSON.stringify(payload, null, 2), 'utf-8')
  return `e2e/artifacts/${filename}`
}

// Skip the entire describe block under any project except no-auth. Probes
// hit only static assets so they don't need an authenticated context.
test.describe('Block D — bundle scans @bundle', () => {
  test.skip(
    ({}, testInfo) => testInfo.project.name !== 'no-auth',
    'bundle probes hit static assets — no-auth project only',
  )

  // ─── Probe 1: hardcoded keys ────────────────────────────────────────────

  test('e2e.bundle.hardcoded-keys', async ({ request }, testInfo) => {
    const testId = 'e2e.bundle.hardcoded-keys'
    const baseURL = testInfo.project.use.baseURL
    test.skip(!baseURL, 'no baseURL configured')

    const { bundles } = await fetchSpaBundle(baseURL, request)

    // Concatenate every fetched bundle body. We could also report which
    // bundle leaked, and we do — by tracking findings per bundle URL.
    const allFindings = []
    for (const { url, body } of bundles) {
      const hits = scanForHardcodedKeys(body)
      for (const hit of hits) {
        allFindings.push({ url, ...hit })
      }
    }

    // Severity policy: ANY high-severity hit -> high. Otherwise if there are
    // medium hits -> medium. Otherwise low (no hits = PASS).
    const high = allFindings.filter((f) => f.severity === 'high')
    const medium = allFindings.filter((f) => f.severity === 'medium')
    const severity = high.length > 0 ? 'high' : medium.length > 0 ? 'medium' : 'low'

    const evidencePath = writeEvidence(testId, {
      probe: 'hardcoded-keys',
      bundles_scanned: bundles.map((b) => b.url),
      findings: allFindings,
      counts: { high: high.length, medium: medium.length },
    })

    testInfo.annotations.push({
      type: 'harness-result',
      description: JSON.stringify({
        target_kind: 'page',
        target_id: 'spa-root',
        persona: 'ciso',
        evidence_path: evidencePath,
      }),
    })
    if (allFindings.length > 0) {
      testInfo.annotations.push({
        type: 'severity',
        description: severity,
      })
    }

    expect(
      allFindings,
      `bundle leak: ${JSON.stringify(allFindings, null, 2)}`,
    ).toEqual([])
  })

  // ─── Probe 2: source maps in production ─────────────────────────────────

  test('e2e.bundle.source-maps-in-prod', async ({ request }, testInfo) => {
    const testId = 'e2e.bundle.source-maps-in-prod'
    const baseURL = testInfo.project.use.baseURL
    test.skip(!baseURL, 'no baseURL configured')

    const { scriptUrls, baseObj } = await fetchSpaBundle(baseURL, request)

    // HEAD `<script>.map` for every script URL. PASS on 404/403, FAIL on 200.
    const exposed = []
    const checked = []
    for (const url of scriptUrls) {
      const fullUrl = new URL(url, baseObj).toString() + '.map'
      const resp = await request.fetch(fullUrl, {
        method: 'HEAD',
        failOnStatusCode: false,
      })
      const status = resp.status()
      checked.push({ url: fullUrl, status })
      if (status === 200) {
        exposed.push({ url: fullUrl, status })
      }
    }

    const evidencePath = writeEvidence(testId, {
      probe: 'source-maps-in-prod',
      checked,
      exposed,
    })

    testInfo.annotations.push({
      type: 'harness-result',
      description: JSON.stringify({
        target_kind: 'page',
        target_id: 'spa-root',
        persona: 'ciso',
        evidence_path: evidencePath,
      }),
    })
    if (exposed.length > 0) {
      testInfo.annotations.push({ type: 'severity', description: 'medium' })
    }

    expect(
      exposed,
      `source maps exposed in production: ${JSON.stringify(exposed, null, 2)}`,
    ).toEqual([])
  })

  // ─── Probe 3: sensitive comments / debug logging ────────────────────────

  test('e2e.bundle.sensitive-comments', async ({ request }, testInfo) => {
    const testId = 'e2e.bundle.sensitive-comments'
    const baseURL = testInfo.project.use.baseURL
    test.skip(!baseURL, 'no baseURL configured')

    const { html, bundles } = await fetchSpaBundle(baseURL, request)

    const findings = []
    // Root HTML.
    for (const hit of scanForSensitiveComments(html)) {
      findings.push({ source: 'index.html', ...hit })
    }
    // Each bundle.
    for (const { url, body } of bundles) {
      for (const hit of scanForSensitiveComments(body)) {
        findings.push({ source: url, ...hit })
      }
    }

    const evidencePath = writeEvidence(testId, {
      probe: 'sensitive-comments',
      bundles_scanned: bundles.map((b) => b.url),
      findings,
    })

    testInfo.annotations.push({
      type: 'harness-result',
      description: JSON.stringify({
        target_kind: 'page',
        target_id: 'spa-root',
        persona: 'ciso',
        evidence_path: evidencePath,
      }),
    })
    // All four categories report severity LOW; the test fails if any hit
    // (LOW findings still count as findings for the report).
    if (findings.length > 0) {
      testInfo.annotations.push({ type: 'severity', description: 'low' })
    }

    expect(
      findings,
      `sensitive comments / debug logging: ${JSON.stringify(findings, null, 2)}`,
    ).toEqual([])
  })

  // ─── Probe 4: Subresource Integrity (SRI) on third-party assets ─────────

  test('e2e.bundle.sri-on-third-party', async ({ request }, testInfo) => {
    const testId = 'e2e.bundle.sri-on-third-party'
    const baseURL = testInfo.project.use.baseURL
    test.skip(!baseURL, 'no baseURL configured')

    const { tags, baseObj } = await fetchSpaBundle(baseURL, request)

    const { thirdPartyCount, missingSri } = scanForSriCompliance(
      tags,
      baseObj.hostname,
    )

    const evidencePath = writeEvidence(testId, {
      probe: 'sri-on-third-party',
      spa_hostname: baseObj.hostname,
      total_tags_scanned: tags.length,
      third_party_count: thirdPartyCount,
      missing_sri: missingSri,
    })

    testInfo.annotations.push({
      type: 'harness-result',
      description: JSON.stringify({
        target_kind: 'page',
        target_id: 'spa-root',
        persona: 'ciso',
        evidence_path: evidencePath,
        ...(thirdPartyCount === 0
          ? { skipped_reason: 'no third-party assets found' }
          : {}),
      }),
    })

    if (thirdPartyCount === 0) {
      // No third-party assets to check — skip explicitly so the matrix
      // records a deliberate SKIP rather than a hollow PASS.
      test.skip(true, 'no third-party assets found in SPA HTML')
      return
    }

    if (missingSri.length > 0) {
      testInfo.annotations.push({ type: 'severity', description: 'medium' })
    }

    expect(
      missingSri,
      `third-party assets missing SRI: ${JSON.stringify(missingSri, null, 2)}`,
    ).toEqual([])
  })
})
