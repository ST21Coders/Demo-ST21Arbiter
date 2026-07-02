import { useCallback, useEffect, useRef, useState } from 'react'
import {
  CheckCircle, Clock, RefreshCw, Database, FileText, Server, Loader2, Activity,
  Upload, AlertTriangle,
} from 'lucide-react'
import {
  getUploadStatus,
  listDataGroupingProjects,
  listScanRuns,
  materializeDataGroupingProject,
  presignUpload,
  uploadToPresignedUrl,
} from '../hooks/useApi'
import { formatDistanceToNow } from 'date-fns'

// ── Static source / KB reference (kept from previous build) ─────────────────

const SOURCES = [
  { id: 'sharepoint', name: 'SharePoint',  icon: FileText, description: 'Policy documents, standards, procedures',          s3Prefix: 's3://dev-st21arbiter-poc-raw/sharepoint/',  docCount: 12, formats: ['DOCX', 'PDF', 'MD'], color: { bg: '#eef2ff', icon: '#4f46e5', border: '#c7d2fe' } },
  { id: 'zscaler',    name: 'Zscaler ZIA', icon: Server,   description: 'URL categorization rules, policy enforcement',       s3Prefix: 's3://dev-st21arbiter-poc-raw/zscaler/',     docCount: 3,  formats: ['JSON'],            color: { bg: '#f0f9ff', icon: '#0284c7', border: '#bae6fd' } },
  { id: 'awsconfig',  name: 'AWS Config',  icon: Database, description: 'Security groups, S3 bucket configs, IAM snapshots', s3Prefix: 's3://dev-st21arbiter-poc-raw/aws-config/',  docCount: 8,  formats: ['JSON'],            color: { bg: '#fff7ed', icon: '#ea580c', border: '#fed7aa' } },
]

const GROUPS_STORAGE_KEY = 'arbiter.dataGrouping.v2.savedGroups'
const PIPELINE_PROJECT_NAME = 'Vendor Audit June 2026'
const PIPELINE_PROJECT_ID = 'vendor-audit-june-2026'

function slugify(value) {
  return String(value || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '') || 'group'
}

function groupNameFromInput(value) {
  return String(value || '').trim().replace(/\s+/g, '_')
}

function fileKey(file) {
  return file?.key || file?.sourceKey || file?.source_key || file?.s3_key || file?.path || file?.name || ''
}

function readLocalGroups() {
  if (typeof window === 'undefined') return []
  try {
    const saved = JSON.parse(localStorage.getItem(GROUPS_STORAGE_KEY) || '[]')
    return Array.isArray(saved) ? saved.filter(group => group?.name) : []
  } catch {
    return []
  }
}

function persistLocalGroups(groups) {
  localStorage.setItem(GROUPS_STORAGE_KEY, JSON.stringify(groups))
}

function starterPromptsForGroup(groupName) {
  return [
    `For this ${groupName} group, rank stores from highest to lowest total sales. Include branch city, branch state, total revenue, units sold, transaction count, top category, and a short explanation.`,
    `For this ${groupName} group, rank product categories by revenue and units sold. Include part category, total revenue, units sold, average unit price, and the leading branch if available.`,
    `For this ${groupName} group, compare sales channels by revenue, units sold, transaction count, and average line revenue. Include a short explanation of channel mix.`,
    `For this ${groupName} group, analyze gross margin using Unit_Cost and Unit_Price. Rank stores or products by estimated margin dollars and margin percent.`,
  ]
}

function localGroupProfile(groupName, files) {
  const text = `${groupName} ${(files || []).map(file => file.name || file.key || '').join(' ')}`.toLowerCase()
  if (text.includes('electronics') || text.includes('sales') || text.includes('line_revenue')) {
    return {
      kind: 'sales',
      confidence: 'medium',
      starterPrompts: starterPromptsForGroup(groupName),
    }
  }
  return null
}

