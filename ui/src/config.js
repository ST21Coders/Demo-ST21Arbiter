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

// ──────────────────────────── LLM Control panel config ───────
// Mirrored from Infra/params/dev.json at build time by post_deploy_ui.py
// (VITE_* vars). Defaults below match dev.json so `npm run dev` shows the
// correct values offline. Default foundation model is Amazon Nova 2 Lite.
const NOVA_LITE = 'us.amazon.nova-2-lite-v1:0'

const _guardrailVersion = import.meta.env.VITE_GUARDRAIL_VERSION || 'DRAFT'
// Comma-separated list → switcher options. Add new versions in dev.json::GuardrailVersions.
// Always include the active version so the dropdown can render the current selection.
const _guardrailVersions = [
  _guardrailVersion,
  ...(import.meta.env.VITE_GUARDRAIL_VERSIONS || 'DRAFT').split(',').map(v => v.trim()),
].filter(Boolean)

export const GUARDRAIL = {
  name:    import.meta.env.VITE_GUARDRAIL_NAME || 'dev-st21arbiter-poc-guardrail',
  id:      import.meta.env.VITE_GUARDRAIL_ID   || '',
  version: _guardrailVersion,
  versions: [...new Set(_guardrailVersions)],
}

// Per-agent foundation model, keyed by the four hosted AgentCore runtimes.
export const AGENT_MODELS = {
  master:     import.meta.env.VITE_MASTER_MODEL_ID     || NOVA_LITE,
  sharepoint: import.meta.env.VITE_SHAREPOINT_MODEL_ID || NOVA_LITE,
  awsconfig:  import.meta.env.VITE_AWSCONFIG_MODEL_ID  || NOVA_LITE,
  zscaler:    import.meta.env.VITE_ZSCALER_MODEL_ID    || NOVA_LITE,
  servicenow: import.meta.env.VITE_SERVICENOW_MODEL_ID || NOVA_LITE,
}

// Friendly display name for a raw foundation-model ID. Matched by substring so
// region prefixes (us./eu.) don't break the lookup; falls back to a best-effort
// prettified form (strip provider/region prefix + version suffix) for unknowns.
const MODEL_LABELS = [
  ['nova-2-lite',     'Nova 2 Lite'],
  ['claude-sonnet-4-6', 'Claude Sonnet 4.6'],
  ['claude-haiku-4-5',  'Claude Haiku 4.5'],
]

export function modelLabel(modelId) {
  if (!modelId) return ''
  const match = MODEL_LABELS.find(([id]) => modelId.includes(id))
  if (match) return match[1]
  return modelId
    .replace(/^[a-z]{2}\./, '')        // drop region prefix, e.g. us.
    .replace(/^(amazon|anthropic|meta)\./, '') // drop provider prefix
    .replace(/-v\d+:\d+$/, '')         // drop version suffix, e.g. -v1:0
    .replace(/-/g, ' ')
}

// ──────────────────────────── App metadata ───────────────────
// Single source of truth for the version string. Shown in the Sidebar
// footer and the Settings → Environment section.
export const APP_VERSION = '1.5.29'
export const APP_VERSION_NOTE = 'Clarify missing department summaries'
