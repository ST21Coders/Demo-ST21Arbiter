// MCP sidebar cosmetic-only verification (task 11b, spec AC18).
//
// What this proves
// ----------------
// CLAUDE.md documents that the MCP server list in MCPChat.jsx is cosmetic:
// "the chat send always goes to the master AgentCore Runtime via sendChat().
// Don't wire sidebar selection to backend routing." Spec AC18 codifies this
// as a test: send the same prompt twice with two different sidebar
// selections, assert the outbound network payload to /chat is identical
// apart from non-load-bearing fields. A diverging payload is severity:high.
//
// How we capture the payload
// --------------------------
// We use `page.route('**/chat', ...)` to intercept the outbound POST to
// the Lambda Function URL. The handler:
//   1. Captures `route.request().postDataJSON()`.
//   2. Responds with a synthetic 200 body `{reply: '(intercepted)',
//      session_id: '<the same id the client sent>'}` so the SPA's
//      `sendChat()` resolves cleanly and the UI doesn't enter an error
//      state. This avoids hitting the real /chat (no Bedrock cost, no DDB
//      writes, no audit-log pollution).
//
// Payload normalization (which fields we strip)
// ---------------------------------------------
// The two payloads will legitimately differ on session-scoped fields:
//   - `session_id` is generated client-side via crypto.randomUUID() the
//     first time the user sends a message in a chat. We start a fresh
//     chat for each of the two sends, so each gets a distinct id. Not
//     load-bearing.
//   - We do NOT strip anything else by default. If the SPA grows future
//     fields like `request_id`, `client_timestamp`, or `nonce`, those go
//     into the strip list explicitly with a comment naming the field and
//     why. Today there is no such field — the body shape is exactly
//     `{prompt, session_id, chat_type}` per ui/src/hooks/useApi.js::sendChat.
//
// We assert the NORMALIZED payloads are deep-equal. If they aren't:
//   - Save both raw payloads + a diff under e2e/artifacts/.
//   - Push severity:high annotation.
//   - Fail the test with a message naming the diverging keys.
//
// Server selection mechanism (verified in MCPChat.jsx)
// ----------------------------------------------------
// The sidebar is a vertical list of clickable <button> elements rendered
// by the ServerListItem component (MCPChat.jsx lines 320-343). Each button
// shows the server name (e.g. "Policy Scanner MCP", "Conflict Detector MCP")
// in a span with class `text-xs font-semibold truncate`. The first server
// in MCP_SERVERS is selected by default on mount. We click the second
// (Conflict Detector MCP) to switch — its onClick calls setSelectedServer.
// Clicking a different sidebar entry resets the active session (see line
// 550: `onSelect={(s) => { setActiveSessionId(null); ... }}`), which is
// what we want — each send gets a fresh session_id.
//
// Two-pass flow
// -------------
//   Pass 1: Click the "+ New" chat button to guarantee a fresh session
//           (server #1 — Policy Scanner MCP — is the default selection,
//           so clicking the sidebar item again may no-op if React's
//           setState short-circuits on equal references; the "+ New"
//           button is the unambiguous session-reset affordance — see
//           ui/src/pages/MCPChat.jsx::newChat at line 461).
//           -> type fixed prompt -> submit -> capture payload1.
//   Pass 2: Click Server #2 (Conflict Detector MCP) — its onSelect
//           callback (MCPChat.jsx:550) already calls setActiveSessionId(null),
//           so this is itself a session reset.
//           -> type same fixed prompt -> submit -> capture payload2.
//
// Each pass asserts the captured payload carries a non-empty `session_id`.
// If the SPA stops generating one (or our intercept short-circuits before
// the client populates it), the cosmetic-only contract is meaningless —
// we'd be comparing two empty payloads — so we fail loudly with a clear
// message instead.
//
// Project scoping
// ---------------
// Runs under the `ciso` project only — MCPChat is CISO-only per the
// manifest (accessible_to: ['ciso'], blocked_for: ['soc','grc','employee']).
//
// Test id and result row
// ----------------------
// Test id is exactly `e2e.mcp-chat.sidebar-cosmetic` per spec AC18.
// target_kind: 'page', target_id: 'mcp-chat', persona: 'ciso'.
//
// Module system: ESM-style imports (matches other spec files).

import { test, expect } from '@playwright/test'
import path from 'node:path'
import fs from 'node:fs'

const RUN_DIR = process.env.RUN_DIR
  || path.resolve(__dirname, '..', '..', 'test-reports', '_local')
const ARTIFACTS_DIR = path.join(RUN_DIR, 'e2e', 'artifacts')

fs.mkdirSync(ARTIFACTS_DIR, { recursive: true })

// Fixed prompt used in both sends. Tagged with [harness] per CLAUDE.local.md
// risk #1 (so the dev team can identify test-originated entries even though
// we don't actually hit the real /chat here — the intercept short-circuits
// the request, so this tag is defense-in-depth for the case where the
// intercept silently breaks and the request leaks through).
const FIXED_PROMPT = '[harness] What is the current compliance status?'

