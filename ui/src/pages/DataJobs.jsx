import { useCallback, useEffect, useState } from 'react'
import {
  Boxes, Database, FileText, CheckCircle, AlertTriangle, Loader2, Clock, RefreshCw,
} from 'lucide-react'
import { formatDistanceToNow } from 'date-fns'
import { listDataJobs } from '../hooks/useApi'

// Data-ingest jobs list (point 7). Backs the async S3-Vectors worker that
// DocuSearch (unstructured) + Structured Analytics (tabular) submit from the
// Data Pipeline page. Polls GET /data-jobs every 5s while any job is non-terminal
// so completions surface without a manual refresh.

const TERMINAL = new Set(['SUCCEEDED', 'FAILED'])

const STATUS_STYLE = {
  QUEUED:    { bg: '#f1f5f9', border: '#e2e8f0', text: '#64748b', Icon: Clock },
  RUNNING:   { bg: '#fffbeb', border: '#fde68a', text: '#b45309', Icon: Loader2 },
  SUCCEEDED: { bg: '#ecfdf5', border: '#a7f3d0', text: '#047857', Icon: CheckCircle },
  FAILED:    { bg: '#fef2f2', border: '#fecaca', text: '#b91c1c', Icon: AlertTriangle },
}

const JOB_TYPE_LABEL = {
  docusearch: { label: 'DocuSearch', Icon: Boxes, hint: 'Unstructured → S3 Vectors' },
  structured_analytics: { label: 'Structured Analytics', Icon: Database, hint: 'Tabular → Glue + S3 Vectors' },
}

function StatusPill({ status }) {
  const s = STATUS_STYLE[status] || STATUS_STYLE.QUEUED
  const Icon = s.Icon
  return (
    <span className="inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-1 rounded-md border whitespace-nowrap"
          style={{ background: s.bg, borderColor: s.border, color: s.text }}>
      <Icon size={12} className={status === 'RUNNING' ? 'animate-spin' : ''} />
      {status || 'QUEUED'}
    </span>
  )
}

// Worker result payloads differ by job type: docusearch → {documents, chunks,
// vectors, files}; structured → {rows, facts, vectors, files}. Render whatever
// numeric counts are present, in a stable order.
function ResultSummary({ result }) {
  if (!result || typeof result !== 'object') return <span className="text-slate-400">—</span>
  const order = ['files', 'documents', 'rows', 'chunks', 'facts', 'vectors']
  const parts = order
    .filter(k => result[k] !== undefined && result[k] !== null)
    .map(k => `${Number(result[k])} ${k}`)
  return parts.length
    ? <span className="text-slate-600">{parts.join(' · ')}</span>
    : <span className="text-slate-400">—</span>
}

function JobRow({ job }) {
  const type = JOB_TYPE_LABEL[job.job_type] || { label: job.job_type || 'ingest', Icon: FileText, hint: '' }
  const TypeIcon = type.Icon
  const created = job.created_at ? new Date(job.created_at) : null
  return (
    <div className="rounded-xl p-4 bg-white border border-slate-200"
         style={{ boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }}>
      <div className="flex items-center justify-between gap-3 mb-2">
        <div className="flex items-center gap-2 min-w-0">
          <div className="w-8 h-8 rounded-lg bg-violet-50 border border-violet-100 flex items-center justify-center flex-shrink-0">
            <TypeIcon size={15} className="text-violet-600" />
          </div>
          <div className="min-w-0">
            <p className="text-sm font-semibold text-slate-900 truncate">
              {type.label}
              {job.group_name && <span className="font-normal text-slate-500"> · {job.group_name}</span>}
            </p>
            <p className="text-[11px] text-slate-500 mt-0.5 truncate">
              {job.project_id ? <>Project <span className="font-medium text-slate-600">{job.project_id}</span> · </> : null}
              {created ? formatDistanceToNow(created, { addSuffix: true }) : ''}
              {job.vector_index && <> · index <span className="font-mono">{job.vector_index}</span></>}
            </p>
          </div>
        </div>
        <StatusPill status={job.status} />
      </div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px]">
        <span className="text-slate-400">Job <span className="font-mono text-slate-500">{job.job_id}</span></span>
        <span className="flex items-center gap-1"><span className="text-slate-400">Result:</span> <ResultSummary result={job.result} /></span>
      </div>
      {job.status === 'FAILED' && job.error && (
        <p className="text-xs text-red-700 mt-2 flex items-start gap-1.5">
          <AlertTriangle size={12} className="mt-0.5 flex-shrink-0" /> {job.error}
        </p>
      )}
    </div>
  )
}

export default function DataJobs() {
  const [jobs, setJobs] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const data = await listDataJobs()
      const items = data?.data_jobs || []
      setJobs(items)
      setError('')
      return items
    } catch (err) {
      setError(err.message || 'failed to load data jobs')
      return null
    } finally {
      setLoading(false)
    }
  }, [])

  // Self-scheduling poll: 5s while any job is QUEUED/RUNNING, else 30s. Scheduling
  // off the fetched items (not a jobs-dep effect) avoids a re-render hot loop.
  useEffect(() => {
    let cancelled = false
    let timer = null
    const tick = async () => {
      const items = await load()
      if (cancelled) return
      const active = Array.isArray(items) && items.some(j => !TERMINAL.has(j.status))
      timer = setTimeout(tick, active ? 5000 : 30_000)
    }
    tick()
    return () => { cancelled = true; if (timer) clearTimeout(timer) }
  }, [load])

  const running = jobs.filter(j => !TERMINAL.has(j.status)).length

  return (
    <div className="p-6 space-y-5 page-container">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-bold text-slate-900 tracking-tight">Data Jobs</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            Async S3-Vectors ingestion jobs from DocuSearch + Structured Analytics. Newest first.
            {running ? <span className="ml-1 font-semibold text-amber-700">{running} in progress</span> : null}
          </p>
        </div>
        <button
          type="button"
          onClick={load}
          className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-600 hover:bg-slate-50"
        >
          <RefreshCw size={13} className={loading ? 'animate-spin' : ''} /> Refresh
        </button>
      </div>

      {error && (
        <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700 flex items-center gap-2">
          <AlertTriangle size={13} /> {error}
        </div>
      )}

      {jobs.length === 0 && !loading && !error ? (
        <div className="rounded-xl border border-dashed border-slate-200 bg-white px-4 py-10 text-center">
          <Boxes size={22} className="text-slate-300 mx-auto mb-2" />
          <p className="text-sm font-medium text-slate-700">No ingestion jobs yet</p>
          <p className="text-xs text-slate-500 mt-1">
            Submit one from the Data Pipeline page with a DocuSearch or Structured Analytics group.
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {jobs.map(job => <JobRow key={job.job_id} job={job} />)}
        </div>
      )}
    </div>
  )
}
