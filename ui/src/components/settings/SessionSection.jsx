import { useEffect, useState } from 'react'
import { LogOut, Clock, Info, Globe } from 'lucide-react'
import { usePersona } from '../../contexts/PersonaContext'
import { signOut, getSessionExpiry } from '../../hooks/useAuth'
import SettingRow from '../SettingRow'

const cardStyle = { background: 'var(--surface)', border: '1px solid rgb(var(--c-slate-200))', boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }

function formatRemaining(ms) {
  if (ms <= 0) return 'expired'
  const totalSec = Math.floor(ms / 1000)
  const h = Math.floor(totalSec / 3600)
  const m = Math.floor((totalSec % 3600) / 60)
  const s = totalSec % 60
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}

export default function SessionSection() {
  const { persona, email } = usePersona()
  const expiry = getSessionExpiry()
  const [now, setNow] = useState(() => Date.now())

  // Tick once a second to drive the countdown. Cleaned up on unmount so
  // StrictMode's double-mount in dev doesn't leak intervals.
  useEffect(() => {
    if (!expiry) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [expiry])

  const remaining = expiry ? expiry - now : null

  return (
    <div className="space-y-5">
      <div className="rounded-xl p-4" style={cardStyle}>
        <p className="text-[10px] text-slate-500 font-semibold uppercase tracking-wider mb-1">Session</p>

        <SettingRow icon={Globe} label="Signed in as"
                    desc={persona ? persona.role : 'Unassigned'}>
          <span className="text-xs font-mono text-slate-600">{email || '—'}</span>
        </SettingRow>

        <SettingRow icon={Clock} label="Session expiry" last
                    desc="Your sign-in token refreshes automatically before it lapses.">
          {remaining == null
            ? <span className="text-xs text-slate-400">—</span>
            : <span className={`text-xs font-medium ${remaining <= 0 ? 'text-red-600' : 'text-slate-700'}`}>
                {remaining <= 0 ? 'Expired' : `expires in ${formatRemaining(remaining)}`}
              </span>}
        </SettingRow>
      </div>

      {/* Sign out */}
      <div className="rounded-xl p-4" style={cardStyle}>
        <div className="flex items-center justify-between gap-4">
          <div>
            <p className="text-sm text-slate-900 font-medium">Sign out</p>
            <p className="text-xs text-slate-500 mt-0.5">End your session on this device.</p>
          </div>
          <button
            onClick={signOut}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium text-white bg-slate-900 hover:bg-slate-800 transition-colors"
          >
            <LogOut size={13} /> Sign out
          </button>
        </div>

        <div className="mt-3 flex items-center justify-between gap-4 pt-3 border-t border-slate-100">
          <div>
            <p className="text-sm text-slate-400 font-medium">Sign out everywhere</p>
            <p className="text-xs text-slate-400 mt-0.5">Revoke your session on all devices.</p>
          </div>
          <button
            disabled
            title="Coming soon"
            className="px-3 py-1.5 rounded-lg text-sm font-medium text-slate-400 bg-slate-100 border border-slate-200 cursor-not-allowed"
          >
            Coming soon
          </button>
        </div>
      </div>

      {/* How to change role */}
      <div className="rounded-xl px-4 py-3 flex items-start gap-2 bg-slate-50 border border-slate-200">
        <Info size={12} className="text-slate-400 mt-0.5 flex-shrink-0" />
        <p className="text-xs text-slate-500">
          To use a different role, sign out and sign in as another user — roles can't be changed in-session.
        </p>
      </div>
    </div>
  )
}
