import { useState } from 'react'
import {
  Radar, Loader2, AlertTriangle, Boxes, Database, Layers,
  UserX, Unlink, PackageX, ServerOff, ShieldQuestion,
} from 'lucide-react'
import { runDriftScan } from '../hooks/useApi'

/* CMDB / Asset drift scan against the live ServiceNow CMDB, reconciled with AWS
   reality by the master orchestrator (POST /servicenow/drift-scan). Surfaces the
   four drift classes — unmanaged AWS resources (no CI), stale CIs (no AWS
   resource), ownership drift, and asset drift — that also feed the main Findings
   pipeline as DRIFT conflicts. */

const SEVERITY_CLS = {
  HIGH:     'bg-red-50 text-red-700 border-red-200',
  CRITICAL: 'bg-red-50 text-red-700 border-red-200',
  MEDIUM:   'bg-amber-50 text-amber-700 border-amber-200',
  LOW:      'bg-sky-50 text-sky-700 border-sky-200',
}

// Drift-kind → icon + human label.
const KIND_META = {
  unmanaged_resource: { icon: ShieldQuestion, label: 'Unmanaged resource' },
  stale_ci:           { icon: ServerOff,      label: 'Stale CI' },
  ownership_drift:    { icon: UserX,          label: 'Ownership drift' },
  asset_stale:        { icon: PackageX,       label: 'Asset drift' },
  asset_unlinked:     { icon: Unlink,         label: 'Asset unlinked' },
}

function kindOf(item) {
  return item?.enforcement_evidence?.[0]?.raw?.drift_kind || 'other'
}

export default function CmdbDrift() {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState(null)

  async function scan() {
    setLoading(true); setError(''); setResult(null)
    try {
      setResult(await runDriftScan())
    } catch (err) {
      setError(err.message || String(err))
    } finally {
      setLoading(false)
    }
  }

  const items = result?.drift_items || []
  const summary = result?.summary || { total: 0, by_kind: {}, by_severity: {} }

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-bold text-slate-800 flex items-center gap-2">
            <Radar size={20} className="text-indigo-600" /> CMDB / Asset Drift Scan
          </h1>
          <p className="text-sm text-slate-500 mt-1">
            Reconciles the live ServiceNow CMDB and Asset Management against AWS reality.
            Surfaces unmanaged resources, stale CIs, ownership drift, and asset drift — the
            same items flow into Findings as <span className="font-medium">DRIFT</span> conflicts.
          </p>
        </div>
        <button onClick={scan} disabled={loading}
          className="btn-primary inline-flex items-center gap-1.5 text-sm shrink-0 disabled:opacity-50">
          {loading ? <Loader2 size={14} className="animate-spin" /> : <Radar size={14} />}
          {loading ? 'Scanning…' : 'Run drift scan'}
        </button>
      </div>

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

          {/* Summary tiles */}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <SummaryTile icon={Layers} label="Drift items" value={summary.total} />
            <SummaryTile icon={Database} label="CIs scanned" value={result.snapshot_counts?.cis ?? '—'} />
            <SummaryTile icon={Boxes} label="Assets scanned" value={result.snapshot_counts?.assets ?? '—'} />
            <SummaryTile icon={Radar} label="AWS resources" value={result.aws_inventory_count ?? '—'} />
          </div>

          {/* Drift list */}
          <div className="border border-slate-200 rounded-xl bg-white divide-y divide-slate-100">
            {items.length === 0 ? (
              <p className="text-sm text-emerald-700 p-4">No CMDB/asset drift detected — the CMDB is reconciled with AWS.</p>
            ) : (
              items.map((item, i) => {
                const kind = kindOf(item)
                const meta = KIND_META[kind] || { icon: AlertTriangle, label: kind }
                const Icon = meta.icon
                return (
                  <div key={item.conflict_id || i} className="p-4 space-y-1.5">
                    <div className="flex items-center gap-2 flex-wrap">
                      <Icon size={15} className="text-slate-500 shrink-0" />
                      <span className="text-sm font-semibold text-slate-800">{item.title}</span>
                      <span className={`text-[10px] font-medium px-2 py-0.5 rounded-full border ${SEVERITY_CLS[item.severity] || 'bg-slate-100 text-slate-600 border-slate-200'}`}>
                        {item.severity}
                      </span>
                      <span className="text-[10px] uppercase tracking-wide text-slate-400 ml-auto">{meta.label}</span>
                    </div>
                    {item.source_technical && (
                      <p className="text-[11px] font-mono text-slate-400 break-all">{item.source_technical}</p>
                    )}
                    <p className="text-sm text-slate-600">{item.finding}</p>
                    {item.impact && <p className="text-xs text-slate-500"><span className="font-medium">Impact:</span> {item.impact}</p>}
                    {Array.isArray(item.remediation) && item.remediation.length > 0 && (
                      <ul className="list-disc ml-5 text-xs text-slate-500 space-y-0.5">
                        {item.remediation.map((r, j) => <li key={j}>{r}</li>)}
                      </ul>
                    )}
                  </div>
                )
              })
            )}
          </div>
        </div>
      )}

      {!result && !loading && !error && (
        <p className="text-sm text-slate-400 italic py-8 text-center">
          Run the scan to reconcile the ServiceNow CMDB &amp; assets against live AWS reality.
        </p>
      )}
    </div>
  )
}

function SummaryTile({ icon: Icon, label, value }) {
  return (
    <div className="border border-slate-200 rounded-xl bg-white p-3">
      <p className="text-[11px] font-bold uppercase tracking-wide text-slate-400 flex items-center gap-1.5">
        <Icon size={12} /> {label}
      </p>
      <p className="text-2xl font-semibold text-slate-800 mt-1">{value}</p>
    </div>
  )
}
