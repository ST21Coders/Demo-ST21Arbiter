import { Link } from 'react-router-dom'
import { Info, Lock, ArrowRight } from 'lucide-react'
import { usePersona } from '../../contexts/PersonaContext'
import { getGroups } from '../../hooks/useAuth'
import { accessibleRoutes } from './routeAccess'

const cardStyle = { background: 'var(--surface)', border: '1px solid rgb(var(--c-slate-200))', boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }

// Account & Identity — strictly read-only.
//
// IMPORTANT: roles are immutable in-session (derived from the Cognito
// IdToken's group claim). There is intentionally NO control here that mutates
// the persona — no dropdown, no role picker, no button. The only way to act as
// a different role is to sign out and sign in as a different user. Do not add a
// persona-switching affordance to this section.
export default function AccountSection() {
  const { persona, email } = usePersona()
  const groups = getGroups()

  // Authenticated but assigned to no persona group — degrade gracefully,
  // mirroring the Sidebar PersonaBadge / AccessDenied copy.
  if (!persona) {
    return (
      <div className="space-y-5">
        <div className="rounded-xl p-4" style={cardStyle}>
          <div className="flex items-center gap-3">
            <div className="w-12 h-12 rounded-full bg-slate-200 flex items-center justify-center text-base text-slate-500 flex-shrink-0">?</div>
            <div className="min-w-0">
              <p className="text-sm font-semibold text-slate-900 truncate">{email || 'Unknown user'}</p>
              <p className="text-xs text-slate-500">Unassigned — no persona group</p>
            </div>
          </div>
          <div className="mt-4 rounded-lg px-3 py-2 flex items-start gap-2 bg-amber-50 border border-amber-200">
            <Lock size={12} className="text-amber-600 mt-0.5 flex-shrink-0" />
            <p className="text-xs text-amber-700">
              Your account is not assigned to a persona group, so most pages are unavailable.
              Roles are assigned by your administrator via Cognito groups and cannot be changed here.
            </p>
          </div>
        </div>
      </div>
    )
  }

  const routes = accessibleRoutes(persona)

  return (
    <div className="space-y-5">
      {/* Identity card */}
      <div className="rounded-xl p-4" style={cardStyle}>
        <div className="flex items-center gap-3">
          <div className="w-12 h-12 rounded-full flex items-center justify-center text-base font-bold text-white flex-shrink-0"
               style={{ background: persona.gradient }}>
            {persona.initials}
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <p className="text-sm font-semibold text-slate-900 truncate">{persona.name}</p>
              {persona.badge && (
                <span className="text-[9px] font-bold px-1.5 py-0.5 rounded uppercase tracking-wider flex-shrink-0"
                      style={{ background: `${persona.color}14`, color: persona.color, border: `1px solid ${persona.color}33` }}>
                  {persona.badge}
                </span>
              )}
            </div>
            <p className="text-xs text-slate-500">{persona.title}</p>
          </div>
        </div>

        <dl className="mt-4 grid grid-cols-1 sm:grid-cols-2 gap-3">
          <Field label="Role" value={persona.role} />
          <Field label="Email" value={email || '—'} mono />
          <Field label="Cognito groups">
            <div className="flex flex-wrap gap-1 mt-0.5">
              {groups.length
                ? groups.map(g => (
                    <span key={g} className="text-[11px] font-mono px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 border border-slate-200">{g}</span>
                  ))
                : <span className="text-xs text-slate-400">None</span>}
            </div>
          </Field>
        </dl>

        <p className="mt-3 text-xs text-slate-500 leading-relaxed">{persona.description}</p>
      </div>

      {/* Accessible pages */}
      <div className="rounded-xl p-4" style={cardStyle}>
        <p className="text-[10px] text-slate-500 font-semibold uppercase tracking-wider mb-3">Accessible pages</p>
        <div className="flex flex-wrap gap-1.5">
          {routes.map(({ path, label }) => (
            <span key={path} className="text-xs px-2 py-1 rounded-lg bg-slate-50 text-slate-700 border border-slate-200">{label}</span>
          ))}
          <span className="text-xs px-2 py-1 rounded-lg bg-slate-50 text-slate-700 border border-slate-200">Personas</span>
          <span className="text-xs px-2 py-1 rounded-lg bg-slate-50 text-slate-700 border border-slate-200">Settings</span>
        </div>
      </div>

      {/* Read-only / how-to-switch note */}
      <div className="rounded-xl px-4 py-3 flex items-start gap-2 bg-slate-50 border border-slate-200">
        <Info size={12} className="text-slate-400 mt-0.5 flex-shrink-0" />
        <p className="text-xs text-slate-500">
          Your role and access are assigned by your administrator via Cognito groups and cannot be
          changed here. To use a different role, sign out and sign in as another user.{' '}
          <Link to="/personas" className="text-indigo-600 hover:text-indigo-700 inline-flex items-center gap-0.5">
            View persona details <ArrowRight size={10} />
          </Link>
        </p>
      </div>
    </div>
  )
}

function Field({ label, value, children, mono }) {
  return (
    <div>
      <dt className="text-[10px] text-slate-400 font-semibold uppercase tracking-wider">{label}</dt>
      {children ?? <dd className={`text-sm text-slate-800 mt-0.5 ${mono ? 'font-mono text-xs' : ''}`}>{value}</dd>}
    </div>
  )
}
