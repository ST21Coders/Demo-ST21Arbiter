// Settings page — layout host.
//
// Universal route (no ROUTE_ACCESS entry; reachable by every authenticated
// persona, like /personas). A left section rail drives a right content panel.
// The active section is mirrored in the URL hash (/settings#appearance) so
// links and refreshes are stable. Sections self-gate: Advanced renders only
// for the IT (platform-operator) capability.
//
// See Documents/settings_spec.md and Documents/settings_impl_plan.md.

import { useLocation, useNavigate } from 'react-router-dom'
import { User, Palette, Bell, ShieldCheck, Server, Wrench } from 'lucide-react'
import { usePersona } from '../contexts/PersonaContext'
import AccountSection from '../components/settings/AccountSection'
import AppearanceSection from '../components/settings/AppearanceSection'
import NotificationsSection from '../components/settings/NotificationsSection'
import SessionSection from '../components/settings/SessionSection'
import EnvironmentSection from '../components/settings/EnvironmentSection'
import AdvancedSection from '../components/settings/AdvancedSection'

const SECTIONS = [
  { id: 'account',       label: 'Account & Identity',      icon: User,        Component: AccountSection },
  { id: 'appearance',    label: 'Appearance',              icon: Palette,     Component: AppearanceSection },
  { id: 'notifications', label: 'Notifications',           icon: Bell,        Component: NotificationsSection },
  { id: 'session',       label: 'Session & Security',      icon: ShieldCheck, Component: SessionSection },
  { id: 'environment',   label: 'Workspace & Environment', icon: Server,      Component: EnvironmentSection },
  // Advanced is gated to roles that can reach LLM Control (CISO is admin).
  { id: 'advanced',      label: 'Advanced',                icon: Wrench,      Component: AdvancedSection,
    gate: (hasAccess) => hasAccess('/llm-control') },
]

export default function Settings() {
  const { hasAccess } = usePersona()
  const location = useLocation()
  const navigate = useNavigate()

  const visible = SECTIONS.filter(s => !s.gate || s.gate(hasAccess))

  // Active section from the hash; fall back to the first visible section when
  // the hash is empty, unknown, or points at a section hidden for this persona
  // (e.g. a non-IT user deep-linking to #advanced).
  const hashId = location.hash.replace(/^#/, '')
  const active = visible.find(s => s.id === hashId) || visible[0]
  const ActiveComponent = active.Component

  return (
    <div className="p-6 max-w-6xl">
      <div className="mb-5">
        <h1 className="text-lg font-bold text-slate-900 tracking-tight">Settings</h1>
        <p className="text-xs text-slate-500 mt-0.5">
          Your account, preferences, session, and workspace
        </p>
      </div>

      <div className="flex flex-col lg:flex-row gap-6">
        {/* Section rail */}
        <nav className="lg:w-56 flex-shrink-0">
          <div className="flex lg:flex-col gap-1 overflow-x-auto lg:sticky lg:top-0">
            {visible.map(({ id, label, icon: Icon }) => {
              const isActive = id === active.id
              return (
                <button
                  key={id}
                  onClick={() => navigate(`/settings#${id}`)}
                  className={`flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm transition-all duration-150 whitespace-nowrap flex-shrink-0 lg:w-full text-left ${
                    isActive
                      ? 'text-indigo-700 font-medium bg-indigo-50'
                      : 'text-slate-600 hover:text-slate-900 hover:bg-slate-50'
                  }`}
                  style={isActive
                    ? { borderLeft: '2px solid #6366f1', paddingLeft: '10px' }
                    : { borderLeft: '2px solid transparent' }}
                >
                  <Icon size={14} className="flex-shrink-0" />
                  <span className="text-[13px]">{label}</span>
                </button>
              )
            })}
          </div>
        </nav>

        {/* Content panel */}
        <div className="flex-1 min-w-0 space-y-5">
          <ActiveComponent />
        </div>
      </div>
    </div>
  )
}
