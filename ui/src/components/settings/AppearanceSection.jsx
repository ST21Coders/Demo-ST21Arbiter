import { Layout, Gauge, Sparkles, Palette, Info } from 'lucide-react'
import { usePersona } from '../../contexts/PersonaContext'
import { usePreferences, setPreference, storageAvailable } from '../../hooks/usePreferences'
import { accessibleRoutes } from './routeAccess'
import SettingRow from '../SettingRow'
import SegmentedControl from '../SegmentedControl'
import Toggle from '../Toggle'

const cardStyle = { background: 'var(--surface)', border: '1px solid rgb(var(--c-slate-200))', boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }

export default function AppearanceSection() {
  const { persona } = usePersona()
  const prefs = usePreferences()
  const { landingPath, density, reduceMotion, theme } = prefs.appearance
  const canPersist = storageAvailable()

  // Landing-page options: the persona's accessible content pages, plus the
  // always-available Personas page. The leading "" option = auto (use the
  // role's default home).
  const landingOptions = persona ? [...accessibleRoutes(persona), { path: '/personas', label: 'Personas' }] : []

  return (
    <div className="space-y-5">
      {!canPersist && (
        <div className="rounded-xl px-4 py-3 flex items-start gap-2 bg-amber-50 border border-amber-200">
          <Info size={12} className="text-amber-600 mt-0.5 flex-shrink-0" />
          <p className="text-xs text-amber-700">Storage is unavailable in this browser — your preferences won't persist after you close the tab.</p>
        </div>
      )}

      <div className="rounded-xl p-4" style={cardStyle}>
        <p className="text-[10px] text-slate-500 font-semibold uppercase tracking-wider mb-1">Appearance & behavior</p>

        <SettingRow icon={Layout} label="Default landing page"
                    desc="Where you land after signing in and via the Home shortcut.">
          <select
            value={landingPath || ''}
            onChange={e => setPreference('appearance.landingPath', e.target.value || null)}
            className="text-sm rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-slate-800 focus:outline-none focus:ring-2 focus:ring-indigo-200"
          >
            <option value="">Auto (role default)</option>
            {landingOptions.map(({ path, label }) => (
              <option key={path} value={path}>{label}</option>
            ))}
          </select>
        </SettingRow>

        <SettingRow icon={Gauge} label="UI density"
                    desc="Compact tightens spacing on dense tables (Findings, Audit Logs).">
          <SegmentedControl
            value={density}
            onChange={v => setPreference('appearance.density', v)}
            options={[{ value: 'comfortable', label: 'Comfortable' }, { value: 'compact', label: 'Compact' }]}
          />
        </SettingRow>

        <SettingRow icon={Sparkles} label="Reduce motion"
                    desc="Minimize transitions and animations across the app.">
          <Toggle on={reduceMotion} onChange={v => setPreference('appearance.reduceMotion', v)} />
        </SettingRow>

        <SettingRow icon={Palette} label="Theme" last
                    desc="Switch between light and dark, or follow your operating system.">
          <SegmentedControl
            value={theme}
            onChange={v => setPreference('appearance.theme', v)}
            options={[
              { value: 'system', label: 'System' },
              { value: 'light',  label: 'Light' },
              { value: 'dark',   label: 'Dark' },
            ]}
          />
        </SettingRow>
      </div>
    </div>
  )
}
