// Block-B clickjacking E2E probe (checklist item #56).
//
// Goal: confirm a real browser refuses to render the ARBITER SPA inside an
// <iframe>. The headers-side check (`headers/test_clickjacking.py`) asserts
// the response carries X-Frame-Options DENY or CSP frame-ancestors; this
// spec verifies the browser-side effect of those headers.
//
// Approach:
//   1. Navigate to a tiny data: URL that hosts an <iframe src="<target>/" />.
//   2. Wait briefly, then read the iframe's contentDocument.
//   3. PASS if the iframe is empty (browser refused to render), or its
//      contentDocument is null (cross-origin without permission to read).
//   4. FAIL if the iframe rendered the SPA — visible heading element from
//      the dashboard appears inside the frame.
//
// Why the no-auth project: clickjacking is a pre-auth concern — the browser
// either refuses to render the page or it doesn't. We don't need a
// storageState here.

import { test, expect } from '@playwright/test'

test.describe.configure({ mode: 'parallel' })

test('SPA refuses to render inside an iframe @clickjacking', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'no-auth', 'clickjacking is pre-auth — no-auth project only')

  // Pull the target base URL from Playwright's config (set via TARGET_BASE_URL env).
  const baseURL = testInfo.project.use.baseURL
  if (!baseURL) {
    test.skip(true, 'no baseURL configured on the no-auth project')
  }

  // A data: URL that hosts an iframe pointing at the SPA. The host page is
  // about:blank-ish, so any successful iframe render is a clickjacking
  // exposure.
  const wrapperHtml = `
    <!doctype html>
    <html><head><title>clickjacking-probe</title></head>
    <body>
      <iframe id="probe" src="${baseURL}/" width="800" height="600"
              style="border:2px solid red"></iframe>
    </body></html>
  `

  await page.goto('data:text/html;base64,' + Buffer.from(wrapperHtml).toString('base64'))

  // Give the iframe time to either render or be rejected by the browser.
  // X-Frame-Options DENY rejects synchronously; CSP frame-ancestors does too.
  // A 2-second wait is generous and well under Playwright's default timeout.
  await page.waitForTimeout(2_000)

  const frame = page.frameLocator('#probe')

  // Try to find any content that the SPA's dashboard normally renders. If we
  // see ANY of these, the iframe rendered — clickjacking exposure.
  const dashboardHeading = await frame
    .getByRole('heading')
    .first()
    .textContent({ timeout: 1_000 })
    .catch(() => null)

  // If the framing is correctly blocked, the iframe is either empty (XFO
  // DENY) or cross-origin (we can't read contentDocument). Either way,
  // dashboardHeading should be null or empty.
  expect(
    dashboardHeading,
    `clickjacking exposure: SPA rendered inside iframe (heading=${JSON.stringify(dashboardHeading)})`,
  ).toBeFalsy()
})
