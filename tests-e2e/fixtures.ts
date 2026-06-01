/**
 * Playwright fixture that injects a mock Cognito token into sessionStorage
 * before every test. The SPA's <RequireAuth> wrapper redirects unauthenticated
 * users to /signin even in mock mode (USE_MOCK only switches API behavior,
 * not auth) — so without this fixture every test lands on the sign-in page.
 *
 * The token is shaped like a Cognito IdToken but its signature is fake.
 * api_handler's _caller_user_id decodes payload-only, so this mirrors the
 * production trust model documented in docs/SECURITY_AUDIT.md (finding 1).
 *
 * For tests that need a specific persona, use the typed variants below.
 */
import { test as base, expect } from '@playwright/test';

function b64url(s: string): string {
  return Buffer.from(s).toString('base64').replace(/=/g, '').replace(/\+/g, '-').replace(/\//g, '_');
}

function mockToken(groups: string[]): string {
  const payload = b64url(JSON.stringify({
    sub: `mock-${groups[0]}-sub-0000`,
    'cognito:username': `mock_${groups[0]}@arbiter.test`,
    'cognito:groups': groups,
    exp: Math.floor(Date.now() / 1000) + 3600,
    token_use: 'id',
  }));
  return `${b64url('{"alg":"none","typ":"JWT"}')}.${payload}.${b64url('fake-sig')}`;
}

type Personas = 'ciso' | 'soc' | 'grc' | 'employee';

async function injectTokens(page: import('@playwright/test').Page, persona: Personas) {
  const token = mockToken([persona]);
  await page.addInitScript((t) => {
    sessionStorage.setItem('arbiter.tokens', JSON.stringify({
      id_token: t,
      access_token: t,
      refresh_token: 'mock-refresh-token',
      expires_at: Date.now() + 3600 * 1000,
    }));
  }, token);
}

export const test = base.extend<{ asCiso: void; asSoc: void; asGrc: void; asEmployee: void }>({
  // Default: every test gets a CISO token (broadest access — most pages render).
  page: async ({ page }, use) => {
    await injectTokens(page, 'ciso');
    await use(page);
  },
  // Opt-in persona fixtures for the persona-RBAC tests.
  asCiso: [async ({ page }, use) => { await injectTokens(page, 'ciso'); await use(); }, { auto: false }],
  asSoc: [async ({ page }, use) => { await injectTokens(page, 'soc'); await use(); }, { auto: false }],
  asGrc: [async ({ page }, use) => { await injectTokens(page, 'grc'); await use(); }, { auto: false }],
  asEmployee: [async ({ page }, use) => { await injectTokens(page, 'employee'); await use(); }, { auto: false }],
});

export { expect };