function upsertLocalGroupFile(groupTarget, fileInfo) {
  if (!groupTarget?.name || !fileInfo?.key) return
  const current = readLocalGroups()
  const existing = current.find(group => group.id === groupTarget.id || group.name === groupTarget.name)
  const nextGroup = {
    ...(existing || {}),
    id: existing?.id || groupTarget.id || `${slugify(groupTarget.name)}-${Date.now()}`,
    projectId: PIPELINE_PROJECT_ID,
    projectName: PIPELINE_PROJECT_NAME,
    name: groupTarget.name,
    type: existing?.type || 'pipeline_upload',
    updatedAt: new Date().toISOString(),
  }
  const existingFiles = Array.isArray(existing?.files) ? existing.files : []
  const byKey = new Map(existingFiles.map(file => [fileKey(file), file]))
  byKey.set(fileInfo.key, {
    ...byKey.get(fileInfo.key),
    ...fileInfo,
    addedAt: byKey.get(fileInfo.key)?.addedAt || new Date().toISOString(),
  })
  nextGroup.files = [...byKey.values()]
  nextGroup.fileKeys = nextGroup.files.map(file => fileKey(file)).filter(Boolean)
  nextGroup.groupProfile = existing?.groupProfile || localGroupProfile(nextGroup.name, nextGroup.files)
  const others = current.filter(group => group.id !== nextGroup.id && group.name !== nextGroup.name)
  persistLocalGroups([...others, nextGroup])
}

function dataGroupOptionsFromLocal() {
  return readLocalGroups().map(group => ({
    id: `local::${group.id || group.name}`,
    localId: group.id,
    groupName: group.name,
    label: `${group.projectName || 'Local Data Grouping'} / ${group.name}`,
    fileCount: Array.isArray(group.files) ? group.files.length : Array.isArray(group.fileKeys) ? group.fileKeys.length : 0,
    source: 'local',
  }))
}

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

