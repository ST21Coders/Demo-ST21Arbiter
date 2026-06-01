import {
  CognitoIdentityProviderClient,
  InitiateAuthCommand,
  AuthFlowType,
} from '@aws-sdk/client-cognito-identity-provider';
import type { Page } from '@playwright/test';

type Tokens = {
  id_token: string;
  access_token: string;
  refresh_token: string;
  expires_at: number;
};

/**
 * Programmatic Cognito login via USER_PASSWORD_AUTH. Returns the same token
 * shape the SPA's useAuth hook stores in sessionStorage under 'arbiter.tokens',
 * so injectTokens() can drop them in without going through the Hosted UI.
 */
export async function getCognitoTokens(opts: {
  region: string;
  clientId: string;
  username: string;
  password: string;
}): Promise<Tokens> {
  const client = new CognitoIdentityProviderClient({ region: opts.region });
  const cmd = new InitiateAuthCommand({
    AuthFlow: AuthFlowType.USER_PASSWORD_AUTH,
    ClientId: opts.clientId,
    AuthParameters: { USERNAME: opts.username, PASSWORD: opts.password },
  });
  const resp = await client.send(cmd);
  const r = resp.AuthenticationResult;
  if (!r?.IdToken || !r.AccessToken || !r.RefreshToken) {
    throw new Error(
      `Cognito InitiateAuth returned no tokens (challenge=${resp.ChallengeName ?? 'none'}). ` +
        'If a NEW_PASSWORD_REQUIRED challenge fires, the test user needs its temporary password reset.'
    );
  }
  return {
    id_token: r.IdToken,
    access_token: r.AccessToken,
    refresh_token: r.RefreshToken,
    expires_at: Date.now() + (r.ExpiresIn ?? 3600) * 1000,
  };
}

/**
 * Inject tokens into sessionStorage at the SPA's storage key so the next
 * navigation sees an authenticated session — mirrors what useAuth.handleCallback()
 * writes after the Cognito redirect.
 */
export async function injectTokens(page: Page, tokens: Tokens): Promise<void> {
  await page.addInitScript((t) => {
    sessionStorage.setItem('arbiter.tokens', JSON.stringify(t));
  }, tokens);
}
