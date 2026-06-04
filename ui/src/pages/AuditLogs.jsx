import { Fragment, useEffect, useState } from 'react'
import { Loader2, Download, Shield, ChevronDown, ChevronRight } from 'lucide-react'
import { useAudit } from '../hooks/useApi'
import { StatusBadge } from '../components/SeverityBadge'
import { format } from 'date-fns'

const ACTION_COLORS = {
  SCAN_TRIGGERED:    'text-indigo-700',
  SCAN_STARTED:      'text-indigo-700',
  SCAN_COMPLETED:    'text-emerald-700',
  SCAN_FAILED:       'text-red-700',
  INGESTION_COMPLETE:'text-indigo-700',
  CR_CREATED:        'text-amber-700',
  CR_APPROVED:       'text-emerald-700',
  CR_REJECTED:       'text-red-700',
  CR_EXECUTED:       'text-teal-700',
  CR_ESCALATED:      'text-indigo-700',
  CONFLICT_RESOLVED: 'text-emerald-700',
  JIRA_LINKED:       'text-sky-700',
  KB_SYNC:           'text-slate-600',
}

// Attempt JSON parse; return null on failure so we can render the raw string.
function parseDetails(d) {
  if (!d) return null
  if (typeof d === 'object') return d
  try { return JSON.parse(d) } catch { return null }
}

// Short single-line summary, derived from the parsed details when available.
// Falls back to the raw details string. Pulled from the details JSON so the
// row's "Details" column is human-readable without expanding.
function shortDetails(log) {
  const parsed = parseDetails(log.details)
  if (!parsed) return log.details || ''
  const trig = parsed.triggered_by || ''
  if (log.action_type === 'SCAN_COMPLETED' && parsed.totals) {
    const t = parsed.totals
    const auto = trig.startsWith('auto-ingest:') ? ` (auto: ${trig.replace('auto-ingest:', '')})` : ''
    return `conflicts ${t.conflicts ?? '?'} · compliant ${t.compliant ?? '?'} · critical ${t.critical ?? 0}${auto}`
  }
  if (log.action_type === 'SCAN_STARTED' && trig) {
    return trig.startsWith('auto-ingest:') ? `auto-ingest: ${trig.replace('auto-ingest:', '')}` : trig
  }
  if (log.action_type === 'JIRA_LINKED' && parsed.jira_ticket_key) {
    return parsed.jira_ticket_key
  }
  if (parsed.cr_id) return parsed.cr_id
  if (parsed.conflicts_found != null) return `conflicts_found: ${parsed.conflicts_found}`
  // Last resort: compact JSON
  return JSON.stringify(parsed)
}

function ExpandedDetail({ log }) {
  const parsed = parseDetails(log.details)
  return (
    <tr className="bg-slate-50 border-b border-slate-100">
      <td colSpan={7} className="px-4 py-3">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 text-xs">
          <div>
            <p className="text-[10px] text-slate-400 font-semibold uppercase tracking-wider mb-1">Event ID</p>
            <p className="font-mono text-slate-700 break-all">{log.event_id || log.log_id}</p>
          </div>
          <div>
            <p className="text-[10px] text-slate-400 font-semibold uppercase tracking-wider mb-1">Resource</p>
            <p className="font-mono text-slate-700 break-all">{log.resource}</p>
          </div>
          <div>
            <p className="text-[10px] text-slate-400 font-semibold uppercase tracking-wider mb-1">User</p>
            <p className="text-slate-700">{log.user}</p>
          </div>
          <div className="md:col-span-3">
            <p className="text-[10px] text-slate-400 font-semibold uppercase tracking-wider mb-1">Details (parsed JSON)</p>
            <pre className="rounded-lg p-3 bg-white border border-slate-200 font-mono text-[11px] text-slate-700 overflow-x-auto whitespace-pre-wrap">
{parsed ? JSON.stringify(parsed, null, 2) : (log.details || '(no details)')}
            </pre>
          </div>
        </div>
      </td>
    </tr>
  )
}

export default function AuditLogs() {
  const { logs, loading, load } = useAudit()
  const [filter, setFilter] = useState('')
  const [expanded, setExpanded] = useState({})

  useEffect(() => { load() }, [load])

  const filtered = filter
    ? logs.filter(l => l.action_type?.toLowerCase().includes(filter.toLowerCase())
                    || l.resource?.toLowerCase().includes(filter.toLowerCase())
                    || l.user?.toLowerCase().includes(filter.toLowerCase())
                    || (l.details || '').toLowerCase().includes(filter.toLowerCase()))
    : logs

  // Newest first — DDB scan order isn't guaranteed.
  const sorted = [...filtered].sort((a, b) => (b.timestamp || '').localeCompare(a.timestamp || ''))

  function toggle(id) {
    setExpanded(prev => ({ ...prev, [id]: !prev[id] }))
  }

  function exportCSV() {
    const headers = ['event_id','timestamp','action_type','resource','user','status','details']
    const rows = logs.map(l => headers.map(h => JSON.stringify(l[h] ?? '')).join(','))
    const blob = new Blob([headers.join(',') + '\n' + rows.join('\n')], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url; a.download = 'arbiter-audit.csv'; a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="p-6 space-y-5 page-container">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-slate-900 tracking-tight">Audit Logs</h1>
          <p className="text-xs text-slate-500 mt-0.5">Immutable record of all ARBITER system actions · click a row to expand</p>
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
          placeholder="Search by action, resource, user, or details…"
          className="input flex-1 text-xs"
        />
        <p className="text-[11px] text-slate-500 self-center flex-shrink-0">{sorted.length} row{sorted.length !== 1 ? 's' : ''}</p>
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
                <th className="w-8 px-2 py-3"></th>
                <th className="text-left px-4 py-3 text-slate-500 font-medium tracking-wide">Timestamp</th>
                <th className="text-left px-4 py-3 text-slate-500 font-medium tracking-wide">Action</th>
                <th className="text-left px-4 py-3 text-slate-500 font-medium tracking-wide">Resource</th>
                <th className="text-left px-4 py-3 text-slate-500 font-medium tracking-wide">User</th>
                <th className="text-left px-4 py-3 text-slate-500 font-medium tracking-wide">Status</th>
                <th className="text-left px-4 py-3 text-slate-500 font-medium tracking-wide">Details</th>
              </tr>
            </thead>
            <tbody>
              {sorted.length === 0 ? (
                <tr>
                  <td colSpan={7} className="text-center text-slate-500 py-12">No audit entries found.</td>
                </tr>
              ) : sorted.map((log, i) => {
                const id = log.event_id || log.log_id || `row-${i}`
                const isOpen = !!expanded[id]
                return (
                  <Fragment key={id}>
                    <tr onClick={() => toggle(id)}
                        className={`transition-colors hover:bg-slate-50 cursor-pointer ${i < sorted.length - 1 ? 'border-b border-slate-100' : ''}`}>
                      <td className="px-2 py-3 align-top">
                        {isOpen
                          ? <ChevronDown size={13} className="text-slate-500" />
                          : <ChevronRight size={13} className="text-slate-500" />}
                      </td>
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
                      <td className="px-4 py-3 text-slate-500 max-w-[260px] truncate font-mono" title={shortDetails(log)}>
                        {shortDetails(log)}
                      </td>
                    </tr>
                    {isOpen && <ExpandedDetail log={log} />}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
