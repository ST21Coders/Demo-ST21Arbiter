import { NavLink } from 'react-router-dom'
import {
  LayoutDashboard, ShieldAlert, Activity, Wrench, Scale,
  GitBranch, ScrollText, MessageSquare, Cpu, Terminal,
  Settings, Wifi, WifiOff, Shield, Users, Coins, Plug, FlaskConical,
  Network, FileText, FolderTree, ShieldCheck, Radar,
} from 'lucide-react'
import { CHAT_URL, USE_MOCK, APP_VERSION } from '../config'
import { usePersona } from '../contexts/PersonaContext'

// Badge counts come in as props from the Shell's single useNavCounts() poll
// (shared with the TopBar notifications bell). Static `badge` literals were
// removed — only badgeKey 'findings' / 'actions' here.
const NAV_GROUPS = [
  {
    label: 'OVERVIEW',
    items: [
      { to: '/',          icon: LayoutDashboard, label: 'Dashboard' },
      { to: '/findings',  icon: ShieldAlert,     label: 'Findings',     badgeKey: 'findings', badgeColor: 'bg-red-600' },
      { to: '/heatmap',   icon: Activity,        label: 'System Map' },
    ],
  },
  {
    label: 'GOVERNANCE',
    items: [
      { to: '/actions',     icon: Wrench,      label: 'Action Center', badgeKey: 'actions', badgeColor: 'bg-amber-500' },
      { to: '/governance',  icon: Scale,       label: 'Compliance' },
      { to: '/reports',     icon: FileText,    label: 'Reports' },
      { to: '/audit',       icon: ScrollText,  label: 'Audit Logs' },
      { to: '/token-usage', icon: Coins,       label: 'Token Tracking', adminOnly: true },
    ],
  },
  {
    label: 'INTELLIGENCE',
    items: [
      { to: '/analyst',         icon: MessageSquare, label: 'Analyst Chat' },
      { to: '/impact-analysis', icon: Network,       label: 'Impact Analysis' },
      { to: '/cmdb-drift',      icon: Radar,         label: 'CMDB Drift Scan' },
      { to: '/whatif',          icon: FlaskConical,  label: 'What-If Scan' },
      { to: '/llm-control',     icon: Cpu,           label: 'LLM Control', adminOnly: true },
    ],
  },
  {
    label: 'INFRASTRUCTURE',
    items: [
      { to: '/pipeline',     icon: GitBranch, label: 'Data Pipeline', adminOnly: true },
      { to: '/data-grouping', icon: FolderTree, label: 'Data Grouping', adminOnly: true },
      { to: '/config-drift', icon: ShieldCheck, label: 'Config Drift', adminOnly: true },
      { to: '/mcp-chat',     icon: Terminal,  label: 'MCP Admin',     adminOnly: true },
      { to: '/integrations', icon: Plug,      label: 'Integrations',  adminOnly: true },
    ],
  },
]

const PAGE_TITLES = {
  '/':           'Dashboard',
  '/findings':   'Findings',
  '/heatmap':    'System Map',
  '/actions':    'Action Center',
  '/governance': 'Compliance',
  '/reports':    'Reports',
  '/audit':      'Audit Logs',
  '/analyst':    'Analyst Chat',
  '/impact-analysis': 'Impact Analysis',
  '/cmdb-drift': 'CMDB Drift Scan',
  '/whatif':     'What-If Scan',
  '/llm-control':'LLM Control',
  '/pipeline':   'Data Pipeline',
  '/data-grouping': 'Data Grouping',
  '/config-drift': 'Config Drift',
  '/mcp-chat':   'MCP Admin',
  '/integrations':'Integrations',
  '/token-usage':'Token Tracking',
  '/personas':   'Personas',
  '/settings':   'Settings',
}

export { PAGE_TITLES }

// Read-only persona badge. Persona is fixed by the Cognito JWT (no switching).
function PersonaBadge() {
  const { persona, email } = usePersona()
  if (!persona) {
    // Signed-in user not in any persona group — degrade gracefully.
    return (
      <div className="w-full px-4 py-3 flex items-center gap-2.5 border-t border-slate-200">
        <div className="w-7 h-7 rounded-full bg-slate-200 flex items-center justify-center text-xs text-slate-500 flex-shrink-0">?</div>
        <div className="flex-1 min-w-0 text-left">
          <p className="text-xs font-medium text-slate-800 truncate">{email || 'Unknown user'}</p>
          <p className="text-[10px] text-slate-500 truncate">No persona group assigned</p>
        </div>
      </div>
    )
  }
  return (
    <NavLink
      to="/personas"
      className="w-full px-4 py-3 flex items-center gap-2.5 hover:bg-slate-50 transition-colors border-t border-slate-200"
      title="View your persona details"
    >
      <div className="w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold text-white flex-shrink-0"
           style={{ background: persona.gradient }}>
        {persona.initials}
      </div>
      <div className="flex-1 min-w-0 text-left">
        <p className="text-xs font-medium text-slate-800 truncate">{persona.name}</p>
        <p className="text-[10px] text-slate-500 truncate">{persona.role}</p>
      </div>
      {persona.badge && (
        <span className="text-[9px] font-bold px-1.5 py-0.5 rounded uppercase tracking-wider flex-shrink-0"
              style={{ background: `${persona.color}14`, color: persona.color, border: `1px solid ${persona.color}33` }}>
          {persona.badge}
        </span>
      )}
    </NavLink>
  )
}

