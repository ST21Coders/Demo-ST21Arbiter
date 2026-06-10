import { useState } from 'react'
import { FlaskConical, Loader2, ArrowRight, CheckCircle, AlertTriangle, ShieldCheck, Info } from 'lucide-react'
import { dryRunScan } from '../hooks/useApi'
import { SeverityBadge } from '../components/SeverityBadge'

// Baseline observation snapshots — these MIRROR the master's scan fixtures
// (agents/master_orchestrator/agent.py _seed_zscaler/_seed_paloalto). Each
// preset overrides ONE source entirely; the master seeds the others normally,
// so they cancel out in the before/after diff and the delta isolates the change.
const ZSCALER_BASE = [
  { rule_id: 'ZIA-URLCAT-CLOUD-BLK-042',       action: 'BLOCK',          raw: { category: 'Cloud Storage', domains: ['dropbox.com'] } },
  { rule_id: 'ZIA-APP-CTRL-REMOTE-BLOCK-007',  action: 'BLOCK',          raw: { apps: ['TeamViewer', 'AnyDesk'] } },
  { rule_id: 'ZIA-APP-CTRL-BROWSER-FF-009',    action: 'BLOCK',          raw: { app: 'Firefox' } },
  { rule_id: 'ZIA-SSL-BYPASS-FIN-DOMAINS',     action: 'BYPASS_INSPECT', raw: { domains_count: 47, registered_exception: false } },
  { rule_id: 'ZPA-AUTHPOL-ADMIN-MFA-ONLY',     action: 'MFA_REQUIRED',   raw: { scope: 'Privileged Admins', non_admin_users_unprotected: 4200 } },
  { rule_id: 'ZIA-IOT-MONITOR-ONLY-VLAN-19',   action: 'MONITOR',        raw: { vlan: 19, devices: 43 } },
  { rule_id: 'ZIA-DLP-PII-BLOCK-ALL-EXTERNAL', action: 'BLOCK',          raw: { exceptions: [] } },
  { rule_id: 'ZPA-GEO-RESTRICT-INDIA-US-ONLY', action: 'ALLOW',          raw: { countries: ['IN', 'US'] } },
  { rule_id: 'ZIA-URLCAT-SOCIAL-BLOCK-ALL',    action: 'BLOCK',          raw: { department_exceptions: [] } },
  { rule_id: 'ZIA-URLCAT-ANONYMIZER-BLOCK',    action: 'BLOCK',          raw: { category: 'Anonymizer', apps: ['tor', 'ultrasurf'] } },
]
const PALOALTO_BASE = [
  { rule_id: 'PAN-SEC-EGRESS-ANYANY-ALLOW-001', action: 'ALLOW', raw: { action: 'allow', source_zone: 'trust', dest_zone: 'untrust', source: 'any', destination: 'any', application: 'any', service: 'any' } },
  { rule_id: 'PAN-SEC-APP-TOR-ALLOW-022', action: 'ALLOW', raw: { action: 'allow', source_zone: 'trust', dest_zone: 'untrust', application: ['tor', 'ultrasurf'], service: 'application-default' } },
  { rule_id: 'PAN-SEC-MGMT-DENY-EXTERNAL', action: 'DENY', raw: { action: 'deny', source_zone: 'untrust', dest_zone: 'mgmt', application: 'any', log: 'log-end' } },
]

// registered_exception=true on the SSL bypass rule → UC04 clears.
const ZSCALER_SSL_REGISTERED = ZSCALER_BASE.map(r =>
  r.rule_id === 'ZIA-SSL-BYPASS-FIN-DOMAINS'
    ? { ...r, raw: { ...r.raw, registered_exception: true } }
    : r
)
// Remove the Tor App-ID allow on the firewall → UC14 clears.
const PALOALTO_TOR_BLOCKED = PALOALTO_BASE.filter(r => r.rule_id !== 'PAN-SEC-APP-TOR-ALLOW-022')

const PRESETS = [
  {
    id: 'ssl-exception',
    title: 'Register an SSL-inspection exception',
    source: 'Zscaler',
    description: 'Mark the finance-domain SSL inspection bypass (ZIA-SSL-BYPASS-FIN-DOMAINS) as a registered policy exception before pushing it.',
    expect: 'UC04 — SSL inspection bypass should resolve.',
    base: { zscaler: ZSCALER_BASE },
    mutated: { zscaler: ZSCALER_SSL_REGISTERED },
  },
  {
    id: 'tor-block',
    title: 'Block Tor at the perimeter firewall',
    source: 'Palo Alto',
    description: 'Remove the PAN-OS App-ID rule that allows Tor/UltraSurf (PAN-SEC-APP-TOR-ALLOW-022) so the firewall stops contradicting the Zscaler anonymizer block.',
    expect: 'UC14 — Zscaler-vs-firewall anonymizer bypass should resolve.',
    base: { paloalto: PALOALTO_BASE },
    mutated: { paloalto: PALOALTO_TOR_BLOCKED },
  },
]

const conflictsOf = (findings) => (findings || []).filter(f => !f.compliant)
const keyOf = (f) => f.conflict_id || f.rule_key

