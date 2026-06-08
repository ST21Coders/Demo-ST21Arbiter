import { useState } from 'react'
import { Shield, AlertTriangle, Eye, Lock, ToggleLeft, ToggleRight, Info, CheckCircle } from 'lucide-react'
import { GUARDRAIL, AGENT_MODELS } from '../config'

// The four AgentCore runtimes actually deployed by scripts/deploy_agents.py.
// Models are mirrored from params/dev.json via config.js (default Nova 2 Lite).
const AGENTS = [
  { id: 'master',     name: 'Master Orchestrator', runtime: 'dev_st21arbiter_poc_master_orchestrator', model: AGENT_MODELS.master,     role: 'Conflict detection & specialist coordination' },
  { id: 'sharepoint', name: 'SharePoint Specialist', runtime: 'dev_st21arbiter_poc_sharepoint_specialist', model: AGENT_MODELS.sharepoint, role: 'Policy document analysis (SharePoint KB)' },
  { id: 'awsconfig',  name: 'AWSConfig Specialist', runtime: 'dev_st21arbiter_poc_awsconfig_specialist',  model: AGENT_MODELS.awsconfig,  role: 'Security group / IAM / S3 posture (AWS Config)' },
  { id: 'zscaler',    name: 'Zscaler Specialist',  runtime: 'dev_st21arbiter_poc_zscaler_specialist',   model: AGENT_MODELS.zscaler,    role: 'URL categorization & allowlist analysis (Zscaler ZIA)' },
]

// Denied topics (topicPolicyConfig) from scripts/setup_bedrock_kb.py.
const GUARDRAIL_TOPICS = [
  { id: 'cred_disclosure', label: 'Credential Disclosure',     desc: 'Requests asking the agent to reveal stored credentials, API keys, or secrets', active: true },
  { id: 'infra_destruct',  label: 'Infrastructure Destruction', desc: 'Requests to delete VPCs, subnets, production databases, or critical infrastructure', active: true },
  { id: 'politics',        label: 'Politics',                   desc: 'Requests for political opinions, endorsements, or partisan commentary', active: true },
]

// PII entities (sensitiveInformationPolicyConfig) from scripts/setup_bedrock_kb.py.
const PII_RULES = [
  { label: 'US Social Security Number', action: 'ANONYMIZE' },
  { label: 'Credit / Debit card',      action: 'ANONYMIZE' },
  { label: 'AWS Access Key',           action: 'BLOCK' },
  { label: 'AWS Secret Key',           action: 'BLOCK' },
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
  const [version, setVersion] = useState(GUARDRAIL.version)

  function toggleTopic(id) {
    setTopics(prev => prev.map(t => t.id === id ? { ...t, active: !t.active } : t))
  }

  const cardStyle = { background: '#ffffff', border: '1px solid #e2e8f0', boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }

  return (
    <div className="p-6 space-y-5 page-container">
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
              <p className="text-xs text-slate-600">{GUARDRAIL.name}</p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-1.5 text-xs text-slate-600">
              <span className="text-slate-500">Version</span>
              <select
                value={version}
                onChange={(e) => setVersion(e.target.value)}
                className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs font-mono text-slate-800 focus:outline-none focus:ring-1 focus:ring-indigo-400"
              >
                {GUARDRAIL.versions.map(v => (
                  <option key={v} value={v}>{v}</option>
                ))}
              </select>
            </label>
            <Toggle on={guardrailEnabled} onChange={setGuardrailEnabled} />
          </div>
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
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-slate-900">{agent.name}</p>
                <p className="text-xs text-slate-500 mt-0.5">{agent.role}</p>
                <p className="text-[10px] font-mono text-slate-400 mt-0.5 truncate">{agent.runtime}</p>
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
          Toggles and the version selector update local state only. Guardrail id/version and per-agent
          models are sourced from Infra/params/dev.json at build time; to change a foundation model,
          edit dev.json and re-run scripts/deploy_agents.py.
        </p>
      </div>
    </div>
  )
}
