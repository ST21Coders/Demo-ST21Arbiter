import { useEffect, useMemo, useState } from 'react'
import { Loader2, Download, ShieldAlert } from 'lucide-react'
import {
  ResponsiveContainer, AreaChart, Area, BarChart, Bar, Cell,
  XAxis, YAxis, Tooltip, CartesianGrid, Legend,
} from 'recharts'
import { format } from 'date-fns'
import { useTokenUsage } from '../hooks/useApi'
import { tokenUsageToCsv } from '../mockData'
import { AGENT_MODELS, modelLabel } from '../config'

// Consistent colors across the three charts + the table accents. Agent colors
// match the project's existing finding-source palette where possible.
const AGENT_COLORS = {
  master:     '#6366f1',
  sharepoint: '#0ea5e9',
  awsconfig:  '#f59e0b',
  zscaler:    '#ec4899',
}
const PERSONA_COLORS = {
  ciso:     '#f59e0b',
  soc:      '#f472b6',
  grc:      '#6366f1',
  employee: '#0ea5e9',
}

const RANGE_OPTIONS = [
  { id: 'today', label: 'Today',   days: 1  },
  { id: '7d',    label: '7 days',  days: 7  },
  { id: '30d',   label: '30 days', days: 30 },
]

function startOfRange(rangeId) {
  if (rangeId === 'today') {
    const d = new Date(); d.setHours(0, 0, 0, 0)
    return d.getTime()
  }
  const opt = RANGE_OPTIONS.find(r => r.id === rangeId) || RANGE_OPTIONS[1]
  return Date.now() - opt.days * 24 * 3600 * 1000
}

// Bucket boundary used by the time-series chart. Hourly for the "today" view
// (shows the workday shape) and daily for 7d/30d (avoids a thicket of bars).
function bucketKey(ts, granularity) {
  const d = new Date(ts)
  if (granularity === 'hour') { d.setMinutes(0, 0, 0) }
  else                        { d.setHours(0, 0, 0, 0) }
  return d.toISOString()
}

function formatTokens(n) {
  if (!n) return '0'
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M'
  if (n >= 1000)      return (n / 1000).toFixed(1) + 'K'
  return String(n)
}
function formatCost(c) { return '$' + (c || 0).toFixed(4) }

// Hard cap on rendered table rows so an unfiltered 30d view doesn't paint
// thousands of <tr>s. The CSV export uses the full filtered set.
const TABLE_ROW_CAP = 250