export default function WhatIf() {
  const [activeId, setActiveId] = useState(null)
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)

  async function runPreset(preset) {
    setActiveId(preset.id)
    setRunning(true)
    setError(null)
    setResult(null)
    try {
      const [base, mutated] = await Promise.all([
        dryRunScan(preset.base),
        dryRunScan(preset.mutated),
      ])
      const baseC = conflictsOf(base.findings)
      const mutC = conflictsOf(mutated.findings)
      const mutKeys = new Set(mutC.map(keyOf))
      const baseKeys = new Set(baseC.map(keyOf))
      const resolved = baseC.filter(f => !mutKeys.has(keyOf(f)))
      const introduced = mutC.filter(f => !baseKeys.has(keyOf(f)))
      setResult({ preset, baseCount: baseC.length, mutCount: mutC.length, resolved, introduced })
    } catch (err) {
      setError(err.message || String(err))
    } finally {
      setRunning(false)
    }
  }

  return (
    <div className="p-6 space-y-5 page-container">
      {/* Header */}
      <div>
        <h1 className="text-lg font-bold text-slate-900 tracking-tight flex items-center gap-2">
          <FlaskConical size={18} className="text-indigo-600" /> What-If Scan
        </h1>
        <p className="text-xs text-slate-500 mt-0.5">
          Test a policy change before pushing it. Runs the deterministic rule pack against hypothetical
          observations — <span className="font-medium text-slate-700">nothing is written to findings</span>.
        </p>
      </div>

      <div className="flex items-start gap-2 rounded-lg border border-indigo-200 bg-indigo-50/50 px-3 py-2 text-xs text-indigo-800">
        <Info size={14} className="mt-0.5 flex-shrink-0" />
        <p>Each preset runs two dry-runs (baseline vs. proposed change) and shows the delta. This is a simulation — the live scan and your findings are untouched.</p>
      </div>

      {/* Presets */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        {PRESETS.map(p => {
          const isActive = activeId === p.id
          return (
            <div key={p.id}
                 className={`rounded-xl p-4 bg-white border transition-colors ${isActive ? 'border-indigo-300' : 'border-slate-200'}`}
                 style={{ boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }}>
              <div className="flex items-center gap-2 mb-1.5">
                <span className="text-xs px-2 py-0.5 rounded-md bg-slate-100 text-slate-600 border border-slate-200">{p.source}</span>
                <p className="text-sm font-semibold text-slate-900">{p.title}</p>
              </div>
              <p className="text-xs text-slate-600 mb-2">{p.description}</p>
              <p className="text-[11px] text-slate-500 flex items-center gap-1 mb-3">
                <ArrowRight size={11} /> Expected: {p.expect}
              </p>
              <button
                onClick={() => runPreset(p)}
                disabled={running}
                className="btn-primary flex items-center gap-1.5 text-xs"
              >
                {running && isActive ? <Loader2 size={13} className="animate-spin" /> : <FlaskConical size={13} />}
                {running && isActive ? 'Simulating…' : 'Run What-If'}
              </button>
            </div>
          )
        })}
      </div>

      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 flex items-center gap-2">
          <AlertTriangle size={14} /> What-If failed: {error}
        </div>
      )}

      {/* Result */}
      {result && (
        <div className="rounded-xl p-4 bg-white border border-slate-200 space-y-4"
             style={{ boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }}>
          <p className="text-[10px] text-slate-400 font-semibold uppercase tracking-wider">
            Simulation result · {result.preset.title}
          </p>

          {/* Before → After */}
          <div className="flex items-center gap-4">
            <div className="text-center">
              <p className="text-2xl font-bold text-slate-900 tabular-nums">{result.baseCount}</p>
              <p className="text-[11px] text-slate-500">conflicts now</p>
            </div>
            <ArrowRight size={18} className="text-slate-400" />
            <div className="text-center">
              <p className={`text-2xl font-bold tabular-nums ${result.mutCount < result.baseCount ? 'text-emerald-600' : 'text-slate-900'}`}>
                {result.mutCount}
              </p>
              <p className="text-[11px] text-slate-500">after change</p>
            </div>
            {result.baseCount - result.mutCount > 0 && (
              <span className="ml-2 text-xs px-2 py-1 rounded-md bg-emerald-50 text-emerald-700 border border-emerald-200 flex items-center gap-1">
                <CheckCircle size={12} /> {result.baseCount - result.mutCount} resolved
              </span>
            )}
          </div>

          {/* Resolved */}
          {result.resolved.length > 0 && (
            <div>
              <p className="text-[10px] text-emerald-600 font-semibold uppercase tracking-wider mb-1.5 flex items-center gap-1">
                <ShieldCheck size={12} /> Would resolve
              </p>
              <div className="space-y-1.5">
                {result.resolved.map(f => (
                  <div key={keyOf(f)} className="flex items-center gap-2 rounded-lg px-3 py-2 bg-emerald-50/50 border border-emerald-200">
                    <SeverityBadge severity={f.severity} />
                    <span className="text-xs font-mono text-slate-500">{f.rule_key}</span>
                    <span className="text-xs text-slate-700 truncate">{f.title}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Introduced (regressions) */}
          {result.introduced.length > 0 && (
            <div>
              <p className="text-[10px] text-red-600 font-semibold uppercase tracking-wider mb-1.5 flex items-center gap-1">
                <AlertTriangle size={12} /> Would introduce
              </p>
              <div className="space-y-1.5">
                {result.introduced.map(f => (
                  <div key={keyOf(f)} className="flex items-center gap-2 rounded-lg px-3 py-2 bg-red-50/50 border border-red-200">
                    <SeverityBadge severity={f.severity} />
                    <span className="text-xs font-mono text-slate-500">{f.rule_key}</span>
                    <span className="text-xs text-slate-700 truncate">{f.title}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {result.resolved.length === 0 && result.introduced.length === 0 && (
            <p className="text-xs text-slate-500">No change to the conflict set — this policy change has no effect on current findings.</p>
          )}
        </div>
      )}
    </div>
  )
}
