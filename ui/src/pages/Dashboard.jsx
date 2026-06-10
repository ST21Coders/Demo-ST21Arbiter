import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ScanLine, ShieldAlert, ShieldCheck, Clock, AlertTriangle, ArrowRight, Loader2, TrendingUp, Activity, Upload, FileText, Zap } from 'lucide-react'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, Legend } from 'recharts'
import { useFindings, useDashboard, triggerScan, getScanRun, useScanFeed } from '../hooks/useApi'
import { SeverityBadge, StatusBadge, TypeBadge } from '../components/SeverityBadge'
import {
  buildDomainSourceMatrix, DOMAIN_LABELS, DOMAIN_KEYS, SOURCE_PAIRS,
} from '../mockData'
import { usePersona } from '../contexts/PersonaContext'
import { formatDistanceToNow } from 'date-fns'

const STAT_STYLES = {
  red:    { border: '#ef4444', glow: 'rgba(239,68,68,0.08)',   icon: 'rgba(239,68,68,0.10)',   text: '#dc2626' },
  amber:  { border: '#f59e0b', glow: 'rgba(245,158,11,0.08)',  icon: 'rgba(245,158,11,0.10)',  text: '#b45309' },
  emerald:{ border: '#10b981', glow: 'rgba(16,185,129,0.08)',  icon: 'rgba(16,185,129,0.10)',  text: '#047857' },
  indigo: { border: '#6366f1', glow: 'rgba(99,102,241,0.08)',  icon: 'rgba(99,102,241,0.10)',  text: '#4338ca' },
}

function StatCard({ icon: Icon, label, value, sub, color = 'indigo' }) {
  const s = STAT_STYLES[color]
  return (
    <div className="rounded-xl p-4 flex items-center gap-4 bg-white"
         style={{
           border: `1px solid ${s.border}25`,
           borderLeft: `3px solid ${s.border}`,
           boxShadow: `0 1px 3px rgba(15,23,42,0.06), 0 1px 2px rgba(15,23,42,0.04)`,
         }}>
      <div className="w-10 h-10 rounded-lg flex items-center justify-center flex-shrink-0"
           style={{ background: s.icon }}>
        <Icon size={18} style={{ color: s.text }} />
      </div>
      <div>
        <p className="text-2xl font-bold text-slate-900 tabular-nums">{value}</p>
        <p className="text-xs text-slate-600 mt-0.5">{label}</p>
        {sub && <p className="text-[10px] text-slate-400 mt-0.5">{sub}</p>}
      </div>
    </div>
  )
}

// Heat-map cell colour by count. Tailwind classes mirror HeatMap.jsx's hm-* CSS.
function heatCell(n) {
  if (n === 0) return { bg: '#f8fafc', fg: '#94a3b8', border: '#e2e8f0' }
  if (n === 1) return { bg: '#fef3c7', fg: '#92400e', border: '#fde68a' }
  if (n === 2) return { bg: '#fed7aa', fg: '#9a3412', border: '#fdba74' }
  if (n === 3) return { bg: '#fecaca', fg: '#991b1b', border: '#fca5a5' }
  return         { bg: '#fca5a5', fg: '#7f1d1d', border: '#f87171' }
}

