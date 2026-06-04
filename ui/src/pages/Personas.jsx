import {
  User, BarChart3, ShieldAlert, Shield,
  MessageSquare, ScanLine, Activity, CheckCircle, ArrowRight,
  Lock, Unlock
} from 'lucide-react'
import { PERSONAS, usePersona } from '../contexts/PersonaContext'

const PERSONA_ICONS = {
  employee: User,
  grc:      BarChart3,
  soc:      ShieldAlert,
  ciso:     Shield,
}

const PERSONA_FLOWS = {
  employee: [
    { icon: MessageSquare, step: 'Open Analyst Chat', desc: 'Ask why a tool or site is blocked' },
    { icon: CheckCircle,   step: 'Get policy answer', desc: 'ARBITER explains the MIG policy reason' },
    { icon: ArrowRight,    step: 'Self-service',      desc: 'No helpdesk ticket needed' },
  ],
  grc: [
    { icon: ScanLine,      step: 'Run AI Scan',       desc: 'Dispatch agents across SharePoint, Zscaler, AWS' },
    { icon: Activity,      step: 'Review Findings',   desc: 'Filter by severity, domain, and status' },
    { icon: MessageSquare, step: 'Chatbot drill-down', desc: 'Query PCI exposure or policy contradiction' },
    { icon: CheckCircle,   step: 'Export for audit',  desc: 'Download CSV from Audit Logs' },
  ],
  soc: [
    { icon: Activity,      step: 'View Dashboard',    desc: 'Spot new critical detections' },
    { icon: ShieldAlert,   step: 'Open Finding',      desc: 'Expand remediation steps and impact' },
    { icon: MessageSquare, step: 'Investigate via chat', desc: 'Ask ARBITER about broader exposure' },
    { icon: ArrowRight,    step: 'Initiate Action',   desc: 'Create a change request to fix it' },
  ],
  ciso: [
    { icon: ShieldAlert,   step: 'Critical callout',  desc: 'Dashboard flags CISO-required approvals' },
    { icon: CheckCircle,   step: 'Action Center',     desc: 'Review approval chain and justification' },
    { icon: Shield,        step: 'Approve / Reject',  desc: 'Sign off on PROD CRITICAL changes' },
    { icon: BarChart3,     step: 'Compliance posture', desc: 'Review framework scores in Governance' },
  ],
}

const PRIMARY_ROUTE = {
  employee: '/analyst',
  grc:      '/findings',
  soc:      '/findings',
  ciso:     '/actions',
}

const ACCESS_LABEL = {
  employee: 'Chatbot only',
  grc:      'Full dashboard',
  soc:      'Dashboard + Actions',
  ciso:     'Full + Approvals',
}

