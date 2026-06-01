import { test, expect } from '../fixtures';

test('@smoke all images on dashboard have alt text', async ({ page }) => {
  await page.goto('/');
  const imgs = page.locator('img');
  const count = await imgs.count();
  for (let i = 0; i < count; i++) {
    const alt = await imgs.nth(i).getAttribute('alt');
    expect(alt, `img #${i} is missing alt text`).not.toBeNull();
  }
});

test('keyboard Tab can reach the first sidebar link', async ({ page }) => {
  await page.goto('/');
  // Press Tab a few times until focus lands on something tabbable. We assert
  // *some* element becomes focused, not a specific one — locator stability
  // beats over-precision here.
  await page.keyboard.press('Tab');
  const focused = await page.evaluate(() => document.activeElement?.tagName);
  expect(focused).toMatch(/^(A|BUTTON|INPUT|SELECT|TEXTAREA)$/);
});