export default function TokenTracking() {
  const { records, summary, loading, load } = useTokenUsage()
  const [range, setRange]                 = useState('7d')
  const [agentFilter, setAgentFilter]     = useState('all')
  const [personaFilter, setPersonaFilter] = useState('all')

  useEffect(() => {
    const from = new Date(startOfRange(range)).toISOString()
    const to   = new Date().toISOString()
    load({
      from, to,
      agent:   agentFilter   === 'all' ? undefined : agentFilter,
      persona: personaFilter === 'all' ? undefined : personaFilter,
    })
  }, [load, range, agentFilter, personaFilter])

  const granularity = range === 'today' ? 'hour' : 'day'

  // Charts always derive from the already-filtered records returned by the
  // hook — no second filter pass needed and no risk of UI/data drift.
  const timeSeries = useMemo(() => {
    const buckets = new Map()
    for (const r of records) {
      const key = bucketKey(r.timestamp, granularity)
      const cur = buckets.get(key) || { ts: key, input: 0, output: 0 }
      cur.input  += r.input_tokens
      cur.output += r.output_tokens
      buckets.set(key, cur)
    }
    return Array.from(buckets.values()).sort((a, b) => a.ts.localeCompare(b.ts))
  }, [records, granularity])

  const byAgent = useMemo(() => {
    const acc = {}
    for (const r of records) acc[r.agent] = (acc[r.agent] || 0) + r.total_tokens
    return ['master', 'sharepoint', 'awsconfig', 'zscaler']
      .map(a => ({ agent: a, total: acc[a] || 0 }))
  }, [records])

  const byPersona = useMemo(() => {
    const acc = {}
    for (const r of records) acc[r.persona] = (acc[r.persona] || 0) + r.total_tokens
    return ['ciso', 'soc', 'grc', 'employee']
      .map(p => ({ persona: p, total: acc[p] || 0 }))
  }, [records])

  function exportCSV() {
    const csv = tokenUsageToCsv(records)
    const blob = new Blob([csv], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `arbiter-token-usage-${range}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  const visibleRows = records.slice(0, TABLE_ROW_CAP)

  return (
    <div className="p-6 space-y-5 page-container">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-slate-900 tracking-tight">Token Tracking</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            Per-invocation Bedrock usage across the four AgentCore Runtimes · CISO governance view
          </p>
        </div>
        <button onClick={exportCSV} className="btn-ghost flex items-center gap-1.5 text-xs">
          <Download size={13} /> Export CSV
        </button>
      </div>

      {/* KPI strip */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
        <KpiCard
          label="Tokens (range)"
          value={formatTokens(summary.totalTokens)}
          subtext={`${formatTokens(summary.inputTokens)} in · ${formatTokens(summary.outputTokens)} out`}
        />
        <KpiCard
          label="Estimated cost"
          value={formatCost(summary.totalCost)}
          subtext={`${modelLabel(AGENT_MODELS.master)} list pricing`}
        />
        <KpiCard
          label="Avg tokens / chat"
          value={formatTokens(summary.avgPerChat)}
          subtext={`${summary.chats} chats`}
        />
        <KpiCard
          label="Guardrail-blocked"
          value={String(summary.blocked)}
          subtext="input billed · output suppressed"
          tone={summary.blocked > 0 ? 'warn' : 'ok'}
        />
      </div>

      {/* Filter bar */}
      <div className="flex gap-2 items-center flex-wrap">
        <div className="inline-flex rounded-lg border border-slate-200 bg-white p-0.5">
          {RANGE_OPTIONS.map(r => (
            <button
              key={r.id}
              onClick={() => setRange(r.id)}
              className={`text-[11px] px-3 py-1 rounded-md font-medium transition-colors ${
                range === r.id ? 'bg-indigo-50 text-indigo-700' : 'text-slate-600 hover:bg-slate-50'
              }`}
            >
              {r.label}
            </button>
          ))}
        </div>
        <select value={agentFilter} onChange={e => setAgentFilter(e.target.value)} className="input text-xs">
          <option value="all">All agents</option>
          <option value="master">Master</option>
          <option value="sharepoint">SharePoint</option>
          <option value="awsconfig">AWS Config</option>
          <option value="zscaler">Zscaler</option>
        </select>
        <select value={personaFilter} onChange={e => setPersonaFilter(e.target.value)} className="input text-xs">
          <option value="all">All personas</option>
          <option value="ciso">CISO</option>
          <option value="soc">SOC</option>
          <option value="grc">GRC</option>
          <option value="employee">Employee</option>
        </select>
        <p className="text-[11px] text-slate-500 ml-auto">
          {records.length} row{records.length !== 1 ? 's' : ''}
        </p>
      </div>

      {/* Charts */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
        <ChartCard title="Tokens over time" subtitle={granularity === 'hour' ? 'hourly' : 'daily'}>
          <ResponsiveContainer width="100%" height={220}>
            <AreaChart data={timeSeries} margin={{ top: 8, right: 12, bottom: 0, left: -16 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
              <XAxis
                dataKey="ts"
                tickFormatter={t => format(new Date(t), granularity === 'hour' ? 'HH:mm' : 'MM-dd')}
                tick={{ fontSize: 10, fill: '#64748b' }}
                axisLine={false}
              />
              <YAxis tickFormatter={formatTokens} tick={{ fontSize: 10, fill: '#64748b' }} axisLine={false} />
              <Tooltip
                contentStyle={{ fontSize: 11 }}
                labelFormatter={l => format(new Date(l), granularity === 'hour' ? 'yyyy-MM-dd HH:mm' : 'yyyy-MM-dd')}
                formatter={v => formatTokens(v)}
              />
              <Legend wrapperStyle={{ fontSize: 11, paddingTop: 4 }} iconSize={10} />
              <Area type="monotone" dataKey="input"  name="Input"  stackId="1" stroke="#6366f1" fill="#6366f1" fillOpacity={0.55} />
              <Area type="monotone" dataKey="output" name="Output" stackId="1" stroke="#0ea5e9" fill="#0ea5e9" fillOpacity={0.55} />
            </AreaChart>
          </ResponsiveContainer>
        </ChartCard>

        <ChartCard title="Tokens by agent" subtitle="sum across range">
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={byAgent} margin={{ top: 8, right: 12, bottom: 0, left: -16 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
              <XAxis dataKey="agent" tick={{ fontSize: 10, fill: '#64748b' }} axisLine={false} />
              <YAxis tickFormatter={formatTokens} tick={{ fontSize: 10, fill: '#64748b' }} axisLine={false} />
              <Tooltip contentStyle={{ fontSize: 11 }} formatter={v => formatTokens(v)} />
              <Bar dataKey="total" name="Tokens">
                {byAgent.map((r, i) => <Cell key={i} fill={AGENT_COLORS[r.agent]} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>

        <ChartCard title="Tokens by persona" subtitle="sum across range">
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={byPersona} margin={{ top: 8, right: 12, bottom: 0, left: -16 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
              <XAxis dataKey="persona" tick={{ fontSize: 10, fill: '#64748b' }} axisLine={false} />
              <YAxis tickFormatter={formatTokens} tick={{ fontSize: 10, fill: '#64748b' }} axisLine={false} />
              <Tooltip contentStyle={{ fontSize: 11 }} formatter={v => formatTokens(v)} />
              <Bar dataKey="total" name="Tokens">
                {byPersona.map((r, i) => <Cell key={i} fill={PERSONA_COLORS[r.persona]} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>
      </div>

      {/* Records table */}
      {loading ? (
        <div className="flex items-center justify-center py-16">
          <Loader2 size={24} className="animate-spin text-slate-400" />
        </div>
      ) : (
        <div
          className="rounded-xl overflow-hidden bg-white border border-slate-200"
          style={{ boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }}
        >
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-slate-200 bg-slate-50">
                <th className="text-left  px-4 py-3 text-slate-500 font-medium tracking-wide">Timestamp</th>
                <th className="text-left  px-4 py-3 text-slate-500 font-medium tracking-wide">Agent</th>
                <th className="text-left  px-4 py-3 text-slate-500 font-medium tracking-wide">Persona</th>
                <th className="text-left  px-4 py-3 text-slate-500 font-medium tracking-wide">User</th>
                <th className="text-left  px-4 py-3 text-slate-500 font-medium tracking-wide">Session</th>
                <th className="text-right px-4 py-3 text-slate-500 font-medium tracking-wide">Input</th>
                <th className="text-right px-4 py-3 text-slate-500 font-medium tracking-wide">Output</th>
                <th className="text-right px-4 py-3 text-slate-500 font-medium tracking-wide">Total</th>
                <th className="text-right px-4 py-3 text-slate-500 font-medium tracking-wide">Cost</th>
              </tr>
            </thead>
            <tbody>
              {visibleRows.length === 0 ? (
                <tr>
                  <td colSpan={9} className="text-center text-slate-500 py-12">
                    No usage records in this range.
                  </td>
                </tr>
              ) : visibleRows.map((r, i) => (
                <tr
                  key={r.sk || `${r.timestamp}-${r.agent}-${i}`}
                  className={`hover:bg-slate-50 ${i < visibleRows.length - 1 ? 'border-b border-slate-100' : ''} ${r.guardrail_blocked ? 'bg-amber-50' : ''}`}
                >
                  <td className="px-4 py-2 text-slate-500 font-mono whitespace-nowrap">
                    {format(new Date(r.timestamp), 'yyyy-MM-dd HH:mm:ss')}
                  </td>
                  <td className="px-4 py-2">
                    <span className="font-medium" style={{ color: AGENT_COLORS[r.agent] || '#475569' }}>
                      {r.agent}
                    </span>
                    {r.guardrail_blocked && (
                      <span className="inline-flex items-center gap-1 ml-2 text-[9px] uppercase tracking-wider font-bold text-amber-700">
                        <ShieldAlert size={10} /> blocked
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-2">
                    <span
                      className="font-semibold uppercase text-[10px] tracking-wider"
                      style={{ color: PERSONA_COLORS[r.persona] || '#475569' }}
                    >
                      {r.persona}
                    </span>
                  </td>
                  <td className="px-4 py-2 text-slate-500 max-w-[200px] truncate" title={r.user_email}>
                    {r.user_email}
                  </td>
                  <td className="px-4 py-2 text-slate-500 font-mono max-w-[140px] truncate" title={r.session_id}>
                    {r.session_id}
                  </td>
                  <td className="px-4 py-2 text-right text-slate-700 font-mono tabular-nums">
                    {r.input_tokens.toLocaleString()}
                  </td>
                  <td className="px-4 py-2 text-right text-slate-700 font-mono tabular-nums">
                    {r.output_tokens.toLocaleString()}
                  </td>
                  <td className="px-4 py-2 text-right text-slate-900 font-mono font-semibold tabular-nums">
                    {r.total_tokens.toLocaleString()}
                  </td>
                  <td className="px-4 py-2 text-right text-slate-600 font-mono tabular-nums">
                    {formatCost(r.estimated_cost)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {records.length > TABLE_ROW_CAP && (
            <div className="px-4 py-2 bg-slate-50 border-t border-slate-200 text-[11px] text-slate-500 text-center">
              Showing {TABLE_ROW_CAP} of {records.length} rows · export CSV for the full set
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function KpiCard({ label, value, subtext, tone = 'normal' }) {
  const accent =
    tone === 'warn' ? 'border-amber-200   bg-amber-50'
    : tone === 'ok' ? 'border-emerald-200 bg-emerald-50'
    :                 'border-slate-200   bg-white'
  const valueColor =
    tone === 'warn' ? 'text-amber-700'
    : tone === 'ok' ? 'text-emerald-700'
    :                 'text-slate-900'
  return (
    <div className={`rounded-xl border px-4 py-3 ${accent}`}>
      <p className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold">{label}</p>
      <p className={`text-2xl font-bold mt-1 ${valueColor}`}>{value}</p>
      {subtext && <p className="text-[10px] text-slate-500 mt-1">{subtext}</p>}
    </div>
  )
}

function ChartCard({ title, subtitle, children }) {
  return (
    <div
      className="rounded-xl bg-white border border-slate-200 p-3"
      style={{ boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }}
    >
      <div className="flex items-baseline justify-between mb-2">
        <p className="text-xs font-semibold text-slate-700">{title}</p>
        {subtitle && <p className="text-[10px] text-slate-400">{subtitle}</p>}
      </div>
      {children}
    </div>
  )
}