export default function Personas() {
  const { persona: current, email } = usePersona()

  return (
    <div className="p-6 space-y-6 page-container">
      {/* Header */}
      <div>
        <h1 className="text-lg font-bold text-slate-900 tracking-tight">Personas & User Flows</h1>
        <p className="text-xs text-slate-500 mt-0.5">
          ARBITER serves four distinct roles. Your role is determined by your Cognito group membership and cannot be switched in-app.
        </p>
        {current ? (
          <p className="text-xs mt-2 inline-flex items-center gap-2 px-3 py-1.5 rounded-lg"
             style={{ background: `${current.color}10`, color: current.color, border: `1px solid ${current.color}33` }}>
            <Shield size={11} /> Signed in as <span className="font-semibold">{current.name}</span> ({current.role}) · <span className="font-mono">{email}</span>
          </p>
        ) : (
          <p className="text-xs mt-2 inline-flex items-center gap-2 px-3 py-1.5 rounded-lg bg-amber-50 text-amber-800 border border-amber-200">
            <Lock size={11} /> No persona group assigned for {email || 'this account'}. Contact your administrator.
          </p>
        )}
      </div>

      {/* Two interfaces callout */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        {[
          {
            title: 'Dashboard',
            desc: 'Structured work — trigger scans, review findings, analyze the heat map, filter by domain or severity, export for audits.',
            icon: Activity,
            color: { bg: '#eef2ff', border: '#c7d2fe', text: '#4338ca', accent: '#6366f1' },
          },
          {
            title: 'Analyst Chat',
            desc: 'Questions — ask why something is blocked, query PCI exposure, investigate a change, or get a quick posture summary.',
            icon: MessageSquare,
            color: { bg: '#ecfdf5', border: '#a7f3d0', text: '#047857', accent: '#10b981' },
          },
        ].map(({ title, desc, icon: Icon, color }) => (
          <div key={title} className="rounded-xl p-4 flex gap-4"
               style={{ background: color.bg, border: `1px solid ${color.border}`, borderLeft: `3px solid ${color.accent}` }}>
            <div className="w-9 h-9 rounded-lg flex items-center justify-center flex-shrink-0"
                 style={{ background: '#ffffff', border: `1px solid ${color.border}` }}>
              <Icon size={16} style={{ color: color.text }} />
            </div>
            <div>
              <p className="text-sm font-semibold text-slate-900">{title}</p>
              <p className="text-xs text-slate-600 mt-1 leading-relaxed">{desc}</p>
              <p className="text-xs mt-2" style={{ color: color.text }}>Both read from the same MCP servers — a finding in the dashboard can be explained in the chat.</p>
            </div>
          </div>
        ))}
      </div>

      {/* Persona cards */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {Object.values(PERSONAS).map(p => {
          const Icon = PERSONA_ICONS[p.id]
          const flows = PERSONA_FLOWS[p.id]
          const isActive = current?.id === p.id
          const isLimited = p.id === 'employee'

          return (
            <div key={p.id} className="rounded-xl overflow-hidden bg-white"
                 style={{
                   border: isActive ? `1px solid ${p.color}80` : '1px solid #e2e8f0',
                   borderLeft: isActive ? `3px solid ${p.color}` : `3px solid ${p.color}80`,
                   boxShadow: isActive ? `0 4px 12px ${p.color}18` : '0 1px 2px rgba(15,23,42,0.04)',
                 }}>
              {/* Card header */}
              <div className="p-4 flex items-start gap-3">
                <div className="w-10 h-10 rounded-full flex items-center justify-center text-sm font-bold text-white flex-shrink-0"
                     style={{ background: p.gradient }}>
                  {p.initials}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <p className="text-sm font-bold text-slate-900">{p.name}</p>
                    {p.badge && (
                      <span className="text-[9px] font-bold px-1.5 py-0.5 rounded uppercase tracking-wider"
                            style={{ background: `${p.color}14`, color: p.color, border: `1px solid ${p.color}33` }}>
                        {p.badge}
                      </span>
                    )}
                    {isActive && (
                      <span className="text-[9px] font-bold px-1.5 py-0.5 rounded uppercase tracking-wider bg-emerald-50 text-emerald-700 border border-emerald-200">
                        Active
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-slate-600 mt-0.5">{p.title}</p>
                  <p className="text-[10px] text-slate-400 font-mono mt-0.5">{p.email}</p>
                </div>
                <div className="flex items-center gap-1 flex-shrink-0"
                     style={{ color: isLimited ? '#b45309' : '#047857' }}>
                  {isLimited ? <Lock size={12} /> : <Unlock size={12} />}
                  <span className="text-[10px] font-medium">{ACCESS_LABEL[p.id]}</span>
                </div>
              </div>

              {/* Description */}
              <div className="px-4 pb-3 border-t border-slate-100">
                <p className="text-xs text-slate-600 leading-relaxed mt-3">{p.description}</p>
              </div>

              {/* Flow steps */}
              <div className="px-4 pb-4">
                <p className="text-[9px] text-slate-400 font-bold uppercase tracking-wider mb-2.5">Primary Flow</p>
                <div className="space-y-0">
                  {flows.map((f, i) => {
                    const FIcon = f.icon
                    return (
                      <div key={i} className="flex items-start gap-2.5">
                        <div className="flex flex-col items-center flex-shrink-0">
                          <div className="w-6 h-6 rounded-md flex items-center justify-center flex-shrink-0"
                               style={{ background: `${p.color}10`, border: `1px solid ${p.color}33` }}>
                            <FIcon size={12} style={{ color: p.color }} />
                          </div>
                          {i < flows.length - 1 && (
                            <div className="w-px flex-1 my-0.5" style={{ background: `${p.color}26`, minHeight: '12px' }} />
                          )}
                        </div>
                        <div className="pb-2.5">
                          <p className="text-xs font-medium text-slate-800">{f.step}</p>
                          <p className="text-[11px] text-slate-500">{f.desc}</p>
                        </div>
                      </div>
                    )
                  })}
                </div>
              </div>

              {/* Footer */}
              <div className="px-4 py-3 flex items-center justify-between border-t border-slate-100">
                <span className="text-[10px] text-slate-500 flex items-center gap-1.5">
                  <Icon size={11} style={{ color: p.color }} />
                  {p.role}
                </span>
                {isActive ? (
                  <span className="text-xs flex items-center gap-1 font-medium" style={{ color: p.color }}>
                    <CheckCircle size={12} /> This is your role
                  </span>
                ) : (
                  <span className="text-xs text-slate-400 flex items-center gap-1">
                    <Lock size={11} /> Not your role
                  </span>
                )}
              </div>
            </div>
          )
        })}
      </div>

      {/* Note */}
      <div className="rounded-xl px-4 py-3 flex items-center gap-2 bg-indigo-50 border border-indigo-200">
        <Shield size={12} className="text-indigo-600 flex-shrink-0" />
        <p className="text-xs text-indigo-700">
          Role is driven by the <span className="font-mono">cognito:groups</span> claim on your Cognito IdToken. To change roles you must sign out and sign in as a different user.
        </p>
      </div>
    </div>
  )
}
