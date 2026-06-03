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

import { COGNITO, cognitoLoginURL, cognitoLogoutURL } from '../config'

const KEY = 'arbiter.tokens'

function load() {
  try { return JSON.parse(sessionStorage.getItem(KEY) || 'null') } catch { return null }
}
function save(t) { sessionStorage.setItem(KEY, JSON.stringify(t)) }
function clear() { sessionStorage.removeItem(KEY) }

export function isAuthenticated() {
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
  const p = decodeIdTokenPayload()
  return Array.isArray(p?.['cognito:groups']) ? p['cognito:groups'] : []
}

export function getEmail() {
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
