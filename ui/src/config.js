// Runtime configuration. Build-time values come from Vite env vars
// (see .env.production.example) so the same compiled bundle can be
// re-pointed without code changes.

// ──────────────────────────── API endpoints ──────────────────
// Set VITE_API_URL to the API Gateway invoke URL printed by the
// 06-api CloudFormation stack output `ApiEndpoint`, e.g.
// https://abcd1234.execute-api.us-east-1.amazonaws.com/dev
export const API_URL  = import.meta.env.VITE_API_URL  || ''
export const CHAT_URL = import.meta.env.VITE_CHAT_URL || API_URL || ''

// Mock mode: when no API URL is set, useApi.js falls back to the
// canned mockData fixtures so the UI is fully demoable offline.
export const USE_MOCK = !API_URL

// ──────────────────────────── Cognito (UI ↔ API auth) ────────
// All three values come from the 03-identity stack outputs.
// The UI uses Authorization Code with PKCE against the hosted UI.
export const COGNITO = {
  region:      import.meta.env.VITE_COGNITO_REGION       || 'us-east-1',
  userPoolId:  import.meta.env.VITE_COGNITO_USER_POOL_ID || '',
  clientId:    import.meta.env.VITE_COGNITO_CLIENT_ID    || '',
  domain:      import.meta.env.VITE_COGNITO_DOMAIN       || '', // e.g. dev-lmarbiter.auth.us-east-1.amazoncognito.com
  redirectUri: import.meta.env.VITE_COGNITO_REDIRECT_URI || (typeof window !== 'undefined' ? `${window.location.origin}/callback` : ''),
  logoutUri:   import.meta.env.VITE_COGNITO_LOGOUT_URI   || (typeof window !== 'undefined' ? window.location.origin : ''),
  scopes:      ['openid', 'email', 'profile'],
}

// Hosted-UI login URL. Redirect the user here for sign-in.
export function cognitoLoginURL() {
  if (!COGNITO.domain || !COGNITO.clientId) return ''
  const p = new URLSearchParams({
    client_id: COGNITO.clientId,
    response_type: 'code',
    scope: COGNITO.scopes.join(' '),
    redirect_uri: COGNITO.redirectUri,
  })
  return `https://${COGNITO.domain}/login?${p.toString()}`
}

export function cognitoLogoutURL() {
  if (!COGNITO.domain || !COGNITO.clientId) return ''
  const p = new URLSearchParams({
    client_id: COGNITO.clientId,
    logout_uri: COGNITO.logoutUri,
  })
  return `https://${COGNITO.domain}/logout?${p.toString()}`
}

// ──────────────────────────── Model IDs (display only) ───────
export const MODELS = {
  haiku:  'anthropic.claude-haiku-4-5-20251001-v1:0',
  sonnet: 'anthropic.claude-sonnet-4-6-20251006-v1:0',
}

// ──────────────────────────── App metadata ───────────────────
// Single source of truth for the version string. Shown in the Sidebar
// footer and the Settings → Environment section.
export const APP_VERSION = '1.2.0-poc'
