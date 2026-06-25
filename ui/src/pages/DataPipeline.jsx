import { useCallback, useEffect, useRef, useState } from 'react'
import {
  CheckCircle, Clock, RefreshCw, Database, FileText, Server, Loader2, Activity,
  Upload, AlertTriangle,
} from 'lucide-react'
import { presignUpload, uploadToPresignedUrl, listScanRuns, getUploadStatus } from '../hooks/useApi'
import { formatDistanceToNow } from 'date-fns'

// ── Static source / KB reference (kept from previous build) ─────────────────

const SOURCES = [
  { id: 'sharepoint', name: 'SharePoint',  icon: FileText, description: 'Policy documents, standards, procedures',          s3Prefix: 's3://dev-st21arbiter-poc-raw/sharepoint/',  docCount: 12, formats: ['DOCX', 'PDF', 'MD'], color: { bg: '#eef2ff', icon: '#4f46e5', border: '#c7d2fe' } },
  { id: 'zscaler',    name: 'Zscaler ZIA', icon: Server,   description: 'URL categorization rules, policy enforcement',       s3Prefix: 's3://dev-st21arbiter-poc-raw/zscaler/',     docCount: 3,  formats: ['JSON'],            color: { bg: '#f0f9ff', icon: '#0284c7', border: '#bae6fd' } },
  { id: 'awsconfig',  name: 'AWS Config',  icon: Database, description: 'Security groups, S3 bucket configs, IAM snapshots', s3Prefix: 's3://dev-st21arbiter-poc-raw/aws-config/',  docCount: 8,  formats: ['JSON'],            color: { bg: '#fff7ed', icon: '#ea580c', border: '#fed7aa' } },
]

// ── 4-step pipeline status for a single upload ──────────────────────────────
// Each upload moves through:
//   uploading → uploaded (file PUT to Raw bucket)
//   processed                       (processing_pipeline moved Raw → Processed)
//   kb_ingesting / kb_done           (Bedrock KB ingestion job complete)
//   scanning / scan_done             (scanner_lambda finished, scan-runs COMPLETED)
// We don't have a per-step API today, so we infer from the scan-runs row's
// presence + status. If the scan-runs row exists, we collapse everything up to
// scanning into "complete". If still missing, we show steps 1-3 as in-progress.

// Unstructured docs (pdf/docx/txt/md/json) flow raw → processed → KB → scan.
const STEP_DEFS = [
  { key: 'raw',       label: 'Raw',       desc: 'File landed in raw S3 bucket'                       },
  { key: 'processed', label: 'Processed', desc: 'processing_pipeline moved Raw → Processed'           },
  { key: 'kb',        label: 'KB ingest', desc: 'Bedrock KB ingestion job complete'                  },
  { key: 'scan',      label: 'Scan',      desc: 'scanner_lambda finished; conflicts re-evaluated'    },
]

// Structured exports (.csv) take the Glue/Athena path — NOT the KB. They land in
// processed/structured/<dataset>/ and trigger a Glue crawler; the re-scan is run
// separately ("Run AI Scan"), so there's no KB or scan step here.
const STEP_DEFS_STRUCTURED = [
  { key: 'raw',       label: 'Raw',       desc: 'File landed in raw S3 bucket'                       },
  { key: 'processed', label: 'Processed', desc: 'Moved Raw → processed/structured/'                  },
  { key: 'catalog',   label: 'Catalog',   desc: 'Glue crawler refresh started — Athena-queryable'    },
]

function isStructuredUpload(u) {
  return (u.filename || '').toLowerCase().endsWith('.csv')
}

function stepDefsFor(u) {
  return isStructuredUpload(u) ? STEP_DEFS_STRUCTURED : STEP_DEFS
}

const STATUS_STYLE = {
  pending:  { bg: '#f1f5f9', border: '#e2e8f0', text: '#64748b' },
  running:  { bg: '#fffbeb', border: '#fde68a', text: '#b45309' },
  done:     { bg: '#ecfdf5', border: '#a7f3d0', text: '#047857' },
  failed:   { bg: '#fef2f2', border: '#fecaca', text: '#b91c1c' },
}

function StepChip({ status, label }) {
  const s = STATUS_STYLE[status] || STATUS_STYLE.pending
  const Icon = status === 'done' ? CheckCircle : status === 'failed' ? AlertTriangle : status === 'running' ? Loader2 : Clock
  return (
    <span className="flex items-center gap-1 text-[10px] font-medium px-2 py-1 rounded-md border whitespace-nowrap"
          style={{ background: s.bg, borderColor: s.border, color: s.text }}>
      <Icon size={11} className={status === 'running' ? 'animate-spin' : ''} />
      {label}
    </span>
  )
}

