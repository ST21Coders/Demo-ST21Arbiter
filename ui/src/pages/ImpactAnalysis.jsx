import { useState } from 'react'
import {
  Network, Search, Loader2, Building2, GitBranch, ShieldCheck,
  FileText, AlertTriangle, ArrowDownRight, ArrowUpRight, ExternalLink, Boxes,
} from 'lucide-react'
import { runImpactAnalysis } from '../hooks/useApi'

/* Change-impact analysis against the ServiceNow CMDB + Change Management, via
   the servicenow_specialist runtime (POST /servicenow/impact-analysis). Given a
   changed AWS resource it shows: the matched CI, the blast radius (cmdb_rel_ci),
   the owning team (who does the work), the approver chain (who approves), and an
   optionally-drafted change_request. */

const EXAMPLES = [
  'alb-mig-prod-claims-api-001',
  'mig-prod-claims-data-primary',
  'pcx-mig-prod-dev-001',
]
const ENVIRONMENTS = ['DEV', 'STAGING', 'PRE_PROD', 'PROD']
const SEVERITIES = ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL']

const APPROVER_STATUS_CLS = {
  PENDING:  'bg-amber-50 text-amber-700 border-amber-200',
  APPROVED: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  NOTIFIED: 'bg-slate-100 text-slate-600 border-slate-200',
  REJECTED: 'bg-red-50 text-red-600 border-red-200',
}

function DirectionBadge({ direction }) {
  const downstream = direction === 'downstream'
  const Icon = downstream ? ArrowDownRight : ArrowUpRight
  return (
    <span className={`inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded-full border whitespace-nowrap ${
      downstream ? 'bg-rose-50 text-rose-600 border-rose-200' : 'bg-sky-50 text-sky-700 border-sky-200'
    }`}>
      <Icon size={10} /> {downstream ? 'impacted by change' : 'depended on'}
    </span>
  )
}

