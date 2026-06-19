import { useCallback, useEffect, useRef, useState } from 'react'
import { ChevronDown, Loader2, Trash2 } from 'lucide-react'

// Prefixes the adversarial harness uses when minting session ids. The "harness
// chats only" scope matches a sidebar row whose `session_id` startsWith any of
// these. Kept as a flat array (not a regex) so the rules are auditable and easy
// to extend if a future harness introduces a new prefix.
export const HARNESS_PREFIXES = ['harness-', 'features-', 'logic-race-']

// 30 days in ms — single fixed N per spec; no submenu, no freeform input.
const THIRTY_DAYS_MS = 30 * 24 * 60 * 60 * 1000

// Window between bulk-delete completion and toast auto-dismissal.
const TOAST_DISMISS_MS = 4000

export function isHarnessId(id) {
  if (typeof id !== 'string' || id.length === 0) return false
  for (const p of HARNESS_PREFIXES) {
    if (id.startsWith(p)) return true
  }
  return false
}

// Returns true iff session.created_at parses to a valid date that is strictly
// more than 30 days before `nowMs`. Missing/unparseable values return false so
// rows with bad metadata are never swept into a destructive scope.
export function isOlderThan30Days(session, nowMs) {
  const raw = session && session.created_at
  if (!raw) return false
  const t = Date.parse(raw)
  if (Number.isNaN(t)) return false
  return (nowMs - t) > THIRTY_DAYS_MS
}

// Pure: given the sidebar's currently loaded sessions, partition them into the
// three scopes the dropdown offers. Returns full session objects (not just ids)
// so callers can render counts and map to ids as needed.
export function computeScopes(sessions, nowMs) {
  const list = Array.isArray(sessions) ? sessions : []
  const all = list.slice()
  const harness = list.filter(s => isHarnessId(s && s.session_id))
  const old = list.filter(s => isOlderThan30Days(s, nowMs))
  return { all, harness, old }
}

// Confirm-message wording reflects that delete is now server-scoped: it sweeps
// every matching session the user owns, including ones the sidebar (Limit=50)
// hasn't loaded. Visible counts may understate what's actually deleted.
function confirmMessage(scopeKey) {
  if (scopeKey === 'all') {
    return 'Delete all of your chats? This includes chats not currently loaded in the sidebar. This cannot be undone.'
  }
  if (scopeKey === 'harness') {
    return 'Delete all of your harness chats? This includes harness chats not currently loaded in the sidebar. This cannot be undone.'
  }
  return 'Delete all of your chats older than 30 days? This includes chats not currently loaded in the sidebar. This cannot be undone.'
}

// Map a scope key to the API call payload — { scope, days? }.
function scopePayload(scopeKey) {
  if (scopeKey === 'old') return { scope: 'older_than_days', days: 30 }
  if (scopeKey === 'harness') return { scope: 'harness' }
  return { scope: 'all' }
}

