import { useEffect, useState } from 'react'
import { ChevronDown, ChevronRight, Loader2, ScanLine, ExternalLink, Zap } from 'lucide-react'
import { useFindings, useChangeRequests } from '../hooks/useApi'
import { SeverityBadge, StatusBadge, TypeBadge } from '../components/SeverityBadge'
import ActionRequestModal from '../components/ActionRequestModal'
import { formatDistanceToNow } from 'date-fns'

const SEVERITY_ORDER = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3 }

const SEV_BORDER = {
  CRITICAL: '#ef4444',
  HIGH:     '#f97316',
  MEDIUM:   '#f59e0b',
  LOW:      '#10b981',
}

export default function Findings() {
  const { findings, loading, scanning, load, runScan } = useFindings()
  const { createAction } = useChangeRequests()
  const [expanded, setExpanded] = useState(null)
  const [filterSev, setFilterSev] = useState('')
  const [filterStatus, setFilterStatus] = useState('')
  const [actionTarget, setActionTarget] = useState(null)

  useEffect(() => { load() }, [load])

  const filtered = findings
    .filter(f => !filterSev || f.severity === filterSev)
    .filter(f => !filterStatus || f.status === filterStatus)
    .sort((a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity])

  function toggle(id) {
    setExpanded(prev => prev === id ? null : id)
  }

  async function handleCreateAction(payload) {
    return createAction(payload)
  }

  return (
    <div className="p-6 space-y-5 max-w-6xl">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-slate-900 tracking-tight">Conflict Findings</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            {findings.length} finding{findings.length !== 1 ? 's' : ''} across all domains
          </p>
        </div>
        <button onClick={runScan} disabled={scanning} className="btn-primary flex items-center gap-2 text-xs">
          {scanning ? <Loader2 size={13} className="animate-spin" /> : <ScanLine size={13} />}
          {scanning ? 'Scanning…' : 'Re-scan'}
        </button>
      </div>

      {/* Filters */}
      <div className="flex gap-3">
        <select value={filterSev} onChange={e => setFilterSev(e.target.value)} className="input w-40 text-xs">
          <option value="">All Severities</option>
          {['CRITICAL','HIGH','MEDIUM','LOW'].map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <select value={filterStatus} onChange={e => setFilterStatus(e.target.value)} className="input w-40 text-xs">
          <option value="">All Statuses</option>
          {['OPEN','IN_REVIEW','RESOLVED'].map(s => <option key={s} value={s}>{s.replace(/_/g,' ')}</option>)}
        </select>
        {(filterSev || filterStatus) && (
          <button onClick={() => { setFilterSev(''); setFilterStatus('') }} className="btn-ghost text-xs px-3">
            Clear
          </button>
        )}
      </div>

      {/* List */}
      {loading ? (
        <div className="flex items-center justify-center py-16">
          <Loader2 size={24} className="animate-spin text-slate-400" />
        </div>
      ) : filtered.length === 0 ? (
        <div className="rounded-xl p-10 text-center bg-white border border-slate-200">
          <p className="text-slate-500 text-sm">No findings match the current filters.</p>
        </div>
      ) : (
        <div className="space-y-2">
          {filtered.map(f => {
            const borderColor = SEV_BORDER[f.severity] || '#6366f1'
            const isOpen = expanded === f.conflict_id
            return (
              <div key={f.conflict_id} className="rounded-xl overflow-hidden bg-white border border-slate-200"
                   style={{
                     borderLeft: `3px solid ${borderColor}`,
                     boxShadow: '0 1px 2px rgba(15,23,42,0.04)',
                   }}>
                {/* Row header */}
                <div className="flex items-start gap-3 px-4 py-3.5 cursor-pointer hover:bg-slate-50 transition-colors"
                     onClick={() => toggle(f.conflict_id)}>
                  <div className="mt-0.5">
                    {isOpen
                      ? <ChevronDown size={15} className="text-slate-500" />
                      : <ChevronRight size={15} className="text-slate-500" />
                    }
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <SeverityBadge severity={f.severity} />
                      <span className="text-sm font-medium text-slate-900">{f.title}</span>
                    </div>
                    <p className="text-xs text-slate-500 mt-1">
                      {f.conflict_id}
                      {f.source_policy && <> · <span className="text-slate-600">{f.source_policy}</span></>}
                      {f.source_technical && <> · <span className="text-slate-600">{f.source_technical}</span></>}
                      {' · '}detected {formatDistanceToNow(new Date(f.detected_at), { addSuffix: true })}
                    </p>
                  </div>
                  <div className="flex items-center gap-2 flex-shrink-0">
                    <TypeBadge type={f.type} />
                    <StatusBadge status={f.status} />
                  </div>
                </div>

                {/* Expanded detail */}
                {isOpen && (
                  <div className="px-4 pb-4 pt-1 space-y-4 border-t border-slate-100">
                    <div className="mt-3">
                      <p className="text-[10px] text-slate-400 font-semibold uppercase tracking-wider mb-1.5">Finding</p>
                      <p className="text-sm text-slate-800 leading-relaxed">{f.finding}</p>
                    </div>

                    <div>
                      <p className="text-[10px] text-slate-400 font-semibold uppercase tracking-wider mb-1.5">Business Impact</p>
                      <p className="text-sm text-slate-800 leading-relaxed">{f.impact}</p>
                    </div>

                    <div>
                      <p className="text-[10px] text-slate-400 font-semibold uppercase tracking-wider mb-1.5">Recommended Remediation</p>
                      <ol className="space-y-1">
                        {f.remediation?.map((step, i) => (
                          <li key={i} className="flex gap-2 text-sm text-slate-700">
                            <span className="text-slate-400 font-mono flex-shrink-0">{i + 1}.</span>
                            <span>{step}</span>
                          </li>
                        ))}
                      </ol>
                    </div>

                    <div className="flex items-center gap-2 flex-wrap">
                      <p className="text-[10px] text-slate-400 font-semibold uppercase tracking-wider">Domains:</p>
                      {f.domains?.map(d => (
                        <span key={d} className="text-xs px-2 py-0.5 rounded-full flex items-center gap-1 bg-indigo-50 text-indigo-700 border border-indigo-200">
                          <ExternalLink size={10} /> {d}
                        </span>
                      ))}
                    </div>

                    {f.status === 'OPEN' && (
                      <div className="flex gap-2 pt-1">
                        <button
                          onClick={() => setActionTarget(f)}
                          className="btn-primary flex items-center gap-1.5 text-xs"
                        >
                          <Zap size={12} /> Initiate Remediation Action
                        </button>
                      </div>
                    )}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      {actionTarget && (
        <ActionRequestModal
          conflict={actionTarget}
          onClose={result => {
            setActionTarget(null)
            if (result) load()
          }}
          onCreate={handleCreateAction}
        />
      )}
    </div>
  )
}
