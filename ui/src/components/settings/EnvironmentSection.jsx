import { useState } from 'react'
import { Server, Globe, Tag, Link2, Copy, Check } from 'lucide-react'
import { usePersona } from '../../contexts/PersonaContext'
import { USE_MOCK, APP_VERSION, COGNITO, API_URL, CHAT_URL } from '../../config'
import SettingRow from '../SettingRow'

const cardStyle = { background: 'var(--surface)', border: '1px solid rgb(var(--c-slate-200))', boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }

export default function EnvironmentSection() {
  const { persona, hasAccess } = usePersona()
  const showEndpoints = hasAccess('/llm-control')   // CISO (admin) only
  const [copied, setCopied] = useState(false)

  function copyDiagnostics() {
    // Deliberately excludes tokens and any secret material.
    const blob = {
      version: APP_VERSION,
      mode: USE_MOCK ? 'mock' : 'live',
      region: COGNITO.region,
      apiUrl: API_URL || null,
      chatUrl: CHAT_URL || null,
      persona: persona?.id || null,
    }
    try {
      navigator.clipboard?.writeText(JSON.stringify(blob, null, 2))
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch { /* clipboard unavailable */ }
  }

  return (
    <div className="space-y-5">
      <div className="rounded-xl p-4" style={cardStyle}>
        <p className="text-[10px] text-slate-500 font-semibold uppercase tracking-wider mb-1">Workspace</p>

        <SettingRow icon={Server} label="Mode" desc="Whether the UI is talking to live AWS services or bundled mock data.">
          <span className={`text-[10px] font-bold px-2 py-0.5 rounded uppercase tracking-wider ${
            USE_MOCK ? 'text-slate-500 border border-slate-200 bg-slate-50' : 'text-emerald-700 border border-emerald-200 bg-emerald-50'
          }`}>
            {USE_MOCK ? 'Mock' : 'Live'}
          </span>
        </SettingRow>

        <SettingRow icon={Tag} label="App version">
          <span className="text-xs font-mono text-slate-600">v{APP_VERSION}</span>
        </SettingRow>

        <SettingRow icon={Globe} label="Region" last={!showEndpoints}>
          <span className="text-xs font-mono text-slate-600">{COGNITO.region}</span>
        </SettingRow>

        {showEndpoints && (
          <>
            <SettingRow icon={Link2} label="API endpoint">
              <span className="text-xs font-mono text-slate-600 truncate max-w-[260px] inline-block align-bottom">{API_URL || 'Not connected (mock)'}</span>
            </SettingRow>
            <SettingRow icon={Link2} label="Chat endpoint" last>
              <span className="text-xs font-mono text-slate-600 truncate max-w-[260px] inline-block align-bottom">{CHAT_URL || 'Not connected (mock)'}</span>
            </SettingRow>
          </>
        )}
      </div>

      {showEndpoints && (
        <div className="rounded-xl p-4 flex items-center justify-between gap-4" style={cardStyle}>
          <div>
            <p className="text-sm text-slate-900 font-medium">Copy diagnostics</p>
            <p className="text-xs text-slate-500 mt-0.5">Copy environment info (no tokens or secrets) for support.</p>
          </div>
          <button
            onClick={copyDiagnostics}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium text-slate-700 bg-slate-50 border border-slate-200 hover:bg-slate-100 transition-colors"
          >
            {copied ? <><Check size={13} className="text-emerald-600" /> Copied</> : <><Copy size={13} /> Copy</>}
          </button>
        </div>
      )}
    </div>
  )
}
