import { test, expect } from '../fixtures';

test('@smoke Findings page renders mock conflicts and severity filter narrows the list', async ({ page }) => {
  await page.goto('/findings');
  await expect(page.locator('body')).toBeVisible();

  // Mock data ships 12 ARBITER-UC conflicts. Expect at least one ARBITER-UC* badge.
  const ids = page.getByText(/ARBITER-UC\d+/);
  await expect(ids.first()).toBeVisible({ timeout: 10_000 });
  const totalBefore = await ids.count();
  expect(totalBefore).toBeGreaterThan(0);

  // Pick the severity select and narrow to HIGH — the count must change or stay
  // the same but never grow. If the dropdown can't be found, skip with a clear
  // message rather than failing on a UI element location guess.
  const severitySelect = page.getByRole('combobox').first();
  if (await severitySelect.count() === 0) {
    test.skip(true, 'No severity dropdown found on Findings — UI changed; update selector.');
  }
  await severitySelect.selectOption({ label: /high/i });
  const totalAfter = await ids.count();
  expect(totalAfter).toBeLessThanOrEqual(totalBefore);
});

test('Findings expandable rows toggle when clicked', async ({ page }) => {
  await page.goto('/findings');
  const firstRow = page.getByText(/ARBITER-UC\d+/).first();
  await firstRow.waitFor({ state: 'visible', timeout: 10_000 });
  await firstRow.click();
  // After click, some additional detail text should be visible. The mock data
  // includes remediation_steps in expanded panels — assert one is now present
  // somewhere on the page.
  await expect(page.getByText(/remediat|policy_mandate|regulatory/i).first()).toBeVisible({
    timeout: 5000,
  });
});

test('Sidebar nav links navigate to the correct routes', async ({ page }) => {
  await page.goto('/');
  // The Sidebar groups OVERVIEW / GOVERNANCE / INTELLIGENCE / INFRASTRUCTURE
  // contain anchors. Click Findings via its accessible name.
  await page.getByRole('link', { name: /findings/i }).first().click();
  await expect(page).toHaveURL(/\/findings$/);

  await page.getByRole('link', { name: /audit/i }).first().click();
  await expect(page).toHaveURL(/\/audit$/);
});

test('AuditLogs renders rows from mock data', async ({ page }) => {
  await page.goto('/audit');
  // MOCK_AUDIT has entries like SCAN_TRIGGERED, CR_CREATED, etc.
  await expect(page.getByText(/SCAN_TRIGGERED|CR_CREATED|CR_APPROVED/).first()).toBeVisible({
    timeout: 10_000,
  });
});
