import { test, expect } from '../fixtures';

/**
 * Routes that are accessible without authentication (mock mode treats USE_MOCK
 * as bypassing the persona guards, so every route renders). These are the
 * minimum that must load without console errors or crashes.
 */
const ROUTES = [
  { path: '/', title: /Dashboard/i },
  { path: '/findings', title: /Findings/i },
  { path: '/heatmap', title: /Heat ?Map|Architecture/i },
  { path: '/actions', title: /Action/i },
  { path: '/governance', title: /Governance|Compliance/i },
  { path: '/audit', title: /Audit/i },
  { path: '/analyst', title: /Analyst|Chat/i },
  { path: '/personas', title: /Persona/i },
];

for (const r of ROUTES) {
  test(`@smoke route loads: ${r.path}`, async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (err) => errors.push(err.message));
    page.on('console', (msg) => {
      if (msg.type() === 'error') errors.push(msg.text());
    });

    const resp = await page.goto(r.path, { waitUntil: 'networkidle' });
    expect(resp?.status(), `HTTP status for ${r.path}`).toBeLessThan(400);
    await expect(page.locator('body')).toBeVisible();
    expect(errors, `console errors on ${r.path}`).toEqual([]);
  });
}

test('@smoke browser back/forward keeps app responsive', async ({ page }) => {
  await page.goto('/');
  await page.goto('/findings');
  await page.goBack();
  await expect(page).toHaveURL(/\/$/);
  await page.goForward();
  await expect(page).toHaveURL(/\/findings$/);
});

test('deep link to /findings?severity=HIGH renders without crashing', async ({ page }) => {
  const resp = await page.goto('/findings?severity=HIGH');
  expect(resp?.status()).toBeLessThan(400);
  await expect(page.locator('body')).toBeVisible();
});
