import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ScanLine, ShieldAlert, ShieldCheck, Clock, AlertTriangle, ArrowRight, Loader2 } from 'lucide-react'
import { useFindings } from '../hooks/useApi'
import { SeverityBadge, StatusBadge, TypeBadge } from '../components/SeverityBadge'
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

const DOMAIN_PILL = {
  SharePoint: 'bg-indigo-50 text-indigo-700 border border-indigo-200',
  Zscaler:    'bg-sky-50 text-sky-700 border border-sky-200',
  AWSConfig:  'bg-orange-50 text-orange-700 border border-orange-200',
}

export default function Dashboard() {
  const { findings, loading, scanning, load, runScan } = useFindings()
  const navigate = useNavigate()
  const [scanMsg, setScanMsg] = useState('')

  useEffect(() => { load() }, [load])

  const critical = findings.filter(f => f.severity === 'CRITICAL').length
  const open     = findings.filter(f => f.status === 'OPEN').length
  const resolved = findings.filter(f => f.status === 'RESOLVED').length

  async function handleScan() {
    setScanMsg('')
    await runScan()
    setScanMsg('Scan complete — findings refreshed')
    setTimeout(() => setScanMsg(''), 3000)
  }

  const recent = [...findings].sort((a, b) => new Date(b.detected_at) - new Date(a.detected_at)).slice(0, 5)

  return (
    <div className="p-6 space-y-6 max-w-6xl">
      {/* Page header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-slate-900 tracking-tight">Governance Dashboard</h1>
          <p className="text-xs text-slate-500 mt-0.5">Meridian Insurance Group · ARBITER Smart AI Governance Engine</p>
        </div>
        <button onClick={handleScan} disabled={scanning} className="btn-primary flex items-center gap-2 text-xs">
          {scanning ? <Loader2 size={13} className="animate-spin" /> : <ScanLine size={13} />}
          {scanning ? 'Scanning…' : 'Run AI Scan'}
        </button>
      </div>

      {scanning && (
        <div className="rounded-xl p-4 flex items-center gap-3 bg-indigo-50 border border-indigo-200">
          <Loader2 size={15} className="animate-spin text-indigo-600 flex-shrink-0" />
          <div>
            <p className="text-sm text-indigo-800 font-medium">AI scan in progress</p>
            <p className="text-xs text-indigo-600 mt-0.5">Dispatching specialist agents across MIG-POL-001–005 · Zscaler · AWS Config…</p>
          </div>
        </div>
      )}

      {scanMsg && (
        <div className="rounded-xl p-4 flex items-center gap-2 bg-emerald-50 border border-emerald-200">
          <ShieldCheck size={14} className="text-emerald-600" />
          <p className="text-sm text-emerald-800">{scanMsg}</p>
        </div>
      )}

      {/* Stats row */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <StatCard icon={ShieldAlert}  label="Open Conflicts"     value={open}             color="red"     />
        <StatCard icon={AlertTriangle} label="Critical Severity" value={critical} sub="Immediate action required" color="red" />
        <StatCard icon={ShieldCheck}  label="Resolved"           value={resolved}         color="emerald" />
        <StatCard icon={Clock}        label="Total Findings"     value={findings.length}  color="indigo"  />
      </div>

      {/* Domain health strip */}
      <div className="grid grid-cols-3 gap-3">
        {['SharePoint', 'Zscaler', 'AWSConfig'].map(domain => {
          const count = findings.filter(f => f.domains?.includes(domain)).length
          return (
            <div key={domain} className="rounded-xl p-4 flex items-center justify-between bg-white border border-slate-200"
                 style={{ boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }}>
              <div>
                <p className="text-[10px] text-slate-400 mb-1.5 uppercase tracking-wider">Conflicts in</p>
                <span className={`text-xs px-2 py-0.5 rounded-md font-medium ${DOMAIN_PILL[domain]}`}>{domain}</span>
              </div>
              <p className="text-3xl font-bold text-slate-900 tabular-nums">{count}</p>
            </div>
          )
        })}
      </div>

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
                onClick={() => navigate('/findings')}
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
                  <TypeBadge type={f.type} />
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
