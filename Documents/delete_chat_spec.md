# Spec — Delete chat (finish the half-built per-chat delete)

**Status:** Draft (spec only — no implementation yet)
**Owner:** UI (`ui/`) + API (`Infra/functions/api_handler/`) + IAM (`Infra/templates/02-security.yaml`)
**Version target:** bump `APP_VERSION` in [`ui/src/config.js`](../ui/src/config.js) on ship, per project convention.

---

## 1. Problem

The per-chat **delete** feature is wired on the frontend but the implementation got lost in a merge/sync. Today, clicking the trash button (or "Resolve", or hitting auto-archive) throws `deleteSession is not a function` at runtime, because:

- The UI **calls** `deleteSession` — destructured at [`AnalystView.jsx:277`](../ui/src/pages/AnalystView.jsx#L277), invoked from `handleDeleteSession()` (lines 439–455), `handleResolve()` (459–471), and the CR auto-archive effect (330–336).
- The hook **does not provide it** — [`useApi.js:263-268`](../ui/src/hooks/useApi.js#L263-L268) has only a leftover comment describing `deleteSession`; the function body is gone and it is missing from the `return` at [`useApi.js:269-273`](../ui/src/hooks/useApi.js#L269-L273).
- The backend has **no DELETE route** — [`api_handler.py:157-174`](../Infra/functions/api_handler/api_handler.py#L157-L174) only routes `GET /conversations`, `GET /conversations/{id}`, `GET /conversations/{id}/messages`.
- The API Lambda role **cannot delete** — [`02-security.yaml:130-142`](../Infra/templates/02-security.yaml#L130-L142) (`Sid: DDBReadWrite`) grants `GetItem/PutItem/UpdateItem/Query/Scan` but **not** `DeleteItem`.

## 2. Goal

Clicking the trash icon on a chat in the analyst sidebar permanently removes that chat: it disappears from the sidebar immediately, is gone after refresh, and cannot be re-opened. "Resolve" and CR-driven auto-archive reuse the same path (they already call `deleteSession`).

Non-goals: bulk delete, soft-delete/undo, a confirmation modal redesign (the existing `window.confirm` stays), MCPChat delete (out of scope — MCPChat doesn't call `deleteSession`).

## 3. Design

Four pieces, top to bottom. Each mirrors an existing pattern in the same file so the diff reads like its neighbors.

### 3.1 IAM — add `DeleteItem` ([`02-security.yaml`](../Infra/templates/02-security.yaml))

Add `dynamodb:DeleteItem` to the `DDBReadWrite` statement (line ~136). Resource ARNs are unchanged (the table-level `table/${Environment}-${ProjectName}-*` ARN already covers the sessions table). This is a template change → requires `aws cloudformation validate-template` then a change-set deploy of `02-security` (per CLAUDE.local.md rules). **The route is dead until this lands** — without it, `DeleteItem` returns `AccessDenied`.

### 3.2 Backend — `DELETE /conversations/{id}` ([`api_handler.py`](../Infra/functions/api_handler/api_handler.py))

**Routing** — extend the existing `/conversations/` path-param block (lines 167–174) to handle `method == "DELETE"`:

```python
if not sub and method == "DELETE":
    return _handle_delete_conversation(event, session_id)
```

**Handler** — new `_handle_delete_conversation(event, session_id)`, modeled on `_handle_get_conversation` (lines 771–790):

1. Guard `sessions_table` configured → `_err(500)`.
2. Resolve `user_id = _caller_user_id(event)` → `_err(401)` if none.
3. Guard `session_id` present → `_err(400)`.
4. **Ownership check first** — `get_item` on `session_id`; if missing or `item["user_id"] != user_id`, return `_err(404)` (same as the read paths — never delete another user's row, never confirm existence of a row you don't own).
5. `sessions_table.delete_item(Key={"session_id": session_id})`.
6. **Memory drain — see Decision D1 below.**
7. Return `_ok({"deleted": True, "session_id": session_id})`.

Ownership matters because `session_id` is the only DDB key; without the check, any authenticated caller could delete any chat by id.

### 3.3 Frontend hook — implement + export `deleteSession` ([`useApi.js`](../ui/src/hooks/useApi.js))

Replace the dangling comment at lines 263–268 with the real `useCallback`, mirroring `loadMessages`/`addLocalSession`:

```js
const deleteSession = useCallback(async (sessionId) => {
  if (!sessionId) return
  // Optimistic: drop from the sidebar before the network call so it feels instant.
  setSessions(prev => prev.filter(s => s.session_id !== sessionId))
  if (USE_MOCK) { await sleep(150); return }
  try {
    await apiFetch(`/conversations/${encodeURIComponent(sessionId)}`, { method: 'DELETE' })
  } catch (err) {
    // On failure, the next list() pull restores the row (server is source of truth).
    console.warn('deleteSession failed:', err)
    throw err
  }
}, [])
```

Add `deleteSession` to the hook's `return` (lines 269–273). `apiFetch` already handles auth + 401 refresh; no change there.

### 3.4 Frontend — render a trash button next to each chat ([`AnalystView.jsx`](../ui/src/pages/AnalystView.jsx))

This is the piece that actually got lost. `handleDeleteSession` exists (line 441) and calls `ev.stopPropagation()`, but **it is never rendered** — there is no trash button in the chat list. The list at [lines 495-512](../ui/src/pages/AnalystView.jsx#L495-L512) renders each chat as a bare `<button>`.

Changes to the `sessions.map(...)` row:

1. **Convert the row from `<button>` to `<div>`** (clickable via `onClick={() => openSession(s.session_id)}`, keep the same classes + active-state highlight). A trash `<button>` cannot be nested inside a `<button>` — that's invalid HTML and React will warn. Add `group` to the row's className so the trash icon can fade in on hover.
2. **Add the trash button** inside the row, after the title/meta block: a small `<button>` with the `Trash2` icon (already imported, line 5), `onClick={(ev) => handleDeleteSession(s.session_id, ev)}`. Hidden-until-hover (`opacity-0 group-hover:opacity-100`) so the list stays clean, with `title="Delete chat"` for affordance. The existing `ev.stopPropagation()` in `handleDeleteSession` stops the row's open-handler from also firing.

`handleResolve` and the CR auto-archive effect already call `deleteSession` and reset the active session — no change there. Per-user isolation is already enforced server-side (`/conversations` filters by `user_id`); each user only lists/deletes their own chats.

## 4. Open decision (need your call)

**D1 — what happens to the conversation's messages in AgentCore Memory?**

The messages live in AgentCore Memory, keyed by `(actorId=user_id, sessionId)`, *not* in DynamoDB. Deleting the DDB index row makes the chat **unreachable** (both `GET /messages` and `GET /{id}` gate on the DDB row existing), but the raw events stay in Memory.

| Option | What it does | Cost |
|---|---|---|
| **A — Unlink only (recommended)** | Delete the DDB row only. Messages become permanently unreachable but remain in Memory until its retention expires. | Simplest. No new IAM. Matches "best-effort" intent. Chat is gone from the user's view. |
| **B — Hard drain** | Also loop `list_events` → `delete_event` to purge Memory. | Needs `bedrock-agentcore:DeleteEvent` added to IAM `AgentCoreInvoke` (line ~124), plus a paginated delete loop. True erasure. |

My recommendation: **A** for this demo — it satisfies "the chat is deleted" from every user-facing surface, adds no IAM/blast-radius, and the comment that got lost said "best-effort drains," implying drain was never load-bearing. We can add B later if data-retention/erasure is a real requirement.

## 5. Test plan

- **UI mock mode** (`VITE_API_URL` empty): trash a chat → vanishes from sidebar instantly; refresh keeps it gone within the mock session. No console error.
- **Hook unit** ([`ui/src/__tests__/`](../ui/src/__tests__/)): optimistic removal happens before the await; rejection path is swallowed by the caller.
- **Live**: with `02-security` redeployed — delete own chat → 200, gone after `list()` refetch, `GET /{id}` now 404. Delete a **non-owned** id → 404 (not 200, not 403-leak). Delete without `DeleteItem` IAM → confirm the pre-deploy `AccessDenied` to prove the IAM piece is required.
- **Regression**: `handleResolve` and CR auto-archive still clear the active session and reset to the greeting.

## 6. Files touched

| File | Change |
|---|---|
| [`Infra/templates/02-security.yaml`](../Infra/templates/02-security.yaml) | +`dynamodb:DeleteItem` in `DDBReadWrite` (+`DeleteEvent` if D1=B) |
| [`Infra/functions/api_handler/api_handler.py`](../Infra/functions/api_handler/api_handler.py) | DELETE route + `_handle_delete_conversation` |
| [`Infra/templates/06-api.yaml`](../Infra/templates/06-api.yaml) | `ConversationDelete` SAM `Event` (DELETE `/conversations/{session_id}`) — this API wires each method explicitly, no proxy catch-all. CORS `AllowMethods` already lists DELETE. |
| [`ui/src/hooks/useApi.js`](../ui/src/hooks/useApi.js) | implement + export `deleteSession` |
| [`ui/src/pages/AnalystView.jsx`](../ui/src/pages/AnalystView.jsx) | row `<button>`→`<div>`; add per-chat `Trash2` button wired to `handleDeleteSession` |
| [`ui/src/config.js`](../ui/src/config.js) | bump `APP_VERSION` |

## 7. Deploy / ship order

1. `02-security` validate + change-set deploy **first** (route is dead without `DeleteItem`).
2. `06-api` SAM deploy (new Lambda code).
3. UI build + sync (handled by `deploy.sh::post_deploy_ui`), or `npm run dev` for local verify in mock mode.
