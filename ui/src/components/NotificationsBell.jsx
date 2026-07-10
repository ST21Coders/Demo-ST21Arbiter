import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Bell } from 'lucide-react'
import { usePersona } from '../contexts/PersonaContext'
import { SeverityBadge } from './SeverityBadge'

export const MAX_NOTIFICATIONS = 6

// Millisecond timestamp for ordering. Missing/unparseable detected_at maps to
// -1 so rows with bad metadata sink below every dated finding instead of
// poisoning the sort with NaN.
function detectedMs(f) {
  const t = Date.parse(f && f.detected_at)
  return Number.isNaN(t) ? -1 : t
}

// Pure: the newest-first slice of open findings shown in the bell panel.
// Ordering is by detected_at descending only — severity is rendered as a badge
// on each row but deliberately does not affect position (product decision:
// the panel behaves like a feed, not a triage queue).
export function pickTopNotifications(findings, n = MAX_NOTIFICATIONS) {
  const list = Array.isArray(findings) ? findings : []
  return list
    .filter(f => f && f.status === 'OPEN')
    .slice()
    .sort((a, b) => detectedMs(b) - detectedMs(a))
    .slice(0, n)
}

// Compact relative timestamp for a notification row. Empty string on bad
// input so a malformed detected_at renders as no timestamp, not "NaN ago".
export function timeAgo(iso, nowMs = Date.now()) {
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return ''
  const s = Math.max(0, Math.floor((nowMs - t) / 1000))
  if (s < 60) return 'just now'
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

// TopBar bell + notifications popover. `openFindings` is the OPEN findings
// slice from useNavCounts (polled once at the Shell level) so opening the
// panel costs zero requests. Hidden entirely for personas that can reach
// neither Findings nor the Action Center (i.e. employee) — a notification
// they can't click through to is worse than no bell.
export default function NotificationsBell({ openFindings = [] }) {
  const { hasAccess } = usePersona()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const wrapRef = useRef(null)

  // Click-outside closes the panel. Mouse-down (not click) so a click that
  // started inside but released outside still counts as inside — same
  // semantics as ClearChatsButton's dropdown.
  useEffect(() => {
    if (!open) return
    function onDocDown(ev) {
      if (wrapRef.current && !wrapRef.current.contains(ev.target)) setOpen(false)
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

  const canSeeFindings = hasAccess('/findings')
  if (!canSeeFindings && !hasAccess('/actions')) return null

  const items = pickTopNotifications(openFindings)
  const goTo = (path) => { setOpen(false); navigate(path) }

  return (
    <div ref={wrapRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        title="Notifications"
        className="relative p-1.5 text-slate-500 hover:text-slate-900 hover:bg-slate-100 rounded-lg transition-colors"
      >
        <Bell size={14} />
        {openFindings.length > 0 && (
          <span data-testid="notifications-dot" className="absolute top-1 right-1 w-1.5 h-1.5 bg-red-500 rounded-full" />
        )}
      </button>

      {open && (
        <div
          // No role="menu" — no arrow-key navigation is implemented, so the
          // role would mislead screen readers (matches ClearChatsButton).
          data-testid="notifications-panel"
          className="absolute right-0 top-full mt-1 z-20 w-80 bg-white border border-slate-200 rounded-lg shadow-lg"
        >
          <div className="flex items-center justify-between px-3 py-2 border-b border-slate-100">
            <p className="text-xs font-semibold text-slate-800">Notifications</p>
            <span className="text-[10px] text-slate-500">
              {openFindings.length} open finding{openFindings.length === 1 ? '' : 's'}
            </span>
          </div>

          {items.length === 0 ? (
            <p className="px-3 py-4 text-[11px] text-slate-500">No open findings.</p>
          ) : (
            <ul className="max-h-80 overflow-y-auto py-1">
              {items.map(f => (
                <li key={f.conflict_id}>
                  <button
                    type="button"
                    onClick={() => canSeeFindings && goTo(`/findings/${f.conflict_id}`)}
                    className="w-full text-left px-3 py-2 hover:bg-slate-50 transition-colors"
                  >
                    <div className="flex items-center gap-2">
                      <SeverityBadge severity={f.severity} />
                      <span className="text-[10px] text-slate-400 ml-auto flex-shrink-0">
                        {timeAgo(f.detected_at)}
                      </span>
                    </div>
                    <p className="text-[11px] text-slate-700 mt-1 line-clamp-2">{f.title}</p>
                  </button>
                </li>
              ))}
            </ul>
          )}

          {canSeeFindings && (
            <button
              type="button"
              onClick={() => goTo('/findings')}
              className="block w-full text-center px-3 py-2 text-[11px] text-indigo-600 hover:bg-slate-50 border-t border-slate-100 rounded-b-lg"
            >
              View all findings →
            </button>
          )}
        </div>
      )}
    </div>
  )
}
