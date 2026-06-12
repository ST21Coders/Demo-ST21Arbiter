import { useEffect, useMemo, useState } from 'react'
import {
  FileText, FileSpreadsheet, Package, Table, ScrollText, ShieldCheck,
  Loader2, Download, Search, Play, History, AlertTriangle,
} from 'lucide-react'
import { useReports, triggerDownload } from '../hooks/useApi'
import { USE_MOCK } from '../config'
import { formatDistanceToNow } from 'date-fns'

const ICONS = { FileText, FileSpreadsheet, Package, Table, ScrollText, ShieldCheck }

const CATEGORY_COLORS = {
  Compliance: { bg: '#eef2ff', text: '#4338ca', border: '#c7d2fe' },
  Audit:      { bg: '#fff7ed', text: '#c2410c', border: '#fed7aa' },
  Risk:       { bg: '#fef2f2', text: '#b91c1c', border: '#fecaca' },
}

function fmtBytes(n) {
  if (n == null) return '—'
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  if (n >= 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${n} B`
}

function ReportListItem({ report, selected, onClick }) {
  const Icon = ICONS[report.icon] || FileText
  const cat = CATEGORY_COLORS[report.category] || CATEGORY_COLORS.Compliance
  return (
    <button
      onClick={onClick}
      className={`w-full text-left px-3 py-2.5 rounded-lg border transition-colors flex items-start gap-2.5 ${
        selected ? 'bg-white border-indigo-300 shadow-sm' : 'bg-white/40 border-transparent hover:bg-white hover:border-slate-200'
      }`}
    >
      <div className="w-7 h-7 rounded-md flex items-center justify-center flex-shrink-0 mt-0.5"
           style={{ background: cat.bg, color: cat.text, border: `1px solid ${cat.border}` }}>
        <Icon size={13} />
      </div>
      <div className="flex-1 min-w-0">
        <p className={`text-xs font-semibold truncate ${selected ? 'text-slate-900' : 'text-slate-700'}`}>{report.title}</p>
        <p className="text-[10px] text-slate-500 mt-0.5">{report.category} · {report.formats.length} format{report.formats.length !== 1 ? 's' : ''}</p>
      </div>
    </button>
  )
}

function ParamField({ param, value, onChange }) {
  if (param.type !== 'multi_select') return null
  const current = Array.isArray(value) ? value : (param.default || [])
  return (
    <div>
      <label className="block text-[11px] font-semibold uppercase tracking-wider text-slate-500 mb-1.5">{param.label}</label>
      <div className="flex flex-wrap gap-1.5">
        {param.options.map(o => {
          const on = current.includes(o.id)
          return (
            <button
              key={o.id}
              type="button"
              onClick={() => {
                const next = on ? current.filter(x => x !== o.id) : [...current, o.id]
                onChange(next.length ? next : param.default)
              }}
              className={`text-[11px] px-2.5 py-1 rounded-full border font-medium transition-colors ${
                on ? 'bg-indigo-600 text-white border-indigo-600' : 'bg-white text-slate-600 border-slate-200 hover:border-slate-300'
              }`}
            >
              {o.label}
            </button>
          )
        })}
      </div>
    </div>
  )
}

function ReportDetail({ report, onGenerate, generating }) {
  const Icon = ICONS[report.icon] || FileText
  const cat = CATEGORY_COLORS[report.category] || CATEGORY_COLORS.Compliance
  const busy = generating === report.id

  const [format, setFormat] = useState(report.default_format || report.formats[0])
  const [paramValues, setParamValues] = useState({})

  useEffect(() => {
    setFormat(report.default_format || report.formats[0])
    const init = {}
    for (const p of (report.parameters || [])) init[p.id] = p.default
    setParamValues(init)
  }, [report.id])

  return (
    <div className="card flex flex-col">
      <div className="flex items-start gap-3 mb-4">
        <div className="w-12 h-12 rounded-lg flex items-center justify-center flex-shrink-0"
             style={{ background: cat.bg, color: cat.text, border: `1px solid ${cat.border}` }}>
          <Icon size={22} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h2 className="text-base font-bold text-slate-900">{report.title}</h2>
            <span className="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded"
                  style={{ background: cat.bg, color: cat.text, border: `1px solid ${cat.border}` }}>
              {report.category}
            </span>
          </div>
          <p className="text-xs text-slate-500 mt-0.5">{report.audience}</p>
        </div>
      </div>

      <p className="text-sm text-slate-700 leading-relaxed mb-4">{report.description}</p>

      <div className="space-y-4 p-4 rounded-lg bg-slate-50 border border-slate-200 mb-4">
        <div>
          <label className="block text-[11px] font-semibold uppercase tracking-wider text-slate-500 mb-1.5">Format</label>
          <div className="flex flex-wrap gap-1.5">
            {report.formats.map(f => (
              <button
                key={f}
                type="button"
                onClick={() => setFormat(f)}
                className={`text-[11px] px-2.5 py-1 rounded-md border font-semibold uppercase tracking-wider transition-colors ${
                  format === f ? 'bg-slate-900 text-white border-slate-900' : 'bg-white text-slate-600 border-slate-200 hover:border-slate-300'
                }`}
              >
                {f}
              </button>
            ))}
          </div>
        </div>
        {(report.parameters || []).map(p => (
          <ParamField key={p.id} param={p} value={paramValues[p.id]}
                      onChange={(v) => setParamValues(prev => ({ ...prev, [p.id]: v }))} />
        ))}
      </div>

      <button
        onClick={() => onGenerate(report, format, paramValues)}
        disabled={busy}
        className="btn-primary flex items-center justify-center gap-2 text-sm py-2.5"
      >
        {busy ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
        {busy ? 'Generating…' : `Generate & download ${format.toUpperCase()}`}
      </button>
      <p className="text-[10px] text-slate-400 mt-1.5 text-center">
        Estimated ~{report.estimated_seconds}s · saved to the reports bucket · download starts automatically
      </p>
    </div>
  )
}

function RecentRuns({ runs, error }) {
  if (error) {
    return (
      <div className="card flex items-center gap-2 text-sm text-red-700 bg-red-50 border-red-200">
        <AlertTriangle size={14} /> {error}
      </div>
    )
  }
  if (!runs.length) return null
  return (
    <div className="card">
      <p className="text-xs font-semibold text-slate-600 uppercase tracking-wider flex items-center gap-1.5 mb-3">
        <History size={11} /> Generated this session
      </p>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="bg-slate-50 border-b border-slate-200">
              <th className="text-left px-3 py-2 text-slate-500 font-medium">Report</th>
              <th className="text-left px-3 py-2 text-slate-500 font-medium">Format</th>
              <th className="text-left px-3 py-2 text-slate-500 font-medium">When</th>
              <th className="text-left px-3 py-2 text-slate-500 font-medium">Size</th>
              <th className="text-right px-3 py-2 text-slate-500 font-medium"></th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r, i) => (
              <tr key={r.key} className={i < runs.length - 1 ? 'border-b border-slate-100' : ''}>
                <td className="px-3 py-2 font-medium text-slate-800">{r.report_title}</td>
                <td className="px-3 py-2">
                  <span className="text-[10px] font-mono font-bold bg-slate-100 border border-slate-200 px-1.5 py-0.5 rounded">{(r.format || '').toUpperCase()}</span>
                </td>
                <td className="px-3 py-2 text-slate-500">{formatDistanceToNow(new Date(r.at), { addSuffix: true })}</td>
                <td className="px-3 py-2 text-slate-500 tabular-nums">{fmtBytes(r.size_bytes)}</td>
                <td className="px-3 py-2 text-right">
                  <button onClick={() => triggerDownload(r.download_url, r.filename)}
                          className="text-indigo-600 hover:text-indigo-700 text-xs font-semibold inline-flex items-center gap-1">
                    <Download size={11} /> Download
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export default function Reports() {
  const { catalog, loadingCatalog, loadCatalog, generate } = useReports()
  const [generating, setGenerating] = useState(null)
  const [filterCategory, setFilterCategory] = useState('')
  const [search, setSearch] = useState('')
  const [selectedId, setSelectedId] = useState(null)
  const [runs, setRuns] = useState([])
  const [error, setError] = useState('')

  useEffect(() => { loadCatalog() }, [loadCatalog])

  const items = catalog?.catalog || []
  const categories = catalog?.categories || []

  const filtered = useMemo(() => {
    let out = items
    if (filterCategory) out = out.filter(r => r.category === filterCategory)
    if (search.trim()) {
      const q = search.trim().toLowerCase()
      out = out.filter(r =>
        r.title.toLowerCase().includes(q) ||
        (r.description || '').toLowerCase().includes(q) ||
        (r.audience || '').toLowerCase().includes(q) ||
        (r.tags || []).some(t => t.toLowerCase().includes(q))
      )
    }
    return out
  }, [items, filterCategory, search])

  useEffect(() => {
    if (filtered.length === 0) { setSelectedId(null); return }
    if (!filtered.find(r => r.id === selectedId)) setSelectedId(filtered[0].id)
  }, [filtered, selectedId])

  const selected = items.find(r => r.id === selectedId)

  async function handleGenerate(report, format, params) {
    setGenerating(report.id)
    setError('')
    try {
      const data = await generate(report.id, format, params)
      if (data?.download_url) triggerDownload(data.download_url, data.filename)
      setRuns(prev => [{
        key: `${report.id}-${Date.now()}`,
        report_title: data?.report_title || report.title,
        format: data?.format || format,
        filename: data?.filename,
        size_bytes: data?.size_bytes,
        download_url: data?.download_url,
        at: new Date().toISOString(),
      }, ...prev].slice(0, 10))
    } catch (e) {
      setError(`Report generation failed: ${e.message}`)
    } finally {
      setGenerating(null)
    }
  }

  return (
    <div className="p-6 space-y-4 page-container">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-lg font-bold text-slate-900 tracking-tight">Reports</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            Compliance, audit and risk reports. Pick a report, choose a format, and the file downloads automatically.
            {USE_MOCK && ' (Mock mode: files are generated client-side as CSV/JSON.)'}
          </p>
        </div>
      </div>

      <div className="grid grid-cols-12 gap-4">
        <div className="col-span-12 lg:col-span-4 xl:col-span-3 space-y-3">
          <div className="relative">
            <Search size={13} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400 pointer-events-none" />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search reports…"
              className="input text-sm pl-9 w-full"
            />
          </div>

          <div className="flex flex-wrap gap-1.5">
            <button
              onClick={() => setFilterCategory('')}
              className={`text-[10px] font-semibold px-2 py-1 rounded-full border transition-colors ${
                !filterCategory ? 'bg-slate-900 text-white border-slate-900' : 'bg-white text-slate-600 border-slate-200 hover:border-slate-300'
              }`}
            >
              All ({items.length})
            </button>
            {categories.map(cat => {
              const n = items.filter(r => r.category === cat).length
              const c = CATEGORY_COLORS[cat] || CATEGORY_COLORS.Compliance
              const active = filterCategory === cat
              return (
                <button
                  key={cat}
                  onClick={() => setFilterCategory(filterCategory === cat ? '' : cat)}
                  className="text-[10px] font-semibold px-2 py-1 rounded-full border transition-colors"
                  style={active ? { background: c.text, color: '#fff', borderColor: c.text } : { background: c.bg, color: c.text, borderColor: c.border }}
                >
                  {cat} ({n})
                </button>
              )
            })}
          </div>

          <div className="space-y-1 bg-slate-100/60 rounded-xl p-2 border border-slate-200">
            {loadingCatalog && !catalog ? (
              <div className="py-8 text-center"><Loader2 size={16} className="animate-spin text-slate-400 mx-auto" /></div>
            ) : filtered.length === 0 ? (
              <p className="text-xs text-slate-400 py-6 text-center">No reports match.</p>
            ) : filtered.map(r => (
              <ReportListItem key={r.id} report={r} selected={selectedId === r.id} onClick={() => setSelectedId(r.id)} />
            ))}
          </div>
        </div>

        <div className="col-span-12 lg:col-span-8 xl:col-span-9 space-y-4">
          {selected ? (
            <ReportDetail report={selected} onGenerate={handleGenerate} generating={generating} />
          ) : (
            <div className="card text-center py-16"><p className="text-sm text-slate-400">Select a report from the left.</p></div>
          )}
          <RecentRuns runs={runs} error={error} />
        </div>
      </div>
    </div>
  )
}
