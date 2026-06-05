// Minimal Cognito Hosted-UI auth helper.
//
// Flow:
//   1. User clicks "Sign in" → window.location = cognitoLoginURL().
//   2. Cognito redirects back to /callback?code=... .
//   3. handleCallback() POSTs the code to Cognito's token endpoint,
//      stores { id_token, access_token, refresh_token, expires_at }
//      in sessionStorage under 'arbiter.tokens'.
//   4. authHeaders() returns { Authorization: 'Bearer <id_token>' } so
//      useApi.js can attach it to every fetch.
//   5. If the IdToken is expired, refresh() exchanges the refresh_token.
//
// No external library needed — keeps the bundle small for the demo.

import { COGNITO, USE_MOCK, cognitoLoginURL, cognitoLogoutURL } from '../config'

const KEY = 'arbiter.tokens'

function load() {
  try { return JSON.parse(sessionStorage.getItem(KEY) || 'null') } catch { return null }
}
function save(t) { sessionStorage.setItem(KEY, JSON.stringify(t)) }
function clear() { sessionStorage.removeItem(KEY) }

// Dev persona override — local-host mock-mode only. There is no Cognito sign-in
// on localhost, so getGroups()/getEmail() have no JWT to decode and the four
// personas can't be exercised end-to-end. DEV_AUTH gates a sessionStorage-backed
// override readable by getGroups/getEmail/isAuthenticated. In any build with
// VITE_API_URL set (live mode), USE_MOCK is false and this path is dead code.
const DEV_AUTH = import.meta.env.DEV && USE_MOCK
const DEV_PERSONA_KEY = 'arbiter.devPersona'
const DEV_PERSONA_DEFAULT = 'ciso'
const DEV_PERSONA_MAP = {
  ciso:     { groups: ['ciso'],     email: 'ciso_diana@meridianinsurance.com' },
  soc:      { groups: ['soc'],      email: 'soc_marcus@meridianinsurance.com' },
  grc:      { groups: ['grc'],      email: 'grc_priya@meridianinsurance.com' },
  employee: { groups: ['employee'], email: 'emp_sarah@meridianinsurance.com' },
}

export function isDevAuth() { return DEV_AUTH }

export function getDevPersonaId() {
  if (!DEV_AUTH) return null
  const id = sessionStorage.getItem(DEV_PERSONA_KEY) || DEV_PERSONA_DEFAULT
  return DEV_PERSONA_MAP[id] ? id : DEV_PERSONA_DEFAULT
}

function devPersonaRecord() {
  return DEV_AUTH ? DEV_PERSONA_MAP[getDevPersonaId()] : null
}

export function setDevPersona(id) {
  if (!DEV_AUTH) return
  if (id && DEV_PERSONA_MAP[id]) sessionStorage.setItem(DEV_PERSONA_KEY, id)
  else sessionStorage.removeItem(DEV_PERSONA_KEY)
}

export function isAuthenticated() {
  if (DEV_AUTH) return true
  const t = load()
  if (!t) return false
  return Date.now() < (t.expires_at || 0)
}

export function getIdToken() {
  const t = load()
  return t?.id_token || ''
}

// Epoch-ms at which the current IdToken expires, or null when there's no token.
// Used by Settings → Session for the expiry countdown.
export function getSessionExpiry() {
  const t = load()
  return t?.expires_at || null
}

export function authHeaders() {
  const token = getIdToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

// Decode the IdToken payload (JWT middle segment, base64url). Demo-only —
// no signature verify (trusted-issuer pattern, mirrors api_handler.py).
function decodeIdTokenPayload() {
  const token = getIdToken()
  if (!token) return null
  try {
    const [, payloadB64] = token.split('.')
    if (!payloadB64) return null
    const base64 = payloadB64.replace(/-/g, '+').replace(/_/g, '/')
    const padded = base64 + '==='.slice((base64.length + 3) % 4)
    return JSON.parse(atob(padded))
  } catch {
    return null
  }
}

export function getGroups() {
  const dev = devPersonaRecord()
  if (dev) return dev.groups
  const p = decodeIdTokenPayload()
  return Array.isArray(p?.['cognito:groups']) ? p['cognito:groups'] : []
}

export function getEmail() {
  const dev = devPersonaRecord()
  if (dev) return dev.email
  const p = decodeIdTokenPayload()
  return p?.email || p?.['cognito:username'] || ''
}

export function signIn() {
  const url = cognitoLoginURL()
  if (url) window.location.href = url
}

export function signOut() {
  clear()
  const url = cognitoLogoutURL()
  if (url) window.location.href = url
}

// Exchange the authorization code for tokens. Call this once from /callback.
// Authorization codes are single-use; React StrictMode (dev) fires effects
// twice, which would consume the code on the first call and then 400 on the
// second. The module-level in-flight promise makes the second caller await
// the first exchange instead of re-POSTing.
let inflightCallback = null
export async function handleCallback() {
  if (inflightCallback) return inflightCallback
  inflightCallback = (async () => {
    const params = new URLSearchParams(window.location.search)
    const code = params.get('code')
    if (!code) return null
    const body = new URLSearchParams({
      grant_type: 'authorization_code',
      client_id: COGNITO.clientId,
      code,
      redirect_uri: COGNITO.redirectUri,
    })
    const resp = await fetch(`https://${COGNITO.domain}/oauth2/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body,
    })
    if (!resp.ok) {
      throw new Error(`Cognito token exchange failed: ${resp.status}`)
    }
    const j = await resp.json()
    save({
      id_token:      j.id_token,
      access_token:  j.access_token,
      refresh_token: j.refresh_token,
      // expires_in is in seconds; subtract 60 to renew a bit early.
      expires_at:    Date.now() + (j.expires_in - 60) * 1000,
    })
    // Drop the ?code= from the URL.
    window.history.replaceState({}, '', window.location.pathname)
    return j
  })()
  return inflightCallback
}

// Refresh the IdToken using the refresh_token. Returns the new id_token
// or empty string if refresh fails (caller should signIn() in that case).
export async function refresh() {
  const t = load()
  if (!t?.refresh_token) return ''
  const body = new URLSearchParams({
    grant_type: 'refresh_token',
    client_id: COGNITO.clientId,
    refresh_token: t.refresh_token,
  })
  const resp = await fetch(`https://${COGNITO.domain}/oauth2/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  })
  if (!resp.ok) { clear(); return '' }
  const j = await resp.json()
  save({
    ...t,
    id_token:     j.id_token,
    access_token: j.access_token,
    expires_at:   Date.now() + (j.expires_in - 60) * 1000,
  })
  return j.id_token
}
