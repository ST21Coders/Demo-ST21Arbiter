import { BellOff, Info } from 'lucide-react'
import { usePersona } from '../../contexts/PersonaContext'
import { usePreferences, setPreference } from '../../hooks/usePreferences'
import SettingRow from '../SettingRow'
import Toggle from '../Toggle'

const cardStyle = { background: 'var(--surface)', border: '1px solid rgb(var(--c-slate-200))', boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }

// Each category renders only when relevant to the current persona, gated by the
// same capability map the rest of the app uses. An employee (analystChat only)
// sees just "Product announcements".
const CATEGORIES = [
  { key: 'criticalFindings', label: 'New critical findings',          desc: 'When a scan surfaces a new critical-severity finding.',     gate: (hasAccess) => hasAccess('/findings') },
  { key: 'crAwaitingMe',     label: 'Change requests awaiting me',    desc: 'When a change request needs your approval.',                gate: (hasAccess) => hasAccess('/actions') },
  { key: 'scanComplete',     label: 'Scan completion',                desc: 'When an AI scan you triggered finishes.',                   gate: (hasAccess) => hasAccess('/findings') },
  { key: 'pipelineSync',     label: 'Pipeline / KB sync status',      desc: 'When knowledge-base ingestion succeeds or fails.',          gate: (hasAccess) => hasAccess('/pipeline') },
  { key: 'announcements',    label: 'Product & system announcements', desc: 'Occasional notes about new features and maintenance.',       gate: () => true },
]

export default function NotificationsSection() {
  const { hasAccess } = usePersona()
  const prefs = usePreferences()
  const { notifications } = prefs
  const paused = notifications.paused

  const visible = CATEGORIES.filter(c => c.gate(hasAccess))

  return (
    <div className="space-y-5">
      {/* Master pause */}
      <div className="rounded-xl p-4" style={cardStyle}>
        <SettingRow icon={BellOff} label="Pause all in-app notifications" last
                    desc="Temporarily silence every category below.">
          <Toggle on={paused} onChange={v => setPreference('notifications.paused', v)} />
        </SettingRow>
      </div>

      {/* Categories */}
      <div className="rounded-xl p-4" style={cardStyle}>
        <p className="text-[10px] text-slate-500 font-semibold uppercase tracking-wider mb-1">Categories</p>
        {visible.map((c, i) => (
          <SettingRow key={c.key} label={c.label} desc={c.desc} last={i === visible.length - 1}>
            <Toggle
              on={!paused && notifications[c.key]}
              onChange={v => setPreference(`notifications.${c.key}`, v)}
              disabled={paused}
            />
          </SettingRow>
        ))}
      </div>

      <div className="rounded-xl px-4 py-3 flex items-start gap-2 bg-slate-50 border border-slate-200">
        <Info size={12} className="text-slate-400 mt-0.5 flex-shrink-0" />
        <p className="text-xs text-slate-500">
          In the POC, notifications are in-app only and preferences are stored in this browser.
          Email and Microsoft Teams delivery are planned.
        </p>
      </div>
    </div>
  )
}