// Map an upload's progress to the 4 step statuses based on what we know.
function stepStatesFor(upload) {
  // Structured (.csv): raw → processed → catalog. The processing_pipeline copies
  // to processed/structured/ and starts the Glue crawler on the S3 ObjectCreated
  // event. Poll /uploads/status so we do not mark cataloging complete before
  // the staged object exists and the crawler has reported a result.
  if (isStructuredUpload(upload)) {
    const s = { raw: 'pending', processed: 'pending', catalog: 'pending' }
    if (upload.state === 'uploading')     { s.raw = 'running'; return s }
    if (upload.state === 'upload_failed') { s.raw = 'failed';  return s }
    s.raw = 'done'
    const status = upload.processingStatus
    if (!status) {
      s.processed = 'running'
      return s
    }
    if (status.processed?.exists || status.structured?.exists) {
      s.processed = 'done'
    } else {
      s.processed = 'running'
      return s
    }
    if (status.status === 'catalog_done') s.catalog = 'done'
    else if (status.status === 'catalog_failed') s.catalog = 'failed'
    else s.catalog = 'running'
    return s
  }

  // raw: done as soon as the browser PUT succeeded (state="uploaded" onward)
  // processed/kb/scan: we infer from scan-runs:
  //   - no scan-run yet → both raw + processed running (processing_pipeline cold start)
  //   - scan-run RUNNING → raw + processed done, kb running, scan running
  //   - scan-run COMPLETED → all done
  //   - scan-run FAILED → raw + processed done, kb/scan failed
  const states = { raw: 'pending', processed: 'pending', kb: 'pending', scan: 'pending' }
  if (upload.state === 'uploading')      { states.raw = 'running'; return states }
  if (upload.state === 'upload_failed')  { states.raw = 'failed';  return states }

  // PUT succeeded; raw is done.
  states.raw = 'done'

  const run = upload.scanRun
  if (!run) {
    // No scan-run yet — assume processing_pipeline + KB ingestion are in flight.
    states.processed = 'running'
    states.kb = 'running'
    return states
  }
  // Once a scan-run exists, the chain has progressed past processing + KB.
  states.processed = 'done'
  states.kb = 'done'
  if (run.status === 'COMPLETED') { states.scan = 'done' }
  else if (run.status === 'FAILED') { states.scan = 'failed' }
  else { states.scan = 'running' }
  return states
}