// Split-button + dropdown that bulk-deletes the signed-in user's chats. The
// dropdown's counts come from `sessions` (the sidebar's loaded slice) and are
// informational — actual deletion is server-scoped through onBulkDelete and
// hits every session the user owns matching that scope, including rows the
// sidebar hasn't loaded.
//
// `onBulkDelete(scopeKey, opts)` is wired by the parent to
// useConversations.bulkDeleteByScope; the parent picks the analyst/mcp slice,
// so this component never crosses the chat-type boundary.
//
// `onBulkDelete` is invoked in a loop until the server's `truncated` field
// goes false, draining users with more than BULK_DELETE_SCOPE_CAP matches.
export default function ClearChatsButton({
  sessions,
  onBulkDelete,
  onAfter,
  activeSessionId,
  onActiveDeleted,
}) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState(null) // { deleted: K, total: N }
  const wrapRef = useRef(null)
  const toastTimerRef = useRef(null)

  // Recompute scopes on every render — sessions is small (≤50 per the sidebar
  // cap) so the cost is negligible. We intentionally do NOT memoize: a memo
  // keyed only on `sessions` would freeze Date.now(), making the "older than
  // 30 days" count stale as time advances.
  const scopes = computeScopes(sessions, Date.now())

  // The button stays enabled even when the sidebar shows 0 sessions: a server
  // sweep may still find off-screen rows the sidebar didn't load. The confirm
  // dialog gates intent.
  const buttonDisabled = busy

  // Click-outside closes the dropdown. Mouse-down (not click) so a click that
  // started inside but released outside is still treated as inside, matching
  // common menu UX.
  useEffect(() => {
    if (!open) return
    function onDocDown(ev) {
      if (wrapRef.current && !wrapRef.current.contains(ev.target)) {
        setOpen(false)
      }
    }
    function onKey(ev) {
      if (ev.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDocDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDocDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  // Clean up any pending toast timer on unmount so we don't setState after
  // teardown (would emit a React warning under StrictMode).
  useEffect(() => {
    return () => {
      if (toastTimerRef.current) clearTimeout(toastTimerRef.current)
    }
  }, [])

  const runScope = useCallback(async (scopeKey) => {
    setOpen(false)
    // We allow the click through even when the visible count is 0: the server
    // may still have older off-screen rows. The confirm dialog gates intent.
    const ok = window.confirm(confirmMessage(scopeKey))
    if (!ok) return

    const payload = scopePayload(scopeKey)
    setBusy(true)
    // Drain in rounds until the server stops reporting truncated=true. The cap
    // is BULK_DELETE_SCOPE_CAP per call; users with more matches get cleared
    // across multiple round-trips here so the caller sees one logical action.
    const deleted = []
    const failed = []
    try {
      for (let round = 0; round < 50; round++) {
        const r = await onBulkDelete(payload.scope, payload)
        if (Array.isArray(r && r.deleted)) deleted.push(...r.deleted)
        if (Array.isArray(r && r.failed)) failed.push(...r.failed)
        if (!(r && r.truncated)) break
      }
    } catch (err) {
      // Total failure (network/HTTP). Sidebar untouched.
      window.alert('Bulk delete failed: ' + (err && err.message ? err.message : 'unknown error'))
      setBusy(false)
      return
    }

    // Partial-failure toast: small inline div, auto-dismiss. The full sidebar
    // reconciliation happens through onAfter() below — we never show a "fake"
    // success count.
    if (failed.length > 0) {
      const toastInfo = { deleted: deleted.length, total: deleted.length + failed.length }
      setToast(toastInfo)
      if (toastTimerRef.current) clearTimeout(toastTimerRef.current)
      toastTimerRef.current = setTimeout(() => setToast(null), TOAST_DISMISS_MS)
    }

    // If the open chat was nuked, reset the right pane before refreshing — the
    // refresh will repopulate sessions without it, and we want the active id
    // cleared synchronously so no orphaned messages render.
    if (activeSessionId && deleted.includes(activeSessionId)) {
      try {
        onActiveDeleted && onActiveDeleted()
      } catch (err) {
        // Don't unwind the delete flow if the parent's reset-active callback
        // throws — the rows are gone, the user-visible work is done, but
        // surface the bug to dev-tools instead of silently dropping it.
        console.error('ClearChatsButton: onActiveDeleted callback threw', err)
      }
    }

    try {
      onAfter && onAfter()
    } catch (err) {
      console.error('ClearChatsButton: onAfter callback threw', err)
    }
    setBusy(false)
  }, [scopes, onBulkDelete, onAfter, activeSessionId, onActiveDeleted])

  return (
    <div ref={wrapRef} className="relative inline-block">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        disabled={buttonDisabled}
        aria-haspopup="menu"
        aria-expanded={open}
        title="Clear chats"
        className={
          'flex items-center gap-0.5 text-[10px] transition-colors ' +
          (buttonDisabled
            ? 'text-slate-300 cursor-not-allowed'
            : 'text-slate-600 hover:text-red-600')
        }
      >
        {busy ? (
          <Loader2 size={10} className="animate-spin" />
        ) : (
          <Trash2 size={10} />
        )}
        <span>Clear</span>
        <ChevronDown size={9} />
      </button>

      {open && (
        <div
          // No role="menu" — we don't implement WAI-ARIA arrow-key navigation,
          // so the role would mislead screen readers. Plain div + Escape-to-close
          // + click-outside is the documented behavior.
          className="absolute right-0 top-full mt-1 z-20 w-52 bg-white border border-slate-200 rounded shadow-lg py-1 text-[11px]"
          data-testid="clear-chats-menu"
        >
          <ScopeItem
            label="All chats"
            count={scopes.all.length}
            onPick={() => runScope('all')}
          />
          <ScopeItem
            label="Harness chats"
            count={scopes.harness.length}
            onPick={() => runScope('harness')}
          />
          <ScopeItem
            label="Older than 30 days"
            count={scopes.old.length}
            onPick={() => runScope('old')}
          />
          <p className="px-2 pt-1 text-[9px] text-slate-400 border-t border-slate-100 mt-0.5">
            Counts reflect the sidebar; delete sweeps everything on the server.
          </p>
        </div>
      )}

      {toast && (
        <div
          role="status"
          className="absolute right-0 top-full mt-1 z-10 w-60 bg-amber-50 border border-amber-200 text-amber-800 rounded px-2 py-1 text-[10px]"
        >
          Deleted {toast.deleted} of {toast.total} chats. {toast.total - toast.deleted} could not be deleted.
        </div>
      )}
    </div>
  )
}

// `count` is shown only as a hint of what's currently in the sidebar — the
// item stays enabled even at 0 because the server may still hold matching rows.
function ScopeItem({ label, count, onPick }) {
  const disabled = false
  return (
    <button
      type="button"
      onClick={onPick}
      disabled={disabled}
      aria-disabled={disabled}
      title={disabled ? 'No chats match' : undefined}
      className={
        'block w-full text-left px-2 py-1 ' +
        (disabled
          ? 'text-slate-300 cursor-not-allowed'
          : 'text-slate-700 hover:bg-slate-100 cursor-pointer')
      }
    >
      {label} ({count})
    </button>
  )
}
