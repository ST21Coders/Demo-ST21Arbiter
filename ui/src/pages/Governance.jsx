import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  CheckCircle, XCircle, AlertCircle, ChevronDown, ChevronRight, Zap, Download,
} from 'lucide-react'
import {
  CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { useFindings, useChangeRequests } from '../hooks/useApi'
import ActionRequestModal from '../components/ActionRequestModal'
import {
  SCORE_TREND_POINTS,
  SCORE_TREND_SERIES,
  TREND_RANGES,
  filterScoreTrend,
  frameworkSummaries,
} from '../lib/governanceScoring'

function scoreColor(score) {
  if (score >= 80) return '#059669'
  if (score >= 70) return '#d97706'
  return '#dc2626'
}

function StatusIcon({ status }) {
  if (status === 'PASS') return <CheckCircle size={14} className="text-emerald-600 flex-shrink-0" />
  if (status === 'FAIL') return <XCircle size={14} className="text-red-600 flex-shrink-0" />
  return <AlertCircle size={14} className="text-amber-600 flex-shrink-0" />
}

function FrameworkScoreCard({ framework }) {
  const color = scoreColor(framework.score)
  return (
    <div className="rounded-xl border border-slate-200 bg-white px-4 py-4 shadow-sm min-w-[210px]">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-semibold text-slate-600 tracking-wide truncate">{framework.name}</p>
          <p className="text-[11px] text-slate-400 mt-1">
            {framework.openCount} open · {framework.criticalCount} critical
          </p>
        </div>
        <span className="h-2 w-2 rounded-full mt-1.5 flex-shrink-0" style={{ background: framework.accent }} />
      </div>

      <div className="mt-3">
        <p className="text-4xl font-bold leading-none" style={{ color }}>{framework.score}</p>
        <div className="mt-3 h-1.5 rounded-full bg-slate-100 overflow-hidden">
          <div className="h-full rounded-full" style={{ width: `${framework.score}%`, background: color }} />
        </div>
      </div>

      <div className="mt-3 flex items-center justify-between gap-2 text-[11px] text-slate-400">
        <span>{framework.highCount} high · {framework.mediumCount} medium</span>
        <ChevronDown size={13} />
      </div>
    </div>
  )
}

function ScoreTrend({ points }) {
  const [range, setRange] = useState('6M')
  const data = useMemo(() => filterScoreTrend(points, range), [points, range])
  const first = data[0]?.month || ''
  const last = data[data.length - 1]?.month || ''

  return (
    <div className="rounded-xl border border-slate-200 bg-white shadow-sm p-4">
      <div className="flex flex-wrap items-start justify-between gap-3 mb-4">
        <div>
          <p className="text-sm font-semibold text-slate-900">Score trend</p>
          <p className="text-xs text-slate-500 mt-0.5">
            {data.length} data points{first && last ? ` · ${first} → ${last}` : ''}
          </p>
        </div>
        <div className="flex items-center gap-1 rounded-lg bg-slate-100 p-1">
          {TREND_RANGES.map(option => (
            <button
              key={option}
              onClick={() => setRange(option)}
              className={`px-2.5 py-1 text-[11px] font-semibold rounded-md transition-colors ${
                range === option ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500 hover:text-slate-800'
              }`}
            >
              {option}
            </button>
          ))}
        </div>
      </div>

      <div className="h-64">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 8, right: 18, left: -18, bottom: 2 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
            <XAxis dataKey="month" tick={{ fontSize: 11, fill: '#64748b' }} tickLine={false} axisLine={false} />
            <YAxis domain={[50, 80]} tick={{ fontSize: 11, fill: '#64748b' }} tickLine={false} axisLine={false} />
            <Tooltip
              contentStyle={{ borderRadius: 8, border: '1px solid #e2e8f0', boxShadow: '0 8px 24px rgba(15,23,42,0.12)' }}
              labelStyle={{ color: '#0f172a', fontWeight: 700 }}
            />
            {SCORE_TREND_SERIES.map(series => (
              <Line
                key={series.key}
                type="monotone"
                dataKey={series.key}
                name={series.name}
                stroke={series.color}
                strokeWidth={2.5}
                dot={{ r: 3, strokeWidth: 1.5 }}
                activeDot={{ r: 5 }}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>

      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-2">
        {SCORE_TREND_SERIES.map(series => (
          <div key={series.key} className="flex items-center gap-1.5 text-[11px] text-slate-500">
            <span className="h-2 w-2 rounded-full" style={{ background: series.color }} />
            {series.name}
          </div>
        ))}
      </div>
    </div>
  )
}

export default function Governance() {
  const navigate = useNavigate()
  const { findings, load: loadFindings } = useFindings()
  const { createAction } = useChangeRequests()
  const [actionTarget, setActionTarget] = useState(null)

  useEffect(() => { loadFindings() }, [loadFindings])

  const summaries = useMemo(() => frameworkSummaries(findings), [findings])
  const openCritical = findings.filter(f => f.severity === 'CRITICAL' && f.status === 'OPEN').length

  function openCRForControl(eval_) {
    if (eval_.linked) {
      setActionTarget(eval_.linked)
    } else {
      setActionTarget({
        conflict_id: eval_.uc || `MANUAL-${eval_.ctrl.id}`,
        title: `${eval_.ctrl.id} ${eval_.ctrl.name}`,
        severity: 'HIGH',
        source_policy: eval_.ctrl.id,
        domains: [],
      })
    }
  }

  return (
    <div className="p-6 space-y-6 page-container">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-lg font-bold text-slate-900 tracking-tight">Governance & Compliance</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            Framework score cards, control posture, and historical score trends from ARBITER findings.
          </p>
        </div>
      </div>

      {openCritical > 0 && (
        <div className="rounded-xl p-4 flex items-center gap-3 bg-red-50 border border-red-200"
             style={{ borderLeft: '3px solid #ef4444' }}>
          <XCircle size={14} className="text-red-600 flex-shrink-0" />
          <p className="text-sm text-red-800 font-medium">
            {openCritical} critical open conflict{openCritical !== 1 ? 's' : ''} actively degrading compliance posture
          </p>
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-5 gap-3">
        {summaries.map(summary => <FrameworkScoreCard key={summary.id} framework={summary} />)}
      </div>

      <ScoreTrend points={SCORE_TREND_POINTS} />

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {summaries.map(fw => (
          <div key={fw.id} className="rounded-xl p-4 bg-white border border-slate-200 shadow-sm">
            <div className="flex items-center gap-4 mb-4">
              <div className="flex h-16 w-16 flex-col items-center justify-center rounded-full border-4 bg-white"
                   style={{ borderColor: `${scoreColor(fw.score)}33` }}>
                <span className="text-lg font-bold" style={{ color: scoreColor(fw.score) }}>{fw.score}</span>
                <span className="text-[10px] text-slate-400">score</span>
              </div>
              <div className="flex-1 min-w-0">
                <p className="font-semibold text-slate-900">{fw.name}</p>
                <p className="text-xs text-slate-500 mt-0.5">
                  <span className="text-emerald-700">{fw.passCount} passing</span>
                  {' · '}
                  <span className="text-red-700">{fw.openCount} failing</span>
                </p>
              </div>
              <button
                disabled
                title={`Export ${fw.name} report (PDF)`}
                className="btn-ghost flex items-center gap-1 text-[10px] px-2 py-1 opacity-50 cursor-not-allowed flex-shrink-0"
              >
                <Download size={11} /> PDF
              </button>
            </div>

            <div className="space-y-0">
              {fw.evals.map((e, i) => {
                const clickable = !!e.uc
                const showCR = e.status === 'FAIL'
                return (
                  <div
                    key={e.ctrl.id}
                    onClick={clickable ? () => navigate(`/findings/${e.uc}`) : undefined}
                    className={`flex items-start gap-2.5 py-2 transition-colors ${i < fw.evals.length - 1 ? 'border-b border-slate-100' : ''} ${clickable ? 'cursor-pointer hover:bg-slate-50 rounded-md -mx-1.5 px-1.5' : ''}`}
                  >
                    <StatusIcon status={e.status} />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-xs font-mono text-slate-400">{e.ctrl.id}</span>
                        <span className="text-xs text-slate-800">{e.ctrl.name}</span>
                        {e.linked?.severity && (
                          <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded ${
                            e.linked.severity === 'CRITICAL' ? 'bg-red-50 text-red-700 border border-red-200' :
                            e.linked.severity === 'HIGH'     ? 'bg-orange-50 text-orange-700 border border-orange-200' :
                            e.linked.severity === 'MEDIUM'   ? 'bg-amber-50 text-amber-700 border border-amber-200' :
                                                               'bg-emerald-50 text-emerald-700 border border-emerald-200'
                          }`}>{e.linked.severity}</span>
                        )}
                        {e.linked && e.linked.status !== 'OPEN' && (
                          <span className="text-[10px] text-emerald-700 bg-emerald-50 border border-emerald-200 px-1.5 py-0.5 rounded">
                            {e.linked.status}
                          </span>
                        )}
                      </div>
                      {e.ctrl.note && <p className="text-xs text-slate-500 mt-0.5">{e.ctrl.note}</p>}
                    </div>
                    <div className="flex items-center gap-2 flex-shrink-0">
                      {showCR && (
                        <button
                          onClick={(ev) => { ev.stopPropagation(); openCRForControl(e) }}
                          className="btn-primary flex items-center gap-1 text-[10px] px-2 py-1"
                          title={e.linked ? `Open CR for ${e.uc}` : 'Create a manual remediation CR'}
                        >
                          <Zap size={10} /> Open CR
                        </button>
                      )}
                      {clickable && <ChevronRight size={13} className="text-slate-300" />}
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        ))}
      </div>

      {actionTarget && (
        <ActionRequestModal
          conflict={actionTarget}
          onClose={result => {
            setActionTarget(null)
            if (result) loadFindings()
          }}
          onCreate={createAction}
        />
      )}
    </div>
  )
}
