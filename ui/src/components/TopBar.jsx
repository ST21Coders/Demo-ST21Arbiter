import { useLocation, NavLink } from 'react-router-dom'
import { Bell, ChevronRight, Home, LogOut } from 'lucide-react'
import { USE_MOCK } from '../config'
import { usePersona } from '../contexts/PersonaContext'
import { signOut, isDevAuth, getDevPersonaId, setDevPersona } from '../hooks/useAuth'

const ROUTE_META = {
  '/':           { title: 'Dashboard',     section: null },
  '/findings':   { title: 'Findings',      section: 'Overview' },
  '/heatmap':    { title: 'System Map',    section: 'Overview' },
  '/actions':    { title: 'Action Center', section: 'Governance' },
  '/governance': { title: 'Compliance',    section: 'Governance' },
  '/audit':      { title: 'Audit Logs',    section: 'Governance' },
  '/analyst':    { title: 'Analyst Chat',  section: 'Intelligence' },
  '/llm-control':{ title: 'LLM Control',   section: 'Intelligence' },
  '/pipeline':   { title: 'Data Pipeline', section: 'Infrastructure' },
  '/data-grouping': { title: 'Data Grouping', section: 'Infrastructure' },
  '/mcp-chat':   { title: 'MCP Admin',     section: 'Infrastructure' },
  '/token-usage':{ title: 'Token Tracking', section: 'Governance' },
  '/personas':   { title: 'Personas',      section: 'Demo' },
  '/settings':   { title: 'Settings',      section: null },
}

export default function TopBar() {
  const location = useLocation()
  const { persona, firstAccessiblePath } = usePersona()
  const meta = ROUTE_META[location.pathname] || { title: 'ARBITER', section: null }
  const now = new Date()
  const homePath = firstAccessiblePath()

  return (
    <header className="h-11 flex items-center px-5 gap-4 flex-shrink-0 z-10 bg-white border-b border-slate-200">
      {/* Breadcrumb (Home icon is now a clickable shortcut to the user's home page) */}
      <div className="flex items-center gap-1.5 text-xs text-slate-500 flex-1 min-w-0">
        <NavLink
          to={homePath}
          title="Home"
          className="p-1 -ml-1 rounded hover:bg-slate-100 text-slate-400 hover:text-slate-700 transition-colors flex items-center"
        >
          <Home size={11} />
        </NavLink>
        {meta.section && (
          <>
            <ChevronRight size={10} className="text-slate-300" />
            <span className="text-slate-500">{meta.section}</span>
          </>
        )}
        <ChevronRight size={10} className="text-slate-300" />
        <span className="text-slate-800 font-medium">{meta.title}</span>
      </div>

      {/* Right side */}
      <div className="flex items-center gap-3 flex-shrink-0">
        {/* Mode badge */}
        <span className={`text-[9px] font-bold px-2 py-0.5 rounded uppercase tracking-wider ${
          USE_MOCK
            ? 'text-slate-500 border border-slate-200 bg-slate-50'
            : 'text-emerald-700 border border-emerald-200 bg-emerald-50'
        }`}>
          {USE_MOCK ? 'Mock Mode' : 'Live'}
        </span>

        {/* Dev persona switcher — local-host mock mode only; absent in any
            build with VITE_API_URL set. Reloads on change so PersonaContext
            re-derives from the new getGroups() return value. */}
        {isDevAuth() && (
          <select
            aria-label="Dev persona switcher"
            value={getDevPersonaId() || 'ciso'}
            onChange={(e) => { setDevPersona(e.target.value); window.location.reload() }}
            className="text-[10px] font-semibold px-1.5 py-0.5 rounded border border-amber-300 bg-amber-50 text-amber-800 uppercase tracking-wider"
            title="Switch persona (dev only — sets sessionStorage.arbiter.devPersona)"
          >
            <option value="ciso">CISO</option>
            <option value="soc">SOC</option>
            <option value="grc">GRC</option>
            <option value="employee">EMPLOYEE</option>
          </select>
        )}

        {/* Persona role badge */}
        {persona?.badge && (
          <span className="text-[9px] font-bold px-2 py-0.5 rounded uppercase tracking-wider"
                style={{ background: `${persona.color}14`, color: persona.color, border: `1px solid ${persona.color}33` }}>
            {persona.badge}
          </span>
        )}

        {/* Time */}
        <span className="text-[11px] text-slate-500 font-mono hidden lg:block tabular-nums">
          {now.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
        </span>

        {/* Notifications */}
        <button className="relative p-1.5 text-slate-500 hover:text-slate-900 hover:bg-slate-100 rounded-lg transition-colors">
          <Bell size={14} />
          <span className="absolute top-1 right-1 w-1.5 h-1.5 bg-red-500 rounded-full" />
        </button>

        {/* Active persona user */}
        {persona && (
          <div className="flex items-center gap-2">
            <div className="w-6 h-6 rounded-full flex items-center justify-center text-[10px] font-bold text-white flex-shrink-0"
                 style={{ background: persona.gradient }}>
              {persona.initials}
            </div>
            <div className="hidden lg:block">
              <p className="text-xs text-slate-800 leading-none">{persona.name}</p>
              <p className="text-[9px] text-slate-500 leading-none mt-0.5">{persona.title}</p>
            </div>
          </div>
        )}

        {/* Logout */}
        <button
          onClick={signOut}
          title="Sign out"
          className="flex items-center gap-1 px-2 py-1 text-[11px] text-slate-500 hover:text-slate-900 hover:bg-slate-100 rounded-md transition-colors"
        >
          <LogOut size={12} />
          <span className="hidden lg:inline">Logout</span>
        </button>
      </div>
    </header>
  )
}