export default function Sidebar({ findingsOpen = 0, actionsPending = 0 }) {
  const isLive = !!CHAT_URL && !USE_MOCK
  const { persona, hasAccess } = usePersona()
  const liveCounts = { findings: findingsOpen, actions: actionsPending }

  const visibleGroups = NAV_GROUPS
    .map(group => ({ ...group, items: group.items.filter(item => hasAccess(item.to)) }))
    .filter(group => group.items.length > 0)

  return (
    <aside className="w-60 flex-shrink-0 flex flex-col bg-white border-r border-slate-200">
      {/* Branding */}
      <div className="px-5 py-4 border-b border-slate-200">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0"
               style={{ background: 'linear-gradient(135deg, #4f46e5, #3730a3)' }}>
            <Shield size={15} className="text-white" />
          </div>
          <div>
            <div className="text-sm font-bold text-slate-900 tracking-widest">ARBITER</div>
            <div className="text-[10px] text-slate-500 leading-tight">AI Governance Engine</div>
          </div>
        </div>
        <div className="mt-3 flex items-center justify-between">
          <span className="text-[10px] text-slate-500">Meridian Insurance Group</span>
          <span className={`flex items-center gap-1 text-[9px] font-bold px-1.5 py-0.5 rounded ${
            isLive
              ? 'bg-emerald-50 text-emerald-700 border border-emerald-200'
              : 'bg-slate-50 text-slate-500 border border-slate-200'
          }`}>
            {isLive ? <Wifi size={8} /> : <WifiOff size={8} />}
            {isLive ? 'LIVE' : 'MOCK'}
          </span>
        </div>
      </div>

      {/* Nav */}
      <nav className="flex-1 overflow-y-auto py-3 px-3 space-y-5">
        {visibleGroups.map(group => (
          <div key={group.label}>
            <p className="text-[9px] font-bold uppercase tracking-[0.18em] text-slate-400 px-2 mb-1.5 select-none">
              {group.label}
            </p>
            <div className="space-y-0.5">
              {group.items.map(({ to, icon: Icon, label, badgeKey, badgeColor, adminOnly }) => {
                const badgeValue = badgeKey ? liveCounts[badgeKey] : null
                return (
                  <NavLink
                    key={to}
                    to={to}
                    end={to === '/'}
                    className={({ isActive }) =>
                      `flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm transition-all duration-150 group relative ${
                        isActive
                          ? 'text-indigo-700 font-medium bg-indigo-50'
                          : 'text-slate-600 hover:text-slate-900 hover:bg-slate-50'
                      }`
                    }
                    style={({ isActive }) => isActive ? {
                      borderLeft: '2px solid #6366f1',
                      paddingLeft: '10px',
                    } : {
                      borderLeft: '2px solid transparent',
                    }}
                  >
                    <Icon size={14} className="flex-shrink-0" />
                    <span className="flex-1 truncate text-[13px]">{label}</span>
                    {adminOnly && (
                      <span className="text-[8px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-700 border border-indigo-200">
                        ADMIN
                      </span>
                    )}
                    {badgeValue ? (
                      <span className={`text-[10px] font-bold ${badgeColor} text-white rounded-full min-w-4 h-4 px-1 flex items-center justify-center flex-shrink-0`}>
                        {badgeValue}
                      </span>
                    ) : null}
                  </NavLink>
                )
              })}
            </div>
          </div>
        ))}

        {/* Personas link */}
        <div>
          <p className="text-[9px] font-bold uppercase tracking-[0.18em] text-slate-400 px-2 mb-1.5 select-none">DEMO</p>
          <NavLink
            to="/personas"
            className={({ isActive }) =>
              `flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm transition-all duration-150 ${
                isActive ? 'font-medium' : 'text-slate-600 hover:text-slate-900 hover:bg-slate-50'
              }`
            }
            style={({ isActive }) => isActive
              ? persona
                ? { background: `${persona.color}14`, color: persona.color, borderLeft: `2px solid ${persona.color}`, paddingLeft: '10px' }
                : { background: '#e2e8f0', color: '#475569', borderLeft: '2px solid #94a3b8', paddingLeft: '10px' }
              : { borderLeft: '2px solid transparent' }
            }
          >
            <Users size={14} className="flex-shrink-0" />
            <span className="flex-1 truncate text-[13px]">Personas</span>
          </NavLink>
        </div>
      </nav>

      {/* Footer */}
      <div className="border-t border-slate-200">
        <div className="px-3 py-2">
          <NavLink
            to="/settings"
            className={({ isActive }) =>
              `flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm transition-all duration-150 ${
                isActive
                  ? 'text-indigo-700 font-medium bg-indigo-50'
                  : 'text-slate-600 hover:text-slate-900 hover:bg-slate-50'
              }`
            }
            style={({ isActive }) => isActive
              ? { borderLeft: '2px solid #6366f1', paddingLeft: '10px' }
              : { borderLeft: '2px solid transparent' }
            }
          >
            <Settings size={14} />
            <span className="text-[13px]">Settings</span>
          </NavLink>
        </div>

        {/* Persona (read-only — derived from Cognito group) */}
        <PersonaBadge />

        <p className="px-4 pb-3 text-[10px] text-slate-400 font-mono">v{APP_VERSION} · us-east-1</p>
      </div>
    </aside>
  )
}
