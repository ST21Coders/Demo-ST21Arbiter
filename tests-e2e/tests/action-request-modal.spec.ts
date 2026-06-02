import { test, expect } from '../fixtures';

/**
 * The ActionRequestModal is the only real form in the SPA. It's triggered by
 * a "Create Action" or "New Action" button on Findings or ActionCenter.
 */
test('@smoke ActionRequestModal opens and closes', async ({ page }) => {
  await page.goto('/actions');
  const openBtn = page.getByRole('button', { name: /new action|create action/i }).first();
  await openBtn.waitFor({ state: 'visible', timeout: 10_000 });
  await openBtn.click();

  // Modal is open: the request textarea should be present and focusable.
  const requestInput = page.getByRole('textbox').first();
  await expect(requestInput).toBeVisible();

  // Close via the close button (×) or Escape.
  await page.keyboard.press('Escape');
  await expect(requestInput).toBeHidden({ timeout: 5000 });
});

test('ActionRequestModal blocks submit when request field is empty', async ({ page }) => {
  await page.goto('/actions');
  await page.getByRole('button', { name: /new action|create action/i }).first().click();

  const submitBtn = page.getByRole('button', { name: /submit|create|send/i }).last();
  // Click submit without filling anything. Expect either the button to be
  // disabled OR the modal to stay open (we shouldn't be navigated away).
  const wasDisabled = await submitBtn.isDisabled().catch(() => false);
  if (!wasDisabled) {
    await submitBtn.click();
    // Modal should still be open — request textarea still visible.
    await expect(page.getByRole('textbox').first()).toBeVisible();
  }
});