// Server names as they appear in MCP_SERVERS in MCPChat.jsx. We click these
// by text — the buttons render the name inside the truncated span. Using
// `getByRole('button', {name: /.../})` matches the accessible name (which
// includes the server name text).
const SERVER_1_NAME = 'Policy Scanner MCP'    // default (no click needed)
const SERVER_2_NAME = 'Conflict Detector MCP' // we click this for pass 2

// Fields stripped from each payload before comparison. Documented above.
const STRIPPED_KEYS = ['session_id']

function normalizePayload(payload) {
  const copy = { ...payload }
  for (const key of STRIPPED_KEYS) {
    delete copy[key]
  }
  return copy
}

function diffKeys(a, b) {
  const allKeys = new Set([...Object.keys(a), ...Object.keys(b)])
  const diverging = []
  for (const k of allKeys) {
    if (JSON.stringify(a[k]) !== JSON.stringify(b[k])) {
      diverging.push({ key: k, in_first: a[k], in_second: b[k] })
    }
  }
  return diverging
}

test.describe('MCP sidebar cosmetic verification (AC18)', () => {
  test('e2e.mcp-chat.sidebar-cosmetic', async ({ page }, testInfo) => {
    // CISO is the only persona that can access /mcp-chat.
    test.skip(
      testInfo.project.name !== 'ciso',
      'mcp-chat is CISO-only — runs under the ciso project',
    )

    const testId = 'e2e.mcp-chat.sidebar-cosmetic'
    const evidencePath = `e2e/artifacts/${testId}-diff.json`

    testInfo.annotations.push({
      type: 'harness-result',
      description: JSON.stringify({
        target_kind: 'page',
        target_id: 'mcp-chat',
        persona: 'ciso',
        evidence_path: evidencePath,
      }),
    })
    testInfo.annotations.push({ type: 'severity', description: 'low' })

    let diverged = false
    let pass1Payload = null
    let pass2Payload = null

    try {
      // Intercept ALL outbound /chat requests for this test. The matcher
      // **/chat catches both the API Gateway path and the Lambda Function
      // URL form (CHAT_URL is the function URL base; sendChat appends
      // 'chat' as the path tail in useApi.js).
      const capturedPayloads = []
      await page.route('**/chat', async (route) => {
        const request = route.request()
        let body = null
        try {
          body = request.postDataJSON()
        } catch {
          // If the body isn't JSON, capture the raw text instead.
          body = { __raw__: request.postData() }
        }
        capturedPayloads.push(body)
        // Echo the session_id back if present (the SPA's sendChat() reads
        // `reply` and `session_id` from the response — see useApi.js:302).
        const sessionId = (body && body.session_id) || 'intercepted-session'
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            reply: '(intercepted by mcp-cosmetic test)',
            session_id: sessionId,
          }),
        })
      })

      // 1. Navigate to /mcp-chat as CISO.
      await page.goto('/mcp-chat')
      await page.waitForLoadState('domcontentloaded')

      // Sanity: the MCP Servers sidebar is visible (page mounted).
      await expect(page.getByText('MCP Servers', { exact: true }))
        .toBeVisible({ timeout: 10_000 })

      // ──────── PASS 1: server #1 (Policy Scanner MCP) ────────
      // Default selection on mount is MCP_SERVERS[0] = Policy Scanner MCP.
      // Clicking it again may no-op if React short-circuits on equal
      // references (W3 reviewer note). Instead, click the "+ New" chat
      // button — `newChat()` in MCPChat.jsx unambiguously calls
      // setActiveSessionId(null), guaranteeing a fresh session_id on the
      // next send.
      // "+ New" button: rendered at MCPChat.jsx:560-566 as a <button> with
      // title="Start a new chat" and visible text "New" (after a Plus icon).
      // The accessible name resolves to "New" (icon has no aria-label).
      await page.getByRole('button', { name: /^new$/i })
        .first()
        .click()

      // The input placeholder is `Query ${selectedServer.name}…` — wait
      // for it to reflect the active server.
      const input1 = page.getByPlaceholder(new RegExp(`query ${SERVER_1_NAME}`, 'i'))
      await expect(input1).toBeVisible({ timeout: 5_000 })

      await input1.fill(FIXED_PROMPT)
      await input1.press('Enter')

      // Wait for the intercepted request to land. The intercept handler
      // pushes into capturedPayloads synchronously when route.fulfill
      // resolves; we poll for length 1.
      await expect.poll(
        () => capturedPayloads.length,
        { message: 'pass-1 /chat request was not intercepted', timeout: 10_000 },
      ).toBeGreaterThanOrEqual(1)

      pass1Payload = capturedPayloads[0]

      // W3 contract: pass-1 must carry a non-empty `session_id`. If it
      // doesn't, the cosmetic-only check downstream is meaningless (we'd
      // be comparing two empty payloads). Fail loudly here so the bug is
      // obvious instead of producing a false-green diff.
      expect(
        pass1Payload && typeof pass1Payload.session_id === 'string' && pass1Payload.session_id.length > 0,
        `pass-1 payload missing session_id (got ${JSON.stringify(pass1Payload && pass1Payload.session_id)}); ` +
        `cosmetic-only comparison requires a real client-generated session id.`,
      ).toBe(true)

      // ──────── PASS 2: server #2 (Conflict Detector MCP) ────────
      // Clicking a different sidebar entry resets state. We confirm by
      // waiting for the placeholder to update to the new server's name.
      // Switching servers also calls setActiveSessionId(null) in the
      // onSelect handler (MCPChat.jsx:550), so we don't need to click
      // "+ New" again — the server switch IS the session reset.
      await page.getByRole('button', { name: new RegExp(SERVER_2_NAME, 'i') })
        .first()
        .click()

      const input2 = page.getByPlaceholder(new RegExp(`query ${SERVER_2_NAME}`, 'i'))
      await expect(input2).toBeVisible({ timeout: 5_000 })

      await input2.fill(FIXED_PROMPT)
      await input2.press('Enter')

      await expect.poll(
        () => capturedPayloads.length,
        { message: 'pass-2 /chat request was not intercepted', timeout: 10_000 },
      ).toBeGreaterThanOrEqual(2)

      pass2Payload = capturedPayloads[1]

      // Same session_id sanity check for pass 2. AND it must differ from
      // pass-1's session_id — same id across passes would mean the
      // session reset didn't actually fire, which would make the
      // cosmetic-only comparison a tautology on the session-tracking
      // fields.
      expect(
        pass2Payload && typeof pass2Payload.session_id === 'string' && pass2Payload.session_id.length > 0,
        `pass-2 payload missing session_id (got ${JSON.stringify(pass2Payload && pass2Payload.session_id)}); ` +
        `cosmetic-only comparison requires a real client-generated session id.`,
      ).toBe(true)
      expect(
        pass2Payload.session_id !== pass1Payload.session_id,
        `pass-1 and pass-2 share session_id '${pass1Payload.session_id}' — ` +
        `session reset between server switches did not fire. The cosmetic-only ` +
        `assertion below would be a tautology under this state.`,
      ).toBe(true)

      // ──────── Save both raw payloads ─────────────────────────
      fs.writeFileSync(
        path.join(ARTIFACTS_DIR, `${testId}-payload-1.json`),
        JSON.stringify(pass1Payload, null, 2) + '\n',
        'utf-8',
      )
      fs.writeFileSync(
        path.join(ARTIFACTS_DIR, `${testId}-payload-2.json`),
        JSON.stringify(pass2Payload, null, 2) + '\n',
        'utf-8',
      )

      // ──────── Normalize + compare ───────────────────────────
      const norm1 = normalizePayload(pass1Payload)
      const norm2 = normalizePayload(pass2Payload)
      const diverging = diffKeys(norm1, norm2)

      if (diverging.length > 0) {
        diverged = true
        // Save the diff as evidence.
        fs.writeFileSync(
          path.join(ARTIFACTS_DIR, `${testId}-diff.json`),
          JSON.stringify({
            stripped_keys: STRIPPED_KEYS,
            normalized_payload_1: norm1,
            normalized_payload_2: norm2,
            diverging_keys: diverging,
          }, null, 2) + '\n',
          'utf-8',
        )
        // Use a separate test.info annotation so the reporter has the
        // diff path even though the path is the same as evidencePath above.
      } else {
        // Even on PASS we write the diff file so the artifact always
        // exists at the path the harness-result annotation pointed at
        // (the reporter's AC20 check expects evidence_path to resolve).
        fs.writeFileSync(
          path.join(ARTIFACTS_DIR, `${testId}-diff.json`),
          JSON.stringify({
            stripped_keys: STRIPPED_KEYS,
            normalized_payload_1: norm1,
            normalized_payload_2: norm2,
            diverging_keys: [],
            verdict: 'identical (cosmetic-only confirmed)',
          }, null, 2) + '\n',
          'utf-8',
        )
      }

      // The actual assertion — fails with a clear message naming the
      // diverging keys when the contract breaks. Per spec AC18 a divergence
      // is severity:high (set below in finally).
      expect(
        diverging,
        diverging.length === 0
          ? '' // ignored when length is 0
          : `MCP sidebar selection appears to influence the /chat payload. ` +
            `Diverging keys: ${diverging.map((d) => d.key).join(', ')}. ` +
            `See ${evidencePath} for the full diff.`,
      ).toEqual([])
    } finally {
      if (diverged) {
        testInfo.annotations.push({ type: 'severity', description: 'high' })
      }
      // Screenshot as supplementary evidence. The primary evidence_path
      // is the diff JSON; this is for visual context.
      await page.screenshot({
        path: path.join(ARTIFACTS_DIR, `${testId}.png`),
        fullPage: true,
      }).catch(() => {})
    }
  })
})
