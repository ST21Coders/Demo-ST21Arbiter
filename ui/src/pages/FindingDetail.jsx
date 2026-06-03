import { useEffect, useMemo, useState } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import {
  ArrowLeft, FileText, Server, Lightbulb, Zap, ExternalLink, ChevronDown, ChevronRight, Loader2,
} from 'lucide-react'
import { useFindings, useFindingDetail, useChangeRequests } from '../hooks/useApi'
import { SeverityBadge, StatusBadge, TypeBadge } from '../components/SeverityBadge'
import ActionRequestModal from '../components/ActionRequestModal'
import { DOMAIN_LABELS } from '../mockData'
import { formatDistanceToNow } from 'date-fns'

const CONFLICT_TYPE_DESC = {
  CONTRADICTION: 'Policy explicitly approves what enforcement explicitly blocks (or vice versa).',
  GAP:           'Policy requires broader coverage than enforcement provides.',
  DRIFT:         'Infrastructure has deviated from the written policy over time.',
  OVERLAP:       'Two policies disagree on the same control.',
}

export default function FindingDetail() {
  const { id } = useParams()
  const navigate = useNavigate()
  const { findings, load: loadFindings } = useFindings()
  const { finding: fetched, loading: fetching } = useFindingDetail(id)
  const { createAction } = useChangeRequests()
  const [actionOpen, setActionOpen] = useState(false)
  const [showRaw, setShowRaw] = useState(false)

  // Keep the list-mode lookup as a fallback so cached lists render instantly
  // before /findings/{id} resolves. Triggered once on mount; the hook is idempotent.
  useEffect(() => {
    if (findings.length === 0) loadFindings()
  }, [findings.length, loadFindings])

  // Prefer the dedicated-endpoint result, fall back to the list cache.
  const finding = useMemo(
    () => fetched || findings.find(f => f.conflict_id === id),
    [fetched, findings, id],
  )

  if (!finding) {
    return (
      <div className="p-6 max-w-5xl">
        <button onClick={() => navigate('/findings')} className="btn-ghost flex items-center gap-1.5 text-xs mb-4">
          <ArrowLeft size={13} /> Back to findings
        </button>
        <div className="card p-10 text-center">
          {fetching ? (
            <>
              <Loader2 size={20} className="animate-spin text-slate-400 mx-auto mb-3" />
              <p className="text-sm text-slate-600">Loading finding <span className="font-mono">{id}</span>…</p>
            </>
          ) : (
            <>
              <p className="text-sm text-slate-600">Finding <span className="font-mono">{id}</span> was not found.</p>
              <p className="text-xs text-slate-400 mt-2">It may have been resolved or deleted. Return to the list and re-scan if needed.</p>
              <button onClick={() => navigate('/findings')} className="btn-primary text-xs mt-4">
                Back to findings list
              </button>
            </>
          )}
        </div>
      </div>
    )
  }

  const policyCitations = finding.policy_citations || []
  const enforcement = finding.enforcement_evidence || []

  return (
    <div className="p-6 space-y-5 max-w-6xl">
      {/* Header / breadcrumb */}
      <div className="flex items-center justify-between">
        <div>
          <button onClick={() => navigate('/findings')} className="btn-ghost flex items-center gap-1.5 text-xs mb-2">
            <ArrowLeft size={13} /> Back to findings
          </button>
          <div className="flex items-center gap-2 flex-wrap">
            <SeverityBadge severity={finding.severity} />
            <StatusBadge status={finding.status} />
            {finding.conflict_type && <TypeBadge type={finding.conflict_type} />}
            <span className="text-[11px] font-mono text-slate-500">{finding.conflict_id}</span>
            {finding.domain && (
              <span className="text-[11px] text-slate-600 bg-slate-50 border border-slate-200 px-2 py-0.5 rounded-md">
                {DOMAIN_LABELS[finding.domain] || finding.domain}
              </span>
            )}
            {finding.source_pair && (
              <span className="text-[11px] text-indigo-700 bg-indigo-50 border border-indigo-200 px-2 py-0.5 rounded-md">
                {finding.source_pair}
              </span>
            )}
          </div>
          <h1 className="text-lg font-bold text-slate-900 mt-2 leading-snug">{finding.title}</h1>
          <p className="text-xs text-slate-500 mt-1">
            Detected {formatDistanceToNow(new Date(finding.detected_at), { addSuffix: true })}
            {finding.regulatory?.length > 0 && (
              <> · {finding.regulatory.join(' · ')}</>
            )}
          </p>
        </div>
        {finding.status === 'OPEN' && (
          <button onClick={() => setActionOpen(true)} className="btn-primary flex items-center gap-1.5 text-xs flex-shrink-0">
            <Zap size={12} /> Create CR
          </button>
        )}
      </div>

      {/* Conflict type explainer */}
      {finding.conflict_type && CONFLICT_TYPE_DESC[finding.conflict_type] && (
        <div className="rounded-xl px-4 py-3 bg-indigo-50 border border-indigo-200 flex items-start gap-2.5">
          <Lightbulb size={14} className="text-indigo-600 flex-shrink-0 mt-0.5" />
          <p className="text-xs text-indigo-800 leading-relaxed">
            <span className="font-semibold">{finding.conflict_type}.</span> {CONFLICT_TYPE_DESC[finding.conflict_type]}
          </p>
        </div>
      )}

      {/* Split screen: policy / enforcement */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Policy citations */}
        <div className="card p-0">
          <div className="px-4 py-3 border-b border-slate-200 flex items-center gap-2">
            <FileText size={14} className="text-indigo-600" />
            <p className="text-xs font-semibold text-slate-700 uppercase tracking-wider">Policy</p>
          </div>
          <div className="p-4 space-y-3">
            {policyCitations.length === 0 ? (
              <p className="text-xs text-slate-500">
                {finding.source_policy
                  ? <>Reference: <span className="font-mono text-slate-700">{finding.source_policy}</span></>
                  : 'No structured citations on this finding.'}
              </p>
            ) : policyCitations.map((c, i) => (
              <div key={i} className="rounded-lg border border-slate-200 bg-slate-50 p-3">
                <p className="text-[11px] text-slate-500 font-mono mb-1">
                  {c.doc} {c.version} · §{c.section}
                  {c.confidence != null && <span className="ml-2 text-indigo-600">conf {Math.round(c.confidence * 100)}%</span>}
                </p>
                <p className="text-sm text-slate-800 leading-relaxed italic">"{c.quote}"</p>
              </div>
            ))}
            {finding.policy_mandates?.length > 0 && (
              <div className="flex items-center gap-1.5 flex-wrap pt-1">
                <span className="text-[10px] text-slate-400 font-semibold uppercase tracking-wider">Mandates:</span>
                {finding.policy_mandates.map(m => (
                  <span key={m} className="text-[11px] font-mono text-slate-700 bg-white border border-slate-200 px-1.5 py-0.5 rounded">{m}</span>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Enforcement evidence */}
        <div className="card p-0">
          <div className="px-4 py-3 border-b border-slate-200 flex items-center gap-2">
            <Server size={14} className="text-orange-600" />
            <p className="text-xs font-semibold text-slate-700 uppercase tracking-wider">Enforcement</p>
          </div>
          <div className="p-4 space-y-3">
            {enforcement.length === 0 ? (
              <p className="text-xs text-slate-500">
                {finding.source_technical
                  ? <>Reference: <span className="font-mono text-slate-700">{finding.source_technical}</span></>
                  : 'No structured enforcement evidence on this finding.'}
              </p>
            ) : enforcement.map((e, i) => (
              <div key={i} className="rounded-lg border border-slate-200 bg-slate-50 p-3">
                <p className="text-[11px] text-slate-500 font-mono mb-1">
                  {e.source} · {e.rule_id || e.resource_id}
                  {e.action && <span className="ml-2 px-1.5 py-0.5 rounded bg-orange-100 text-orange-700">{e.action}</span>}
                </p>
                {e.raw && (
                  <details className="text-[11px]" onToggle={ev => setShowRaw(ev.target.open)}>
                    <summary className="cursor-pointer text-indigo-600 hover:text-indigo-800 select-none flex items-center gap-1">
                      {showRaw ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
                      Raw evidence
                    </summary>
                    <pre className="mt-2 p-2 bg-white border border-slate-200 rounded text-[10px] text-slate-700 font-mono overflow-x-auto">
{JSON.stringify(e.raw, null, 2)}
                    </pre>
                  </details>
                )}
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Finding narrative + remediation */}
      <div className="card p-0">
        <div className="px-4 py-3 border-b border-slate-200 flex items-center gap-2">
          <ExternalLink size={14} className="text-slate-600" />
          <p className="text-xs font-semibold text-slate-700 uppercase tracking-wider">ARBITER analysis</p>
        </div>
        <div className="p-4 space-y-4">
          <div>
            <p className="text-[10px] text-slate-400 font-semibold uppercase tracking-wider mb-1.5">Finding</p>
            <p className="text-sm text-slate-800 leading-relaxed">{finding.finding}</p>
          </div>
          <div>
            <p className="text-[10px] text-slate-400 font-semibold uppercase tracking-wider mb-1.5">Business impact</p>
            <p className="text-sm text-slate-800 leading-relaxed">{finding.impact}</p>
          </div>
          {finding.remediation?.length > 0 && (
            <div>
              <p className="text-[10px] text-slate-400 font-semibold uppercase tracking-wider mb-1.5">Recommended remediation</p>
              <ol className="space-y-1">
                {finding.remediation.map((step, i) => (
                  <li key={i} className="flex gap-2 text-sm text-slate-700">
                    <span className="text-slate-400 font-mono flex-shrink-0">{i + 1}.</span>
                    <span>{step}</span>
                  </li>
                ))}
              </ol>
            </div>
          )}
          {finding.fp_score != null && (
            <p className="text-[11px] text-slate-500">
              False-positive likelihood: <span className="font-mono text-slate-700">{(finding.fp_score * 100).toFixed(0)}%</span>
            </p>
          )}
          <div className="pt-2">
            <Link to="/findings" className="text-[11px] text-indigo-600 hover:text-indigo-800">← All findings</Link>
          </div>
        </div>
      </div>

      {actionOpen && (
        <ActionRequestModal
          conflict={finding}
          onClose={() => setActionOpen(false)}
          onCreate={createAction}
        />
      )}
    </div>
  )
}
