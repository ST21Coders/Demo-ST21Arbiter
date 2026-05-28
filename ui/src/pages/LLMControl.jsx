import { useState } from 'react'
import { Shield, AlertTriangle, Eye, Lock, ToggleLeft, ToggleRight, Info, CheckCircle } from 'lucide-react'

const AGENTS = [
  { id: 'master',      name: 'Master Orchestrator',  model: 'claude-sonnet-4-6-20251006-v1:0', role: 'Conflict detection & sub-agent coordination' },
  { id: 'doc',         name: 'Document Specialist',  model: 'claude-haiku-4-5-20251001-v1:0',  role: 'Policy document analysis (SharePoint)' },
  { id: 'net',         name: 'Network Specialist',   model: 'claude-haiku-4-5-20251001-v1:0',  role: 'Security group & VPC analysis (AWSConfig)' },
  { id: 'zsc',         name: 'Zscaler Specialist',   model: 'claude-haiku-4-5-20251001-v1:0',  role: 'URL categorization analysis (Zscaler ZIA)' },
  { id: 'iam',         name: 'IAM & Data Specialist', model: 'claude-haiku-4-5-20251001-v1:0', role: 'S3 / IAM policy analysis (AWSConfig)' },
  { id: 'reasoner',    name: 'Conflict Reasoner',    model: 'claude-sonnet-4-6-20251006-v1:0', role: 'Cross-domain conflict determination & scoring' },
  { id: 'remediation', name: 'Remediation Planner',  model: 'claude-sonnet-4-6-20251006-v1:0', role: 'Generates ordered remediation steps' },
]

const GUARDRAIL_TOPICS = [
  { id: 'sec_bypass',   label: 'Security Bypass',    desc: 'Blocks attempts to bypass security controls or guardrails', active: true },
  { id: 'audit_hiding', label: 'Audit Hiding',       desc: 'Blocks requests to suppress or falsify audit records',      active: true },
  { id: 'cred_sharing', label: 'Credential Sharing', desc: 'Blocks disclosure of credentials or access keys',           active: true },
]

const PII_RULES = [
  { label: 'Email address',  action: 'ANONYMIZE' },
  { label: 'SSN',            action: 'BLOCK' },
  { label: 'Credit card',    action: 'BLOCK' },
  { label: 'AWS Access Key', action: 'BLOCK' },
  { label: 'AWS Secret Key', action: 'BLOCK' },
  { label: 'Password',       action: 'BLOCK' },
]

function Toggle({ on, onChange, disabled }) {
  return (
    <button
      onClick={() => !disabled && onChange(!on)}
      disabled={disabled}
      className={`transition-colors ${disabled ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer'}`}
    >
      {on
        ? <ToggleRight size={22} className="text-indigo-600" />
        : <ToggleLeft  size={22} className="text-slate-300" />
      }
    </button>
  )
}