function UploadDropzone({
  onFile,
  disabled,
  creatingNewGroup,
  setCreatingNewGroup,
  newGroupName,
  setNewGroupName,
  selectedGroupId,
  setSelectedGroupId,
  groupOptions,
}) {
  const [dragging, setDragging] = useState(false)
  const inputRef = useRef(null)
  const canBrowse = !disabled

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
      className={`rounded-xl border-2 border-dashed p-4 transition-colors ${
        disabled ? 'opacity-50 cursor-not-allowed' : ''
      }`}
      style={{
        borderColor: dragging ? '#6366f1' : '#cbd5e1',
        background: dragging ? '#eef2ff' : '#ffffff',
      }}
    >
      <div className="flex flex-col lg:flex-row lg:items-center gap-3">
        <label className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-700">
          <input
            type="checkbox"
            checked={creatingNewGroup}
            onChange={event => setCreatingNewGroup(event.target.checked)}
            className="h-4 w-4 rounded border-slate-300 text-indigo-600"
          />
          New Group
        </label>
        {creatingNewGroup ? (
          <input
            type="text"
            value={newGroupName}
            onChange={event => setNewGroupName(event.target.value)}
            placeholder="Group name"
            className="min-w-0 flex-1 rounded-lg border border-slate-200 px-3 py-2 text-sm outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
          />
        ) : (
          <select
            value={selectedGroupId}
            onChange={event => setSelectedGroupId(event.target.value)}
            className="min-w-0 flex-1 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
          >
            <option value="">{groupOptions.length ? 'Select existing group' : 'No existing groups found'}</option>
            {groupOptions.map(group => (
              <option key={group.id} value={group.id}>
                {group.groupName} ({group.fileCount || 0})
              </option>
            ))}
          </select>
        )}
        <button
          type="button"
          disabled={!canBrowse}
          onClick={() => canBrowse && inputRef.current?.click()}
          className="inline-flex items-center justify-center gap-2 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:bg-slate-300"
        >
          <Upload size={15} />
          Click to browse
        </button>
      </div>
      <div className="mt-4 flex flex-col items-center gap-2 rounded-lg border border-slate-100 bg-slate-50 px-4 py-6 text-center">
        <div className="w-12 h-12 rounded-full bg-indigo-50 border border-indigo-200 flex items-center justify-center">
          <Upload size={20} className="text-indigo-600" />
        </div>
        <p className="text-sm font-semibold text-slate-900">
          {disabled ? 'Choose or create a group before selecting files' : 'Drop files here to add them to the selected group'}
        </p>
        <p className="text-xs text-slate-500">Policy docs (.md, .pdf, .docx, .json, .txt) → Knowledge Base · structured exports (.csv) → Glue/Athena</p>
      </div>
      <input
        ref={inputRef}
        type="file"
        multiple
        disabled={disabled}
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
              {upload.groupName && <> · Group <span className="font-semibold text-slate-600">{upload.groupName}</span></>}
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
      {upload.groupError && (
        <p className="text-xs text-red-700 mt-2 flex items-center gap-1.5">
          <AlertTriangle size={12} /> group setup failed: {upload.groupError}
        </p>
      )}
      {structuredKey && (
        <p className="text-[11px] text-slate-500 mt-2">
          Structured path: <span className="font-mono">{structuredKey}</span>
        </p>
      )}
      {upload.groupMaterialized && (
        <p className="text-[11px] text-emerald-700 mt-2">
          Added to Data Group · group profile and Athena materialization queued
          {upload.structuredFactSources ? ` · ${upload.structuredFactSources} text fact source${upload.structuredFactSources === 1 ? '' : 's'} indexed` : ''}
          {upload.kbSyncMessage ? ` · KB sync ${upload.kbSyncMessage}` : ''}
        </p>
      )}
      {upload.groupMaterializing && (
        <p className="text-[11px] text-amber-700 mt-2">
          Adding to Data Group...
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
  const [groupOptions, setGroupOptions] = useState([])
  const [groupsLoading, setGroupsLoading] = useState(false)
  const [creatingNewGroup, setCreatingNewGroup] = useState(false)
  const [selectedGroupId, setSelectedGroupId] = useState('')
  const [newGroupName, setNewGroupName] = useState('')
  const [groupPublishStatus, setGroupPublishStatus] = useState({})
  const groupPublishInFlightRef = useRef(new Set())

  // Per-upload state machine. We update via a single setState that maps over
  // the existing array, so concurrent polls + new uploads don't race each other.
  const updateUpload = useCallback((id, patch) => {
    setUploads(prev => prev.map(u => u.id === id ? { ...u, ...patch } : u))
  }, [])

  const refreshGroups = useCallback(async () => {
    const local = dataGroupOptionsFromLocal()
    setGroupOptions(local)
    setGroupsLoading(true)
    try {
      const data = await listDataGroupingProjects()
      const remote = (data.groups || []).map(group => ({
        ...group,
        id: group.id || `${group.projectId || PIPELINE_PROJECT_ID}::${group.groupName}`,
        source: 'remote',
      }))
      const byName = new Map()
      ;[...local, ...remote].forEach(group => {
        if (!group?.groupName) return
        byName.set(group.groupName, group)
      })
      setGroupOptions([...byName.values()].sort((a, b) => String(a.groupName).localeCompare(String(b.groupName))))
    } catch {
      setGroupOptions(local)
    } finally {
      setGroupsLoading(false)
    }
  }, [])

  useEffect(() => {
    refreshGroups()
  }, [refreshGroups])

  function currentGroupTarget() {
    if (creatingNewGroup) {
      const name = groupNameFromInput(newGroupName)
      if (!name) return null
      return {
        id: `${slugify(name)}-${Date.now()}`,
        name,
        source: 'new',
        files: [],
      }
    }
    const selected = groupOptions.find(group => group.id === selectedGroupId)
    if (!selected?.groupName) return null
    return {
      id: selected.localId || selected.id,
      name: selected.groupName,
      source: selected.source || 'existing',
      files: selected.files || [],
    }
  }

  async function handleFile(file) {
    const groupTarget = currentGroupTarget()
    if (!groupTarget) return
    const id = `upl-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`
    const upload = {
      id,
      filename: file.name,
      contentType: file.type || 'application/octet-stream',
      size: file.size,
      state: 'uploading',
      startedAt: new Date().toISOString(),
      key: null,
      scanRun: null,
      processingStatus: null,
      groupId: groupTarget.id,
      groupName: groupTarget.name,
      groupMaterializing: false,
      groupMaterialized: false,
      groupError: null,
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
        try {
          const status = await getUploadStatus(pre.key)
          if (status?.raw?.exists || status?.processed?.exists || status?.structured?.exists) {
            updateUpload(id, { processingStatus: status })
          } else {
            updateUpload(id, { state: 'upload_failed', error: `S3 PUT returned ${res.status}` })
            return
          }
        } catch {
          const detail = /Invalid key=value pair/i.test(res.detail || '')
            ? 'S3 rejected an unexpected Authorization header on the upload retry.'
            : `S3 PUT returned ${res.status}`
          updateUpload(id, { state: 'upload_failed', error: detail })
          return
        }
      }
    } catch (err) {
      updateUpload(id, { state: 'upload_failed', error: 'S3 PUT failed: ' + err.message })
      return
    }
    updateUpload(id, { state: 'uploaded' })
    upsertLocalGroupFile(groupTarget, {
      key: pre.key,
      name: file.name,
      size: file.size,
      last_modified: new Date().toISOString(),
    })
    refreshGroups()
    // Polling effect will pick this up and update scanRun as the chain progresses.
  }

  function uploadReadyForGroupPublish(upload) {
    return Boolean(
      upload?.key
      && !upload.error
      && upload.state !== 'upload_failed'
      && (upload.processingStatus?.processed?.exists || upload.processingStatus?.structured?.exists)
    )
  }

  function selectedGroupName() {
    if (creatingNewGroup) return groupNameFromInput(newGroupName)
    return groupOptions.find(group => group.id === selectedGroupId)?.groupName || ''
  }

  async function filesReadyForGroupPublish(groupName, files) {
    setGroupPublishStatus(prev => ({
      ...prev,
      [groupName]: { state: 'checking', message: `Checking ${files.length} files before publishing ${groupName}...` },
    }))
    const pending = []
    const failed = []
    for (const file of files) {
      const key = fileKey(file)
      if (!key) continue
      try {
        const status = await getUploadStatus(key)
        if (!status?.processed?.exists && !status?.structured?.exists) {
          pending.push(file.name || key)
        }
      } catch (err) {
        failed.push(`${file.name || key}: ${err.message || 'status check failed'}`)
      }
    }
    if (failed.length) {
      setGroupPublishStatus(prev => ({
        ...prev,
        [groupName]: {
          state: 'failed',
          message: `Could not verify ${failed.length} file${failed.length === 1 ? '' : 's'} before publishing.`,
        },
      }))
      return false
    }
    if (pending.length) {
      setGroupPublishStatus(prev => ({
        ...prev,
        [groupName]: {
          state: 'waiting',
          message: `${pending.length}/${files.length} file${pending.length === 1 ? '' : 's'} still moving into processed storage.`,
        },
      }))
      return false
    }
    return true
  }

  async function materializeGroup(groupName) {
    if (!groupName || groupPublishInFlightRef.current.has(groupName)) return
    const localGroups = readLocalGroups()
    const localGroup = localGroups.find(group => group.name === groupName)
    const files = (localGroup?.files || []).filter(file => fileKey(file))
    if (!localGroup || !files.length) return

    groupPublishInFlightRef.current.add(groupName)
    setUploads(prev => prev.map(upload => (
      upload.groupName === groupName
        ? { ...upload, groupMaterializing: true, groupError: null }
        : upload
    )))
    try {
      const ready = await filesReadyForGroupPublish(groupName, files)
      if (!ready) return
      setGroupPublishStatus(prev => ({
        ...prev,
        [groupName]: { state: 'publishing', message: `Publishing ${files.length} files into ${groupName}...` },
      }))
      const result = await materializeDataGroupingProject({
        projectName: PIPELINE_PROJECT_NAME,
        projectId: PIPELINE_PROJECT_ID,
        groups: [{
          id: localGroup.id || slugify(groupName),
          name: groupName,
          type: localGroup?.type || 'pipeline_upload',
          groupProfile: localGroup?.groupProfile || localGroupProfile(groupName, files),
          files,
        }],
        move: false,
      })
      const materializedGroup = (result?.metadata?.groups || []).find(group => group?.name === groupName)
      const factSources = materializedGroup?.structuredFacts?.counts?.factSources || 0
      setGroupPublishStatus(prev => ({
        ...prev,
        [groupName]: {
          state: 'published',
          message: `${groupName} published with ${files.length} files${factSources ? ` and ${factSources} text fact source${factSources === 1 ? '' : 's'}` : ''}.`,
          kbSyncMessage: result?.kbSync?.message || '',
          structuredFactSources: factSources,
        },
      }))
      setUploads(prev => prev.map(upload => (
        upload.groupName === groupName
          ? {
              ...upload,
              groupMaterializing: false,
              groupMaterialized: true,
              kbSyncMessage: result?.kbSync?.message || '',
              structuredFactSources: factSources,
            }
          : upload
      )))
      refreshGroups()
    } catch (err) {
      const message = err.message || `unable to publish ${groupName}`
      setGroupPublishStatus(prev => ({
        ...prev,
        [groupName]: { state: 'failed', message },
      }))
      setUploads(prev => prev.map(upload => (
        upload.groupName === groupName
          ? { ...upload, groupMaterializing: false, groupError: message }
          : upload
      )))
    } finally {
      groupPublishInFlightRef.current.delete(groupName)
      setUploads(prev => prev.map(upload => (
        upload.groupName === groupName && upload.groupMaterializing && !upload.groupMaterialized
          ? { ...upload, groupMaterializing: false }
          : upload
      )))
    }
  }

  // Poll /scan-runs every 5s while any upload is still in-flight. We stop once
  // every upload has either finished or failed, then resume when a new upload
  // appears.
  useEffect(() => {
    const active = uploads.some(u => {
      if (u.state === 'upload_failed') return false
      if (u.key && !u.groupMaterialized && !u.groupError) return true
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
        const statusUploads = uploads.filter(u =>
          u.key &&
          (
            isStructuredUpload(u)
            || (!u.groupMaterialized && !u.groupError)
          ) &&
          !['catalog_done', 'catalog_failed'].includes(u.processingStatus?.status)
        )
        const statusById = {}
        await Promise.all(statusUploads.map(async u => {
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

  useEffect(() => {
    const groupNames = [...new Set(uploads.map(upload => upload.groupName).filter(Boolean))]
    const timers = groupNames.map(groupName => window.setTimeout(() => {
      const groupUploads = uploads.filter(upload => upload.groupName === groupName)
      if (!groupUploads.length) return
      if (groupUploads.every(upload => upload.groupMaterialized)) return
      if (groupUploads.some(upload => upload.groupMaterializing || upload.groupError)) return
      if (groupUploads.some(upload => !upload.key || upload.state === 'uploading')) return
      if (groupUploads.some(upload => !uploadReadyForGroupPublish(upload))) return
      materializeGroup(groupName)
    }, 2500))
    return () => timers.forEach(timer => window.clearTimeout(timer))
  }, [uploads])

  const groupTargetReady = creatingNewGroup ? Boolean(groupNameFromInput(newGroupName)) : Boolean(selectedGroupId)
  const currentSelectedGroupName = selectedGroupName()
  const currentPublishStatus = currentSelectedGroupName ? groupPublishStatus[currentSelectedGroupName] : null

  return (
    <div className="p-6 space-y-6 page-container">
      <div>
        <h1 className="text-lg font-bold text-slate-900 tracking-tight">Data Pipeline</h1>
        <p className="text-xs text-slate-500 mt-0.5">
          Policy docs (.pdf/.docx/.txt/.md/.json) → KB ingestion → scan. Structured exports (.csv) → Glue catalog (then Run AI Scan). Processed in ~30-60s.
        </p>
      </div>

      {/* Upload zone */}
      <UploadDropzone
        onFile={handleFile}
        disabled={!groupTargetReady}
        creatingNewGroup={creatingNewGroup}
        setCreatingNewGroup={setCreatingNewGroup}
        newGroupName={newGroupName}
        setNewGroupName={setNewGroupName}
        selectedGroupId={selectedGroupId}
        setSelectedGroupId={setSelectedGroupId}
        groupOptions={groupOptions}
      />
      <div className="rounded-xl border border-slate-200 bg-white px-4 py-3 text-xs text-slate-600">
        {groupTargetReady ? (
          <div className="flex flex-wrap items-center justify-between gap-3">
            <span>
              Uploads will be assigned to <span className="font-semibold text-slate-800">{currentSelectedGroupName}</span>. Each uploaded file is owned by that group and will be published into group metadata after processing.
            </span>
            <button
              type="button"
              onClick={() => materializeGroup(currentSelectedGroupName)}
              disabled={!currentSelectedGroupName || currentPublishStatus?.state === 'checking' || currentPublishStatus?.state === 'publishing'}
              className="inline-flex items-center gap-1.5 rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-2 text-xs font-semibold text-indigo-700 hover:bg-indigo-100 disabled:cursor-not-allowed disabled:border-slate-200 disabled:bg-slate-100 disabled:text-slate-400"
            >
              {currentPublishStatus?.state === 'checking' || currentPublishStatus?.state === 'publishing' ? (
                <Loader2 size={13} className="animate-spin" />
              ) : (
                <Upload size={13} />
              )}
              Publish selected group
            </button>
          </div>
        ) : (
          <span>
            {groupsLoading ? 'Loading existing groups...' : 'Choose an existing group or create a new group before selecting files.'}
          </span>
        )}
      </div>
      {Object.keys(groupPublishStatus).length > 0 && (
        <div className="space-y-2">
          {Object.entries(groupPublishStatus).map(([groupName, status]) => (
            <div
              key={groupName}
              className={`rounded-xl border px-4 py-3 text-xs ${
                status.state === 'failed'
                  ? 'border-red-200 bg-red-50 text-red-700'
                : status.state === 'published'
                  ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
                  : 'border-amber-200 bg-amber-50 text-amber-800'
              }`}
            >
              <div className="flex flex-wrap items-center gap-2">
                {status.state === 'publishing' || status.state === 'checking' ? (
                  <Loader2 size={13} className="animate-spin" />
                ) : status.state === 'failed' ? (
                  <AlertTriangle size={13} />
                ) : (
                  <CheckCircle size={13} />
                )}
                <span className="font-semibold">{groupName}</span>
                <span>{status.message}</span>
                {status.kbSyncMessage ? <span>KB sync {status.kbSyncMessage}</span> : null}
              </div>
            </div>
          ))}
        </div>
      )}

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