export default function ImpactAnalysis() {
  const [resource, setResource] = useState('')
  const [env, setEnv] = useState('PROD')
  const [severity, setSeverity] = useState('HIGH')
  const [draft, setDraft] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState(null)

  async function analyze(e) {
    e?.preventDefault()
    const r = resource.trim()
    if (!r) return
    setLoading(true); setError(''); setResult(null)
    try {
      const data = await runImpactAnalysis({ resource: r, target_environment: env, severity, draft_change: draft })
      setResult(data)
    } catch (err) {
      setError(err.message || String(err))
    } finally {
      setLoading(false)
    }
  }

  const affected = result?.affected_cis || []
  const approvers = result?.approver_chain || []
  const change = result?.change

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      {/* Header */}
      <div>
        <h1 className="text-xl font-bold text-slate-800 flex items-center gap-2">
          <Network size={20} className="text-indigo-600" /> Change Impact Analysis
        </h1>
        <p className="text-sm text-slate-500 mt-1">
          For a proposed IT-asset configuration change: what breaks, which team does the work, and which team approves —
          from the ServiceNow CMDB and Change Management.
        </p>
      </div>

      {/* Query form */}
      <form onSubmit={analyze} className="border border-slate-200 rounded-xl bg-white p-4 space-y-4">
        <div>
          <label className="text-xs font-medium text-slate-600">Changed AWS resource (id or ARN)</label>
          <div className="relative mt-1">
            <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400" />
            <input
              value={resource}
              onChange={e => setResource(e.target.value)}
              placeholder="e.g. alb-mig-prod-claims-api-001"
              className="input pl-8 w-full text-sm font-mono"
            />
          </div>
          <div className="flex flex-wrap gap-1.5 mt-2">
            {EXAMPLES.map(ex => (
              <button key={ex} type="button" onClick={() => setResource(ex)}
                className="text-[11px] font-mono px-2 py-0.5 rounded-full border border-slate-200 text-slate-500 hover:border-indigo-300 hover:text-indigo-600 transition-colors">
                {ex}
              </button>
            ))}
          </div>
        </div>

        <div className="flex flex-wrap items-end gap-4">
          <div>
            <label className="text-xs font-medium text-slate-600 block">Target environment</label>
            <select value={env} onChange={e => setEnv(e.target.value)} className="input mt-1 text-sm">
              {ENVIRONMENTS.map(x => <option key={x} value={x}>{x}</option>)}
            </select>
          </div>
          <div>
            <label className="text-xs font-medium text-slate-600 block">Severity</label>
            <select value={severity} onChange={e => setSeverity(e.target.value)} className="input mt-1 text-sm">
              {SEVERITIES.map(x => <option key={x} value={x}>{x}</option>)}
            </select>
          </div>
          <label className="flex items-center gap-2 text-xs text-slate-600 cursor-pointer pb-2">
            <input type="checkbox" checked={draft} onChange={e => setDraft(e.target.checked)} />
            Draft a ServiceNow change request
          </label>
          <button type="submit" disabled={loading || !resource.trim()}
            className="btn-primary inline-flex items-center gap-1.5 text-sm ml-auto disabled:opacity-50">
            {loading ? <Loader2 size={14} className="animate-spin" /> : <Network size={14} />}
            {loading ? 'Analyzing…' : 'Analyze impact'}
          </button>
        </div>
      </form>

      {error && (
        <div className="flex items-center gap-2 text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
          <AlertTriangle size={14} /> {error}
        </div>
      )}

      {result && (
        <div className="space-y-4">
          {result.configured === false && (
            <div className="flex items-center gap-2 text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
              <AlertTriangle size={13} /> {result.note || 'ServiceNow not configured — showing structure only.'}
            </div>
          )}

          {/* Changed resource + owner */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div className="border border-slate-200 rounded-xl bg-white p-4">
              <p className="text-[11px] font-bold uppercase tracking-wide text-slate-400 mb-2 flex items-center gap-1.5">
                <Boxes size={12} /> Changed configuration item
              </p>
              <p className="text-sm font-semibold text-slate-800 font-mono break-all">{result.changed_resource?.name || result.changed_resource?.input}</p>
              {result.changed_resource?.class && <p className="text-xs text-slate-500 mt-0.5">class: {result.changed_resource.class}</p>}
              {result.changed_resource?.correlation_id && <p className="text-[11px] text-slate-400 mt-0.5 font-mono break-all">{result.changed_resource.correlation_id}</p>}
              {result.note && result.configured !== false && <p className="text-xs text-amber-600 mt-2">{result.note}</p>}
            </div>
            <div className="border border-slate-200 rounded-xl bg-white p-4">
              <p className="text-[11px] font-bold uppercase tracking-wide text-slate-400 mb-2 flex items-center gap-1.5">
                <Building2 size={12} /> Owning team — who does the work
              </p>
              <p className="text-sm font-semibold text-slate-800">{result.owner_team || 'unassigned'}</p>
              <p className="text-xs mt-2">
                CAB approval:{' '}
                <span className={`font-medium ${result.cab_required ? 'text-amber-700' : 'text-emerald-700'}`}>
                  {result.cab_required ? 'required' : 'not required'}
                </span>
                <span className="text-slate-400"> · {result.target_environment} · {result.severity}</span>
              </p>
            </div>
          </div>

          {/* Blast radius */}
          <div className="border border-slate-200 rounded-xl bg-white p-4">
            <p className="text-[11px] font-bold uppercase tracking-wide text-slate-400 mb-3 flex items-center gap-1.5">
              <GitBranch size={12} /> Blast radius — affected configuration items ({affected.length})
            </p>
            {affected.length === 0 ? (
              <p className="text-sm text-slate-400 italic">No related CIs found in the CMDB.</p>
            ) : (
              <ul className="space-y-1.5">
                {affected.map((ci, i) => (
                  <li key={ci.sys_id || `${ci.name}-${i}`} className="flex items-center gap-2 flex-wrap text-sm">
                    <span className="font-mono text-slate-800">{ci.name}</span>
                    {ci.class && <span className="text-[11px] text-slate-400">[{ci.class}]</span>}
                    <DirectionBadge direction={ci.direction} />
                    {ci.via && <span className="text-[11px] text-slate-400">via {ci.via}</span>}
                    <span className="text-[10px] text-slate-300 ml-auto">hop {ci.depth}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>

          {/* Approver chain */}
          <div className="border border-slate-200 rounded-xl bg-white p-4">
            <p className="text-[11px] font-bold uppercase tracking-wide text-slate-400 mb-3 flex items-center gap-1.5">
              <ShieldCheck size={12} /> Approver chain — who approves
            </p>
            {approvers.length === 0 ? (
              <p className="text-sm text-emerald-700">Auto-approved — no human approvers required for {result.target_environment}.</p>
            ) : (
              <ul className="space-y-1.5">
                {approvers.map((a, i) => (
                  <li key={`${a.role}-${i}`} className="flex items-center gap-2 flex-wrap text-sm">
                    <span className="font-medium text-slate-800 capitalize">{(a.role || '').replace(/_/g, ' ')}</span>
                    {a.type === 'NOTIFICATION' && <span className="text-[10px] text-slate-400 uppercase">notification</span>}
                    <span className="text-xs text-slate-500">{a.email}</span>
                    <span className={`ml-auto text-[10px] font-medium px-2 py-0.5 rounded-full border ${APPROVER_STATUS_CLS[a.status] || 'bg-slate-100 text-slate-600 border-slate-200'}`}>
                      {a.status}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>

          {/* Drafted change request */}
          {change && (
            <div className="border border-indigo-200 rounded-xl bg-indigo-50/50 p-4">
              <p className="text-[11px] font-bold uppercase tracking-wide text-indigo-500 mb-2 flex items-center gap-1.5">
                <FileText size={12} /> Drafted change request
              </p>
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-sm font-semibold text-slate-800 font-mono">{change.number || '(no number)'}</span>
                {typeof result.affected_attached === 'number' && (
                  <span className="text-xs text-slate-500">· {result.affected_attached} affected CI(s) attached</span>
                )}
                {change.url && change.url !== '#' && (
                  <a href={change.url} target="_blank" rel="noreferrer"
                    className="ml-auto text-xs text-indigo-600 hover:underline inline-flex items-center gap-1">
                    Open in ServiceNow <ExternalLink size={11} />
                  </a>
                )}
              </div>
            </div>
          )}
        </div>
      )}

      {!result && !loading && !error && (
        <p className="text-sm text-slate-400 italic py-8 text-center">
          Enter a changed resource to see its blast radius, owning team, and required approvers.
        </p>
      )}
    </div>
  )
}
