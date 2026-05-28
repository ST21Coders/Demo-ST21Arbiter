import { useEffect, useState } from 'react'
import { Loader2, Download, Shield } from 'lucide-react'
import { useAudit } from '../hooks/useApi'
import { StatusBadge } from '../components/SeverityBadge'
import { format } from 'date-fns'

const ACTION_COLORS = {
  SCAN_TRIGGERED:    'text-indigo-700',
  CR_CREATED:        'text-amber-700',
  CR_APPROVED:       'text-emerald-700',
  CR_REJECTED:       'text-red-700',
  CR_EXECUTED:       'text-teal-700',
  CR_ESCALATED:      'text-indigo-700',
  CONFLICT_RESOLVED: 'text-emerald-700',
  KB_SYNC:           'text-slate-600',
}

export default function AuditLogs() {
  const { logs, loading, load } = useAudit()
  const [filter, setFilter] = useState('')

  useEffect(() => { load() }, [load])

  const filtered = filter
    ? logs.filter(l => l.action_type?.includes(filter) || l.resource?.includes(filter) || l.user?.includes(filter))
    : logs

  function exportCSV() {
    const headers = ['log_id','timestamp','action_type','resource','user','status','details']
    const rows = logs.map(l => headers.map(h => JSON.stringify(l[h] ?? '')).join(','))
    const blob = new Blob([headers.join(',') + '\n' + rows.join('\n')], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url; a.download = 'arbiter-audit.csv'; a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="p-6 space-y-5 max-w-6xl">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-slate-900 tracking-tight">Audit Logs</h1>
          <p className="text-xs text-slate-500 mt-0.5">Immutable record of all ARBITER system actions</p>
        </div>
        <button onClick={exportCSV} className="btn-ghost flex items-center gap-1.5 text-xs">
          <Download size={13} /> Export CSV
        </button>
      </div>

      {/* Immutability notice */}
      <div className="rounded-xl px-4 py-3 flex items-center gap-2 bg-indigo-50 border border-indigo-200">
        <Shield size={12} className="text-indigo-600 flex-shrink-0" />
        <p className="text-xs text-indigo-700">
          All entries are cryptographically signed and tamper-evident — SOX / PCI DSS §10.2.1 compliant
        </p>
      </div>

      <div className="flex gap-3">
        <input
          value={filter}
          onChange={e => setFilter(e.target.value)}
          placeholder="Search by action, resource, or user…"
          className="input flex-1 text-xs"
        />
      </div>

      {loading ? (
        <div className="flex items-center justify-center py-16">
          <Loader2 size={24} className="animate-spin text-slate-400" />
        </div>
      ) : (
        <div className="rounded-xl overflow-hidden bg-white border border-slate-200"
             style={{ boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }}>
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-slate-200 bg-slate-50">
                <th className="text-left px-4 py-3 text-slate-500 font-medium tracking-wide">Timestamp</th>
                <th className="text-left px-4 py-3 text-slate-500 font-medium tracking-wide">Action</th>
                <th className="text-left px-4 py-3 text-slate-500 font-medium tracking-wide">Resource</th>
                <th className="text-left px-4 py-3 text-slate-500 font-medium tracking-wide">User</th>
                <th className="text-left px-4 py-3 text-slate-500 font-medium tracking-wide">Status</th>
                <th className="text-left px-4 py-3 text-slate-500 font-medium tracking-wide">Details</th>
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 ? (
                <tr>
                  <td colSpan={6} className="text-center text-slate-500 py-12">No audit entries found.</td>
                </tr>
              ) : filtered.map((log, i) => (
                <tr key={log.log_id}
                    className={`transition-colors hover:bg-slate-50 ${i < filtered.length - 1 ? 'border-b border-slate-100' : ''}`}>
                  <td className="px-4 py-3 text-slate-500 font-mono whitespace-nowrap">
                    {log.timestamp ? format(new Date(log.timestamp), 'yyyy-MM-dd HH:mm:ss') : '—'}
                  </td>
                  <td className="px-4 py-3">
                    <span className={`font-medium ${ACTION_COLORS[log.action_type] || 'text-slate-700'}`}>
                      {log.action_type?.replace(/_/g, ' ')}
                    </span>
                  </td>
                  <td className="px-4 py-3 font-mono text-slate-600 max-w-[160px] truncate" title={log.resource}>
                    {log.resource}
                  </td>
                  <td className="px-4 py-3 text-slate-500 max-w-[180px] truncate" title={log.user}>
                    {log.user}
                  </td>
                  <td className="px-4 py-3">
                    <StatusBadge status={log.status} />
                  </td>
                  <td className="px-4 py-3 text-slate-500 max-w-[200px] truncate font-mono" title={log.details}>
                    {log.details}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