function HeatMapGrid({ findings, onCellClick }) {
  const matrix = buildDomainSourceMatrix(findings)
  return (
    <div className="card p-0">
      <div className="px-5 py-3.5 border-b border-slate-200 flex items-center justify-between">
        <div>
          <p className="text-sm font-semibold text-slate-900">Conflict Heat Map</p>
          <p className="text-xs text-slate-500 mt-0.5">Compliance domain × source system. Click a cell to drill in.</p>
        </div>
        <p className="text-[10px] text-slate-400">conflicts only · compliant alignments excluded</p>
      </div>
      <div className="p-4 overflow-x-auto">
        <table className="w-full text-sm border-separate border-spacing-2 min-w-[480px]">
          <thead>
            <tr>
              <th className="text-left text-xs text-slate-500 font-medium w-44 pb-2">Domain</th>
              {SOURCE_PAIRS.map(s => (
                <th key={s} className="text-xs text-slate-700 font-semibold pb-2 text-center">{s}</th>
              ))}
              <th className="text-xs text-slate-500 font-medium pb-2 text-center w-16">Total</th>
            </tr>
          </thead>
          <tbody>
            {DOMAIN_KEYS.map(dk => {
              const rowTotal = SOURCE_PAIRS.reduce((s, sp) => s + (matrix[dk]?.[sp] ?? 0), 0)
              return (
                <tr key={dk}>
                  <td className="text-xs text-slate-700 font-semibold pr-3 py-1">{DOMAIN_LABELS[dk]}</td>
                  {SOURCE_PAIRS.map(sp => {
                    const count = matrix[dk]?.[sp] ?? 0
                    const c = heatCell(count)
                    return (
                      <td key={sp} className="py-1 text-center">
                        <button
                          onClick={() => onCellClick?.(dk, sp, count)}
                          disabled={count === 0}
                          className="w-full rounded-lg py-3 font-bold text-lg transition-transform hover:scale-105 disabled:cursor-default disabled:hover:scale-100"
                          style={{
                            background: c.bg,
                            color: c.fg,
                            border: `1px solid ${c.border}`,
                          }}
                        >
                          {count > 0 ? count : '—'}
                        </button>
                      </td>
                    )
                  })}
                  <td className="text-center">
                    <span className="text-xs font-bold text-slate-700 tabular-nums">{rowTotal}</span>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// Recent activity surfaces the audit-log entries returned in the /dashboard
// aggregate. Auto-ingest scan-runs land here as "Auto-scan after upload of <key>"
// so the F1 chain has a visible end-point in the UI. CR_* / JIRA_LINKED also flow.
const ACTIVITY_META = {
  SCAN_STARTED:       { icon: ScanLine,    color: '#4338ca', bg: '#eef2ff', border: '#c7d2fe', label: 'Scan started'   },
  SCAN_COMPLETED:     { icon: ShieldCheck, color: '#047857', bg: '#ecfdf5', border: '#a7f3d0', label: 'Scan completed' },
  SCAN_FAILED:        { icon: AlertTriangle, color: '#b91c1c', bg: '#fef2f2', border: '#fecaca', label: 'Scan failed'  },
  INGESTION_COMPLETE: { icon: FileText,    color: '#4338ca', bg: '#eef2ff', border: '#c7d2fe', label: 'KB ingest'      },
  CR_CREATED:         { icon: Zap,         color: '#b45309', bg: '#fffbeb', border: '#fde68a', label: 'CR created'     },
  CR_APPROVED:        { icon: ShieldCheck, color: '#047857', bg: '#ecfdf5', border: '#a7f3d0', label: 'CR approved'    },
  CR_REJECTED:        { icon: AlertTriangle, color: '#b91c1c', bg: '#fef2f2', border: '#fecaca', label: 'CR rejected'  },
  CR_EXECUTED:        { icon: ShieldCheck, color: '#047857', bg: '#ecfdf5', border: '#a7f3d0', label: 'CR executed'    },
  CR_ESCALATED:       { icon: AlertTriangle, color: '#b45309', bg: '#fffbeb', border: '#fde68a', label: 'CR escalated' },
  JIRA_LINKED:        { icon: Activity,    color: '#0369a1', bg: '#f0f9ff', border: '#bae6fd', label: 'JIRA linked'    },
}

function parseDetails(d) {
  if (!d) return {}
  if (typeof d === 'object') return d
  try { return JSON.parse(d) } catch { return {} }
}

function activitySummary(row) {
  const meta = ACTIVITY_META[row.action_type] || {
    icon: Activity, color: '#475569', bg: '#f8fafc', border: '#e2e8f0', label: (row.action_type || 'EVENT').replace(/_/g, ' '),
  }
  const trig = row.triggered_by || ''
  const details = parseDetails(row.details)
  let line = ''
  if (row.action_type === 'SCAN_COMPLETED') {
    const t = details.totals || {}
    const isAutoIngest = (details.triggered_by || trig || '').startsWith('auto-ingest:')
    const key = (details.triggered_by || trig || '').replace(/^auto-ingest:/, '')
    line = isAutoIngest
      ? `Auto-scan after upload of ${key} — ${t.conflicts ?? '?'} conflicts, ${t.compliant ?? '?'} compliant`
      : `Scan complete — ${t.conflicts ?? '?'} conflicts, ${t.compliant ?? '?'} compliant`
  } else if (row.action_type === 'SCAN_STARTED') {
    const isAutoIngest = (details.triggered_by || '').startsWith('auto-ingest:')
    line = isAutoIngest ? `Auto-scan started after upload` : 'Manual scan started'
  } else if (row.action_type?.startsWith('CR_')) {
    line = `${meta.label} — ${row.resource || details.cr_id || ''}`
  } else {
    line = `${row.resource || details.cr_id || row.user || ''}`
  }
  return { meta, line }
}

function RecentActivity({ rows, loading }) {
  return (
    <div className="rounded-xl overflow-hidden bg-white border border-slate-200"
         style={{ boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }}>
      <div className="flex items-center justify-between px-5 py-3.5 border-b border-slate-200">
        <div className="flex items-center gap-2">
          <Activity size={14} className="text-indigo-600" />
          <h2 className="text-sm font-semibold text-slate-900">Recent Activity</h2>
        </div>
        <p className="text-[10px] text-slate-500">audit-log · last 5 events</p>
      </div>
      {loading ? (
        <div className="flex items-center justify-center py-10">
          <Loader2 size={18} className="animate-spin text-slate-400" />
        </div>
      ) : !rows?.length ? (
        <p className="text-center text-slate-500 py-10 text-sm">No recent events yet — upload a doc to /pipeline or click Run AI Scan.</p>
      ) : (
        <div className="divide-y divide-slate-100">
          {rows.map((row, i) => {
            const { meta, line } = activitySummary(row)
            const Icon = meta.icon
            const ts = row.timestamp ? new Date(row.timestamp) : null
            return (
              <div key={row.event_id || row.log_id || i}
                   className="flex items-center gap-3 px-5 py-3 transition-colors">
                <div className="w-7 h-7 rounded-lg flex items-center justify-center flex-shrink-0"
                     style={{ background: meta.bg, border: `1px solid ${meta.border}` }}>
                  <Icon size={13} style={{ color: meta.color }} />
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-sm text-slate-800 truncate" title={line}>
                    <span className="font-medium" style={{ color: meta.color }}>{meta.label}</span>
                    <span className="text-slate-700"> — {line}</span>
                  </p>
                  <p className="text-[11px] text-slate-500 mt-0.5">
                    {ts ? formatDistanceToNow(ts, { addSuffix: true }) : ''}
                    {row.user && row.user !== 'system' && <> · {row.user}</>}
                  </p>
                </div>
                {row.status && (
                  <StatusBadge status={row.status} />
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

function TrendChart({ data }) {
  if (!data?.length) return null
  return (
    <div className="card p-0">
      <div className="px-5 py-3.5 border-b border-slate-200 flex items-center gap-2">
        <TrendingUp size={14} className="text-indigo-600" />
        <div>
          <p className="text-sm font-semibold text-slate-900">Open conflicts by severity — last 30 days</p>
          <p className="text-xs text-slate-500 mt-0.5">Derived from scan-runs history; days without a scan inherit the previous day's totals.</p>
        </div>
      </div>
      <div className="px-4 py-3" style={{ height: 220 }}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 5, right: 12, bottom: 0, left: -16 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
            <XAxis dataKey="date" tickFormatter={d => d.slice(5)} tick={{ fontSize: 10, fill: '#64748b' }} axisLine={false} />
            <YAxis allowDecimals={false} tick={{ fontSize: 10, fill: '#64748b' }} axisLine={false} />
            <Tooltip contentStyle={{ fontSize: 11 }} labelStyle={{ fontSize: 11 }} />
            <Legend wrapperStyle={{ fontSize: 11, paddingTop: 4 }} iconSize={10} verticalAlign="bottom" height={28} />
            <Line type="monotone" dataKey="critical" name="Critical" stroke="#ef4444" strokeWidth={2} dot={false} />
            <Line type="monotone" dataKey="high"     name="High"     stroke="#f97316" strokeWidth={2} dot={false} />
            <Line type="monotone" dataKey="medium"   name="Medium"   stroke="#f59e0b" strokeWidth={2} dot={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}

export default function Dashboard() {
  const { findings, loading, load } = useFindings()
  const { data: dashAgg, reload: reloadDash } = useDashboard()
  const persona = usePersona()
  const personaId = persona?.personaId
  const navigate = useNavigate()
  const [scanMsg, setScanMsg] = useState('')
  const [scanning, setScanning] = useState(false)

  useEffect(() => { load() }, [load])

  // Live scan feed: any background scan (upload/auto-ingest/daily cron, or a
  // manual scan from another tab) refreshes the tiles silently — dashboard KPIs
  // aren't being mid-edited, so silent auto-refresh is the right UX here.
  const { activeRun } = useScanFeed({
    onNewScan: () => { load(); reloadDash() },
  })

  // KPI sources: aggregate endpoint takes precedence; otherwise compute from findings.
  const kpiSev = dashAgg?.kpis?.active_conflicts || {
    CRITICAL: findings.filter(f => f.severity === 'CRITICAL' && !f.compliant).length,
    HIGH:     findings.filter(f => f.severity === 'HIGH'     && !f.compliant).length,
    MEDIUM:   findings.filter(f => f.severity === 'MEDIUM'   && !f.compliant).length,
    LOW:      findings.filter(f => f.severity === 'LOW'      && !f.compliant).length,
  }
  const critical = kpiSev.CRITICAL
  const open     = findings.filter(f => f.status === 'OPEN' && !f.compliant).length
  const resolved = findings.filter(f => f.status === 'RESOLVED').length
  const lastScan = dashAgg?.last_scan

  // Wire Run AI Scan → POST /scan → poll GET /scan-runs/{id} until completion.
  async function handleScan() {
    setScanMsg('')
    setScanning(true)
    try {
      const { scan_run_id, status } = await triggerScan()
      if (status === 'COMPLETED') {
        // stub or sync-completed path
        await load()
        await reloadDash()
        setScanMsg('Scan complete — findings refreshed')
      } else {
        // Async — poll every 2s up to 60s. Tolerate transient 404s while the
        // scanner Lambda cold-starts and writes its own progress row.
        let consecutiveErrors = 0
        for (let attempt = 0; attempt < 30; attempt++) {
          await new Promise(r => setTimeout(r, 2000))
          let run
          try {
            run = await getScanRun(scan_run_id)
            consecutiveErrors = 0
          } catch (err) {
            consecutiveErrors++
            if (consecutiveErrors >= 5) throw err   // give up after ~10s of consecutive failures
            continue
          }
          if (run?.status === 'COMPLETED' || run?.status === 'FAILED') {
            await load()
            await reloadDash()
            const totals = run?.totals
            setScanMsg(run.status === 'COMPLETED' && totals
              ? `Scan complete: ${totals.conflicts || 0} conflicts, ${totals.compliant || 0} compliant`
              : `Scan ${run.status?.toLowerCase()}`)
            break
          }
        }
      }
    } catch (err) {
      setScanMsg(`Scan failed: ${err.message}`)
    } finally {
      setScanning(false)
      setTimeout(() => setScanMsg(''), 6000)
    }
  }

  const recent = [...findings].sort((a, b) => new Date(b.detected_at) - new Date(a.detected_at)).slice(0, 5)

  return (
    <div className="p-6 space-y-6 page-container">
      {/* Page header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-slate-900 tracking-tight">Governance Dashboard</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            Meridian Insurance Group · ARBITER Smart AI Governance Engine
            {lastScan?.finished_at && (
              <> · Last scan {formatDistanceToNow(new Date(lastScan.finished_at), { addSuffix: true })}
                {lastScan.totals && <> — {lastScan.totals.conflicts ?? 0} conflicts, {lastScan.totals.compliant ?? 0} compliant</>}
              </>
            )}
          </p>
        </div>
        <button onClick={handleScan} disabled={scanning} className="btn-primary flex items-center gap-2 text-xs">
          {scanning ? <Loader2 size={13} className="animate-spin" /> : <ScanLine size={13} />}
          {scanning ? 'Scanning…' : 'Run AI Scan'}
        </button>
      </div>

      {(scanning || activeRun) && (
        <div className="rounded-xl p-4 flex items-center gap-3 bg-indigo-50 border border-indigo-200">
          <Loader2 size={15} className="animate-spin text-indigo-600 flex-shrink-0" />
          <div>
            <p className="text-sm text-indigo-800 font-medium">Scan in progress</p>
            <p className="text-xs text-indigo-600 mt-0.5">Analyzing policies — tiles refresh on completion…</p>
          </div>
        </div>
      )}

      {scanMsg && (() => {
        const failed = /^(scan failed|scan rejected|scan error)/i.test(scanMsg)
        return failed ? (
          <div className="rounded-xl p-4 flex items-center gap-2 bg-red-50 border border-red-200">
            <AlertTriangle size={14} className="text-red-600" />
            <p className="text-sm text-red-800">{scanMsg}</p>
          </div>
        ) : (
          <div className="rounded-xl p-4 flex items-center gap-2 bg-emerald-50 border border-emerald-200">
            <ShieldCheck size={14} className="text-emerald-600" />
            <p className="text-sm text-emerald-800">{scanMsg}</p>
          </div>
        )
      })()}

      {/* Stats row */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <StatCard icon={ShieldAlert}  label="Open Conflicts"     value={open}             color="red"     />
        <StatCard icon={AlertTriangle} label="Critical Severity" value={critical} sub="Immediate action required" color="red" />
        <StatCard icon={ShieldCheck}  label="Resolved"           value={resolved}         color="emerald" />
        <StatCard icon={Clock}        label="Total Findings"     value={findings.length}  color="indigo"  />
      </div>

      {/* Persona-specific quick action surfaced first for SOC + CISO */}
      {personaId === 'ciso' && dashAgg?.kpis?.pending_approvals > 0 && (
        <div className="rounded-xl p-4 flex items-center justify-between bg-amber-50 border border-amber-200"
             style={{ borderLeft: '3px solid #f59e0b' }}>
          <div className="flex items-center gap-3">
            <Clock size={16} className="text-amber-600" />
            <div>
              <p className="text-sm font-semibold text-amber-800">{dashAgg.kpis.pending_approvals} change request{dashAgg.kpis.pending_approvals !== 1 ? 's' : ''} pending your approval</p>
              <p className="text-xs text-amber-700 mt-0.5">Open the Action Center to review and approve.</p>
            </div>
          </div>
          <button onClick={() => navigate('/actions')} className="btn-primary flex items-center gap-1.5 text-xs flex-shrink-0">
            Action Center <ArrowRight size={12} />
          </button>
        </div>
      )}

      {/* Conflict Heat Map — Domain × Source (the doc-mandated grid) */}
      <HeatMapGrid
        findings={findings}
        onCellClick={(domain, source) => navigate(`/findings?domain=${domain}&source=${encodeURIComponent(source)}`)}
      />

      {/* Recent Activity — surfaces F1 auto-ingest scans + CR transitions */}
      <RecentActivity rows={dashAgg?.recent_activity} loading={!dashAgg && loading} />

      {/* 30-day open-conflict trend (recharts) */}
      <TrendChart data={dashAgg?.trend} />

      {/* Recent findings */}
      <div className="rounded-xl overflow-hidden bg-white border border-slate-200"
           style={{ boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }}>
        <div className="flex items-center justify-between px-5 py-3.5 border-b border-slate-200">
          <h2 className="text-sm font-semibold text-slate-900">Recent Findings</h2>
          <button onClick={() => navigate('/findings')} className="text-xs text-indigo-600 hover:text-indigo-700 flex items-center gap-1 transition-colors">
            View all <ArrowRight size={11} />
          </button>
        </div>

        {loading ? (
          <div className="flex items-center justify-center py-12">
            <Loader2 size={18} className="animate-spin text-slate-400" />
          </div>
        ) : recent.length === 0 ? (
          <p className="text-center text-slate-500 py-10 text-sm">No findings yet — run a scan.</p>
        ) : (
          <div className="divide-y divide-slate-100">
            {recent.map(f => (
              <div
                key={f.conflict_id}
                onClick={() => navigate(`/findings/${f.conflict_id}`)}
                className="flex items-center gap-3 px-5 py-3.5 hover:bg-slate-50 cursor-pointer transition-colors"
              >
                <SeverityBadge severity={f.severity} />
                <div className="flex-1 min-w-0">
                  <p className="text-sm text-slate-800 font-medium truncate">{f.title}</p>
                  <p className="text-[11px] text-slate-500 mt-0.5">
                    {f.conflict_id} · {formatDistanceToNow(new Date(f.detected_at), { addSuffix: true })}
                  </p>
                </div>
                <div className="flex items-center gap-2 flex-shrink-0">
                  <TypeBadge type={f.conflict_type || f.type} />
                  <StatusBadge status={f.status} />
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Critical callout */}
      {findings.some(f => f.severity === 'CRITICAL' && f.status === 'OPEN') && (
        <div className="rounded-xl p-4 flex items-center justify-between bg-red-50 border border-red-200"
             style={{ borderLeft: '3px solid #ef4444' }}>
          <div className="flex items-center gap-3">
            <AlertTriangle size={16} className="text-red-600 flex-shrink-0" />
            <div>
              <p className="text-sm font-semibold text-red-800">Critical conflicts require immediate action</p>
              <p className="text-xs text-red-700 mt-0.5">
                {critical} critical finding{critical !== 1 ? 's' : ''} — CISO + VPE + Legal approval required (MIG standard)
              </p>
            </div>
          </div>
          <button onClick={() => navigate('/actions')} className="btn-danger flex items-center gap-1.5 flex-shrink-0 text-xs">
            Action Center <ArrowRight size={12} />
          </button>
        </div>
      )}
    </div>
  )
}
