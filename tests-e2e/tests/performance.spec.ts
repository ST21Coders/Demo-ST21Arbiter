/**
 * Performance baselines. Measures wall-clock load time for each route and
 * fails if it exceeds the budget. Budgets are intentionally lenient in mock
 * mode (the Vite dev server is the slowest part).
 *
 * Threshold via env: SLOW_PAGE_MS (default 3000ms). Mirrored in the report
 * generator's `performance.slow_pages` analysis.
 */
import { test, expect } from '../fixtures';

const BUDGET_MS = Number(process.env.SLOW_PAGE_MS ?? 3000);

const ROUTES = ['/', '/findings', '/heatmap', '/actions', '/governance', '/audit', '/analyst'];

for (const path of ROUTES) {
  test(`@perf page load budget ${path} < ${BUDGET_MS}ms`, async ({ page }) => {
    const start = Date.now();
    const resp = await page.goto(path, { waitUntil: 'domcontentloaded' });
    const elapsed = Date.now() - start;
    expect(resp?.status() ?? 0).toBeLessThan(400);
    expect(elapsed,
      `Page ${path} took ${elapsed}ms (budget ${BUDGET_MS}ms). Threshold can ` +
      `be tuned via SLOW_PAGE_MS env var.`
    ).toBeLessThan(BUDGET_MS);
  });
}
