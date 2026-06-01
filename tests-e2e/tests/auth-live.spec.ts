import { test, expect } from '@playwright/test';
import { getCognitoTokens, injectTokens } from '../helpers/auth';

/**
 * Single live smoke covering the real Cognito → SPA path. Skipped unless
 * TEST_MODE=live AND credentials are configured. We get tokens via boto3-style
 * USER_PASSWORD_AUTH (no Hosted UI scraping), then verify a protected route
 * renders with the persona's allowed pages.
 */
test('@live Cognito programmatic auth renders a protected route', async ({ page }) => {
  test.skip(
    process.env.TEST_MODE !== 'live',
    'Skipped outside TEST_MODE=live to keep mock runs deterministic + free.'
  );

  const required = [
    'LIVE_BASE_URL',
    'COGNITO_REGION',
    'COGNITO_CLIENT_ID',
    'TEST_USER_USERNAME',
    'TEST_USER_PASSWORD',
  ];
  for (const k of required) {
    if (!process.env[k]) test.skip(true, `Missing env: ${k}`);
  }

  const tokens = await getCognitoTokens({
    region: process.env.COGNITO_REGION!,
    clientId: process.env.COGNITO_CLIENT_ID!,
    username: process.env.TEST_USER_USERNAME!,
    password: process.env.TEST_USER_PASSWORD!,
  });

  await injectTokens(page, tokens);
  await page.goto(`${process.env.LIVE_BASE_URL!}/`);

  // The Personas link is always-on; using it as a low-noise authenticated check.
  await expect(page.getByRole('link', { name: /persona/i }).first()).toBeVisible({
    timeout: 15_000,
  });
});