export default function LLMControl() {
  const [guardrailEnabled, setGuardrailEnabled] = useState(true)
  const [topics, setTopics] = useState(GUARDRAIL_TOPICS)

  function toggleTopic(id) {
    setTopics(prev => prev.map(t => t.id === id ? { ...t, active: !t.active } : t))
  }

  const cardStyle = { background: '#ffffff', border: '1px solid #e2e8f0', boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }

  return (
    <div className="p-6 space-y-5 max-w-5xl">
      <div>
        <h1 className="text-lg font-bold text-slate-900 tracking-tight">LLM Control Panel</h1>
        <p className="text-xs text-slate-500 mt-0.5">Agent configuration, Bedrock Guardrails, and model selection</p>
      </div>

      {/* Guardrail master toggle */}
      <div className="rounded-xl p-4"
           style={guardrailEnabled
             ? { background: '#eef2ff', border: '1px solid #c7d2fe', borderLeft: '3px solid #6366f1' }
             : { background: '#fef2f2',  border: '1px solid #fecaca',  borderLeft: '3px solid #ef4444' }
           }>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <Shield size={20} className={guardrailEnabled ? 'text-indigo-600' : 'text-red-600'} />
            <div>
              <p className="font-semibold text-slate-900 text-sm">Bedrock Guardrail</p>
              <p className="text-xs text-slate-600">mig-arbiter-guardrail · All LLM calls wrapped</p>
            </div>
          </div>
          <Toggle on={guardrailEnabled} onChange={setGuardrailEnabled} />
        </div>

        {!guardrailEnabled && (
          <div className="mt-3 flex items-center gap-2 rounded-lg px-3 py-2 bg-red-100 border border-red-200">
            <AlertTriangle size={13} className="text-red-600" />
            <p className="text-xs text-red-800">
              WARNING: Disabling guardrails removes all topic blocks and PII protection. POC demo only.
            </p>
          </div>
        )}
      </div>

      {/* Topic blocks */}
      <div className="rounded-xl p-4" style={cardStyle}>
        <p className="text-[10px] text-slate-500 font-semibold uppercase tracking-wider mb-3">Topic Blocks</p>
        <div className="space-y-0">
          {topics.map((t, i) => (
            <div key={t.id}
                 className={`flex items-center justify-between py-3 ${i < topics.length - 1 ? 'border-b border-slate-100' : ''}`}>
              <div>
                <p className="text-sm text-slate-900 font-medium">{t.label}</p>
                <p className="text-xs text-slate-500">{t.desc}</p>
              </div>
              <Toggle on={t.active} onChange={() => toggleTopic(t.id)} disabled={!guardrailEnabled} />
            </div>
          ))}
        </div>
      </div>

      {/* PII protection */}
      <div className="rounded-xl p-4" style={cardStyle}>
        <div className="flex items-center gap-2 mb-3">
          <Eye size={13} className="text-slate-500" />
          <p className="text-[10px] text-slate-500 font-semibold uppercase tracking-wider">PII Protection</p>
        </div>
        <div className="grid grid-cols-2 lg:grid-cols-3 gap-2">
          {PII_RULES.map(r => (
            <div key={r.label} className="flex items-center gap-2 rounded-lg px-3 py-2 bg-slate-50 border border-slate-200">
              <Lock size={11} className="text-slate-400 flex-shrink-0" />
              <div className="flex-1 min-w-0">
                <p className="text-xs text-slate-700">{r.label}</p>
                <p className={`text-xs font-semibold ${r.action === 'BLOCK' ? 'text-red-700' : 'text-amber-700'}`}>
                  {r.action}
                </p>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Agent registry */}
      <div className="rounded-xl p-4" style={cardStyle}>
        <p className="text-[10px] text-slate-500 font-semibold uppercase tracking-wider mb-3">Agent Registry</p>
        <div className="space-y-0">
          {AGENTS.map((agent, i) => (
            <div key={agent.id}
                 className={`flex items-start gap-3 py-3 ${i < AGENTS.length - 1 ? 'border-b border-slate-100' : ''}`}>
              <div className="w-1.5 h-1.5 rounded-full bg-emerald-500 mt-2 flex-shrink-0" />
              <div className="flex-1">
                <p className="text-sm font-medium text-slate-900">{agent.name}</p>
                <p className="text-xs text-slate-500 mt-0.5">{agent.role}</p>
              </div>
              <div className="text-right flex-shrink-0">
                <p className="text-xs font-mono text-indigo-700 truncate max-w-[240px]">{agent.model}</p>
                <p className="text-xs text-emerald-700 mt-0.5 flex items-center justify-end gap-1">
                  <CheckCircle size={10} /> active
                </p>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Note */}
      <div className="rounded-xl px-4 py-3 flex items-start gap-2 bg-slate-50 border border-slate-200">
        <Info size={12} className="text-slate-400 mt-0.5 flex-shrink-0" />
        <p className="text-xs text-slate-500">
          In POC mode these toggles update local state only. Once connected to AWS, changes will call the
          Bedrock Guardrails API to update the live guardrail version and SSM Parameter Store for model overrides.
        </p>
      </div>
    </div>
  )
}