function shortKey(key) {
  if (!key) return ''
  // Drop the users/<sub>/ prefix the presign endpoint adds.
  return key.replace(/^users\/[^/]+\//, '')
}

// ── Upload dropzone ─────────────────────────────────────────────────────────

function UploadDropzone({ onFile, disabled }) {
  const [dragging, setDragging] = useState(false)
  const inputRef = useRef(null)

  function handleFiles(files) {
    if (!files || !files.length) return
    Array.from(files).forEach(f => onFile(f))
  }

  return (
    <div
      onDragOver={e => { e.preventDefault(); setDragging(true) }}
      onDragLeave={() => setDragging(false)}
      onDrop={e => {
        e.preventDefault(); setDragging(false)
        if (!disabled) handleFiles(e.dataTransfer.files)
      }}
      onClick={() => !disabled && inputRef.current?.click()}
      className={`rounded-xl border-2 border-dashed p-8 flex flex-col items-center gap-2 transition-colors cursor-pointer ${
        disabled ? 'opacity-50 cursor-not-allowed' : ''
      }`}
      style={{
        borderColor: dragging ? '#6366f1' : '#cbd5e1',
        background: dragging ? '#eef2ff' : '#ffffff',
      }}
    >
      <div className="w-12 h-12 rounded-full bg-indigo-50 border border-indigo-200 flex items-center justify-center">
        <Upload size={20} className="text-indigo-600" />
      </div>
      <p className="text-sm font-semibold text-slate-900">Drop policy documents here, or click to browse</p>
      <p className="text-xs text-slate-500">Policy docs (.md, .pdf, .docx, .json, .txt) → Knowledge Base · structured exports (.csv) → Glue/Athena</p>
      <input
        ref={inputRef}
        type="file"
        multiple
        accept=".md,.pdf,.docx,.json,.txt,.csv"
        className="hidden"
        onChange={e => handleFiles(e.target.files)}
      />
    </div>
  )
}

// ── One row per upload ──────────────────────────────────────────────────────

function UploadRow({ upload }) {
  const stepDefs = stepDefsFor(upload)
  const states = stepStatesFor(upload)
  const structured = isStructuredUpload(upload)
  const finished = stepDefs.every(d => states[d.key] === 'done') || stepDefs.some(d => states[d.key] === 'failed')
  const ts = upload.startedAt ? new Date(upload.startedAt) : null
  const statusMessage = structured ? upload.processingStatus?.message : null
  const structuredKey = structured ? upload.processingStatus?.structured?.key : null

  return (
    <div className="rounded-xl p-4 bg-white border border-slate-200"
         style={{ boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }}>
      <div className="flex items-center justify-between mb-3 gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <FileText size={14} className="text-slate-500 flex-shrink-0" />
          <div className="min-w-0">
            <p className="text-sm font-medium text-slate-900 truncate" title={upload.filename}>{upload.filename}</p>
            <p className="text-[11px] text-slate-500 mt-0.5">
              {ts ? formatDistanceToNow(ts, { addSuffix: true }) : ''}
              {upload.key && <> · <span className="font-mono">{shortKey(upload.key)}</span></>}
            </p>
          </div>
        </div>
        {finished && structured && states.catalog === 'done' && (
          <p className="text-xs text-emerald-700 flex-shrink-0">Cataloged · ready for grouping/query</p>
        )}
        {finished && structured && states.catalog === 'failed' && (
          <p className="text-xs text-red-700 flex-shrink-0">Catalog failed</p>
        )}
        {!finished && structured && statusMessage && (
          <p className="text-xs text-amber-700 flex-shrink-0">{statusMessage}</p>
        )}
        {finished && !structured && upload.scanRun?.totals && (
          <p className="text-xs text-emerald-700 flex-shrink-0">
            {upload.scanRun.totals.conflicts ?? 0} conflicts · {upload.scanRun.totals.compliant ?? 0} compliant
          </p>
        )}
        {!finished && (
          <p className="text-xs text-amber-700 flex-shrink-0 flex items-center gap-1">
            <Loader2 size={11} className="animate-spin" /> in progress
          </p>
        )}
      </div>
      {/* Steps */}
      <div className="flex items-center gap-2 flex-wrap">
        {stepDefs.map((step, i) => (
          <div key={step.key} className="flex items-center gap-2">
            <StepChip status={states[step.key]} label={step.label} />
            {i < stepDefs.length - 1 && (
              <span className="text-slate-300 text-xs select-none">→</span>
            )}
          </div>
        ))}
      </div>
      {upload.error && (
        <p className="text-xs text-red-700 mt-2 flex items-center gap-1.5">
          <AlertTriangle size={12} /> {upload.error}
        </p>
      )}
      {structuredKey && (
        <p className="text-[11px] text-slate-500 mt-2">
          Structured path: <span className="font-mono">{structuredKey}</span>
        </p>
      )}
    </div>
  )
}

// ── Source card (existing — kept as informational reference) ────────────────

function SourceCard({ source }) {
  const Icon = source.icon
  return (
    <div className="rounded-xl p-4 bg-white"
         style={{ border: `1px solid ${source.color.border}`, boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }}>
      <div className="flex items-start justify-between mb-4">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-lg flex items-center justify-center flex-shrink-0"
               style={{ background: source.color.bg }}>
            <Icon size={16} style={{ color: source.color.icon }} />
          </div>
          <div>
            <p className="font-semibold text-slate-900 text-sm">{source.name}</p>
            <p className="text-xs text-slate-500">{source.description}</p>
          </div>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-3 text-xs">
        <div>
          <p className="text-slate-400 mb-0.5">S3 Prefix</p>
          <p className="font-mono text-slate-600 truncate text-[11px]" title={source.s3Prefix}>{source.s3Prefix}</p>
        </div>
        <div>
          <p className="text-slate-400 mb-0.5">Documents</p>
          <p className="text-slate-700">{source.docCount} files ({source.formats.join(', ')})</p>
        </div>
        <div className="col-span-2">
          <p className="text-slate-400 mb-0.5">Status</p>
          <span className="flex items-center gap-1 text-emerald-700">
            <CheckCircle size={11} /> SYNCED · auto-detect ENABLED
          </span>
        </div>
      </div>
    </div>
  )
}

// ── Two-path flow explainer (static — shown beneath the dropzone) ───────────
// Drives off the same STEP_DEFS / STEP_DEFS_STRUCTURED used by UploadRow, so the
// explainer can't drift from how an upload actually progresses. Purely cosmetic:
// neutral chips, no live status (per-upload status lives in UploadRow below).

const PATH_DEFS = [
  {
    id: 'policy',
    title: 'Policy Documents',
    subtitle: '.pdf · .docx · .txt · .md · .json → Knowledge Base',
    steps: STEP_DEFS,
    Icon: FileText,
    accent: { bg: '#eef2ff', icon: '#4f46e5', border: '#c7d2fe' },
  },
  {
    id: 'structured',
    title: 'Structured Exports',
    subtitle: '.csv → Glue / Athena catalog',
    steps: STEP_DEFS_STRUCTURED,
    Icon: Database,
    accent: { bg: '#fff7ed', icon: '#ea580c', border: '#fed7aa' },
  },
]

const STEP_ICON = { raw: Upload, processed: Server, kb: Database, catalog: Database, scan: CheckCircle }

function PathCard({ path }) {
  const HeaderIcon = path.Icon
  return (
    <div className="rounded-xl p-4 bg-white"
         style={{ border: `1px solid ${path.accent.border}`, boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }}>
      <div className="flex items-center gap-3 mb-4">
        <div className="w-9 h-9 rounded-lg flex items-center justify-center flex-shrink-0"
             style={{ background: path.accent.bg }}>
          <HeaderIcon size={16} style={{ color: path.accent.icon }} />
        </div>
        <div className="min-w-0">
          <p className="font-semibold text-slate-900 text-sm">{path.title}</p>
          <p className="text-xs text-slate-500 truncate" title={path.subtitle}>{path.subtitle}</p>
        </div>
      </div>
      <div>
        {path.steps.map((step, i) => {
          const Icon = STEP_ICON[step.key] || Clock
          return (
            <div key={step.key}>
              <div className="flex items-start gap-2.5">
                <span className="flex items-center gap-1.5 text-[11px] font-medium px-2 py-1 rounded-md border whitespace-nowrap"
                      style={{ background: STATUS_STYLE.pending.bg, borderColor: STATUS_STYLE.pending.border, color: '#334155' }}>
                  <Icon size={12} style={{ color: path.accent.icon }} />
                  {step.label}
                </span>
                <p className="text-[10px] text-slate-400 leading-snug pt-1.5">{step.desc}</p>
              </div>
              {i < path.steps.length - 1 && (
                <div className="text-slate-300 text-sm leading-none pl-3 py-0.5 select-none">↓</div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function PipelinePaths() {
  return (
    <div>
      <p className="text-[10px] text-slate-500 font-semibold uppercase tracking-wider mb-3">Processing Paths</p>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {PATH_DEFS.map(p => <PathCard key={p.id} path={p} />)}
      </div>
    </div>
  )
}

// ── Page ─────────────────────────────────────────────────────────────────────

export default function DataPipeline() {
  const [uploads, setUploads] = useState([])     // newest first

  // Per-upload state machine. We update via a single setState that maps over
  // the existing array, so concurrent polls + new uploads don't race each other.
  const updateUpload = useCallback((id, patch) => {
    setUploads(prev => prev.map(u => u.id === id ? { ...u, ...patch } : u))
  }, [])

  async function handleFile(file) {
    const id = `upl-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`
    const upload = {
      id,
      filename: file.name,
      contentType: file.type || 'application/octet-stream',
      state: 'uploading',
      startedAt: new Date().toISOString(),
      key: null,
      scanRun: null,
      processingStatus: null,
      error: null,
    }
    setUploads(prev => [upload, ...prev])

    // 1. presign
    let pre
    try {
      pre = await presignUpload({ filename: file.name, contentType: file.type })
    } catch (err) {
      updateUpload(id, { state: 'upload_failed', error: 'presign failed: ' + err.message })
      return
    }
    updateUpload(id, { key: pre.key, bucket: pre.bucket })

    // 2. PUT directly to S3
    try {
      const res = await uploadToPresignedUrl(pre.url, pre.headers, file)
      if (!res.ok) {
        updateUpload(id, { state: 'upload_failed', error: `S3 PUT returned ${res.status}` })
        return
      }
    } catch (err) {
      updateUpload(id, { state: 'upload_failed', error: 'S3 PUT failed: ' + err.message })
      return
    }
    updateUpload(id, { state: 'uploaded' })
    // Polling effect will pick this up and update scanRun as the chain progresses.
  }

  // Poll /scan-runs every 5s while any upload is still in-flight. We stop once
  // every upload has either finished or failed, then resume when a new upload
  // appears.
  useEffect(() => {
    const active = uploads.some(u => {
      if (u.state === 'upload_failed') return false
      if (isStructuredUpload(u)) {
        return !['catalog_done', 'catalog_failed'].includes(u.processingStatus?.status)
      }
      return !u.scanRun || (u.scanRun.status !== 'COMPLETED' && u.scanRun.status !== 'FAILED')
    })
    if (!active) return
    let cancelled = false
    const tick = async () => {
      try {
        const data = await listScanRuns(20)
        const runs = data?.scan_runs || []
        if (cancelled) return
        const structuredUploads = uploads.filter(u =>
          u.key &&
          isStructuredUpload(u) &&
          !['catalog_done', 'catalog_failed'].includes(u.processingStatus?.status)
        )
        const statusById = {}
        await Promise.all(structuredUploads.map(async u => {
          try {
            statusById[u.id] = await getUploadStatus(u.key)
          } catch (err) {
            statusById[u.id] = { status: 'catalog_failed', message: err.message || 'status check failed' }
          }
        }))
        // For each upload that has a key but no terminal scanRun, see if a row
        // matching its triggered_by has appeared.
        setUploads(prev => prev.map(u => {
          if (statusById[u.id]) return { ...u, processingStatus: statusById[u.id] }
          if (!u.key) return u
          if (u.scanRun?.status === 'COMPLETED' || u.scanRun?.status === 'FAILED') return u
          const wanted = `auto-ingest:${u.key}`
          const match = runs.find(r => r.triggered_by === wanted)
          if (!match) return u
          return { ...u, scanRun: match }
        }))
      } catch {
        /* silent — keep polling */
      }
    }
    tick()
    const handle = setInterval(tick, 5000)
    return () => { cancelled = true; clearInterval(handle) }
  }, [uploads])

  return (
    <div className="p-6 space-y-6 page-container">
      <div>
        <h1 className="text-lg font-bold text-slate-900 tracking-tight">Data Pipeline</h1>
        <p className="text-xs text-slate-500 mt-0.5">
          Policy docs (.pdf/.docx/.txt/.md/.json) → KB ingestion → scan. Structured exports (.csv) → Glue catalog (then Run AI Scan). Processed in ~30-60s.
        </p>
      </div>

      {/* Upload zone */}
      <UploadDropzone onFile={handleFile} />

      {/* Two-path flow explainer */}
      <PipelinePaths />

      {/* Recent uploads */}
      {uploads.length > 0 && (
        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <Activity size={12} className="text-slate-500" />
            <p className="text-[10px] text-slate-500 font-semibold uppercase tracking-wider">Recent Uploads (this session)</p>
          </div>
          <div className="space-y-2">
            {uploads.map(u => <UploadRow key={u.id} upload={u} />)}
          </div>
        </div>
      )}

      {/* Source connectors — informational */}
      <div>
        <p className="text-[10px] text-slate-500 font-semibold uppercase tracking-wider mb-3">Data Sources (S3-backed · auto-detect enabled)</p>
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          {SOURCES.map(src => (
            <SourceCard key={src.id} source={src} />
          ))}
        </div>
      </div>

      {/* KB info */}
      <div className="rounded-xl p-4 bg-white border border-slate-200"
           style={{ boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }}>
        <p className="text-[10px] text-slate-500 font-semibold uppercase tracking-wider mb-3">Bedrock Knowledge Base</p>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 text-xs">
          {[
            { label: 'KB ID',            value: import.meta.env.VITE_KB_ID || '2ADHACW6LB',                                            mono: true },
            { label: 'Embedding Model',  value: 'Titan Embed Text v2',                                                                  mono: false },
            { label: 'Chunk Size',       value: '512 tokens (20% overlap)',                                                             mono: false },
            { label: 'Vector Store',     value: 'OpenSearch Serverless',                                                                mono: false },
          ].map(item => (
            <div key={item.label}
                 className="rounded-lg p-3 bg-slate-50 border border-slate-200">
              <p className="text-slate-400 mb-1">{item.label}</p>
              <p className={`text-slate-700 ${item.mono ? 'font-mono text-[11px]' : ''}`}>{item.value}</p>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
