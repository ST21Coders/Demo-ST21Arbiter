import { useCallback, useEffect, useRef, useState } from 'react'
import {
  CheckCircle, Clock, RefreshCw, Database, FileText, Server, Loader2, Activity,
  Upload, AlertTriangle, Boxes, Sparkles,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import {
  getUploadStatus,
  listDataGroupingProjects,
  listScanRuns,
  materializeDataGroupingProject,
  presignUpload,
  triggerDataIngest,
  uploadToPresignedUrl,
} from '../hooks/useApi'
import { formatDistanceToNow } from 'date-fns'

const GROUPS_STORAGE_KEY = 'arbiter.dataGrouping.v2.savedGroups'
const PROJECTS_STORAGE_KEY = 'arbiter.dataGrouping.v2.projects'
const PIPELINE_PROJECT_NAME = 'Discovery'
const PIPELINE_PROJECT_ID = 'discovery'
const GROUP_FILE_MIX_OPTIONS = [
  { id: 'csv_only', label: 'CSV only', description: 'Structured tables only' },
  { id: 'text_only', label: 'Text only', description: 'Notes, docs, facts' },
  { id: 'csv_text', label: 'CSV + text', description: 'Tables plus context' },
  { id: 'csv_text_media', label: 'CSV + text + images/docs', description: 'Tables plus evidence files' },
  { id: 'unstructured_vector', label: 'Unstructured + Vector', description: 'pdf/docx/txt/json → S3 Vectors (DocuSearch)' },
  { id: 'structured_vector_glue', label: 'Structured + Vector + Glue', description: 'csv/excel/parquet → Glue + S3 Vectors (Structured Analytics)' },
]

// Group-content mixes that feed the async S3-Vectors ingest worker (Phase 2
// backend). Each maps to a POST /data-pipeline/ingest jobType. Groups NOT in
// this map keep the existing KB/Glue publish behavior untouched (incl. Policy
// Documents — point 6-bis).
const VECTOR_INGEST_JOB_TYPE = {
  unstructured_vector: 'docusearch',
  structured_vector_glue: 'structured_analytics',
}

function ingestJobTypeForMix(fileMix) {
  return VECTOR_INGEST_JOB_TYPE[fileMix] || null
}

// Max files accepted per group upload batch (points 5b / 6c).
const MAX_UPLOAD_FILES = 200
const GROUP_KEY_FILE_SAMPLE_LIMIT = 40
const GROUP_KEY_CHANGE_SAMPLE_LIMIT = 12

function slugify(value) {
  return String(value || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '') || 'group'
}

function groupNameFromInput(value) {
  return String(value || '')
    .replace(/[^\p{L}\p{N}\s_-]+/gu, ' ')
    .trim()
    .replace(/\s+/g, '_')
    .replace(/_+/g, '_')
    .replace(/^_+|_+$/g, '')
}

function projectNameFromInput(value) {
  return String(value || '').trim()
}

function defaultProject() {
  return {
    id: PIPELINE_PROJECT_ID,
    name: PIPELINE_PROJECT_NAME,
    source: 'default',
  }
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

function readLocalProjects() {
  if (typeof window === 'undefined') return [defaultProject()]
  try {
    const saved = JSON.parse(localStorage.getItem(PROJECTS_STORAGE_KEY) || '[]')
    const projects = Array.isArray(saved) ? saved.filter(project => project?.id && project?.name) : []
    const byId = new Map([[PIPELINE_PROJECT_ID, defaultProject()]])
    projects.forEach(project => byId.set(project.id, { ...project, source: project.source || 'local' }))
    return [...byId.values()]
  } catch {
    return [defaultProject()]
  }
}

function persistLocalProjects(projects) {
  const byId = new Map([[PIPELINE_PROJECT_ID, defaultProject()]])
  projects.forEach(project => {
    if (project?.id && project?.name) byId.set(project.id, project)
  })
  localStorage.setItem(PROJECTS_STORAGE_KEY, JSON.stringify([...byId.values()]))
}

function upsertLocalProject(project) {
  if (!project?.id || !project?.name) return
  persistLocalProjects([...readLocalProjects(), { ...project, source: project.source || 'local' }])
}

function projectKey(projectId, groupName = '') {
  return `${projectId || PIPELINE_PROJECT_ID}::${groupName}`
}

function starterPromptsForGroup(groupName) {
  return [
    'For this group, rank stores from highest to lowest total sales. Include branch city, branch state, total revenue, units sold, transaction count, top category, and a short explanation.',
    'For this group, rank product categories by revenue and units sold. Include part category, total revenue, units sold, average unit price, and the leading branch if available.',
    'For this group, compare sales channels by revenue, units sold, transaction count, and average line revenue. Include a short explanation of channel mix.',
    'For this group, analyze gross margin using Unit_Cost and Unit_Price. Rank stores or products by estimated margin dollars and margin percent.',
  ]
}

function operationalStarterPromptsForGroup(groupName) {
  return [
    'For this group, create an operational asset performance summary by floor zone. Include asset count, activity volume, revenue, utilization, service calls, uptime, and revenue per asset.',
    'For this group, compare equipment categories by revenue, activity volume, utilization, and maintenance activity. Rank categories by total revenue.',
    'For this group, summarize maintenance impact by floor zone. Include service calls, uptime, maintenance cost, asset count, and related performance totals.',
  ]
}

function vendorIntelligenceStarterPromptsForGroup(groupName) {
  return [
    'For this group, create a vendor spend summary ranked from highest to lowest total amount. Include vendor ID, vendor name if available, total invoice amount, invoice count, document count, and business unit if available.',
    'For this group, list records for vendor V0066. Include filename or table, document type, date if available, amount if available, and a short neutral summary.',
    'For this group, compare contract, invoice, rate sheet, and payment reconciliation records by vendor. Include vendor ID, vendor name if available, record counts, total amounts if available, and useful next review steps.',
    'For this group, summarize vendor relationships across documents and tables. Include vendor ID, vendor category if available, related departments, document types, and timing patterns.',
  ]
}

function fileExtension(name = '') {
  const match = String(name).toLowerCase().match(/\.([a-z0-9]+)$/)
  return match ? match[1] : 'unknown'
}

function summarizeFileTypes(files = []) {
  return files.reduce((counts, file) => {
    const ext = fileExtension(file?.name || file?.key || '')
    counts[ext] = (counts[ext] || 0) + 1
    return counts
  }, {})
}

function groupKeyFileId(file = {}) {
  return file?.projectKey || file?.sourceKey || file?.key || file?.name || ''
}

function groupKeyFileInventory(files = []) {
  return files
    .filter(file => file && String(file.name || file.key || '').toLowerCase() !== 'group_key.json')
    .map(file => ({
      name: file.name || file.filename || file.key || 'unnamed file',
      type: file.type || fileExtension(file.name || file.key || ''),
      glue_table_hint: file.glueTableHint || '',
      added_at: file.addedAt || '',
    }))
    .slice(0, GROUP_KEY_FILE_SAMPLE_LIMIT)
}

function groupKeyFileCount(files = []) {
  return files.filter(file => file && String(file.name || file.key || '').toLowerCase() !== 'group_key.json').length
}

function groupKeyRecentChanges(existing, files, inventory, now) {
  const previous = Array.isArray(existing?.file_inventory) ? existing.file_inventory : []
  const previousCount = Number(existing?.file_structure?.file_count ?? previous.length) || previous.length
  const currentCount = groupKeyFileCount(files)
  const previousIds = new Map(previous.map(file => [groupKeyFileId({
    name: file.name,
  }), file]).filter(([id]) => id))
  const currentIds = new Map(files
    .filter(file => file && String(file.name || file.key || '').toLowerCase() !== 'group_key.json')
    .map(file => [groupKeyFileId({ name: file.name || file.filename || file.key }), {
      name: file.name || file.filename || file.key || 'unnamed file',
    }])
    .filter(([id]) => id))
  const added = [...currentIds.entries()]
    .filter(([id]) => !previousIds.has(id))
    .map(([, file]) => file.name)
  const removed = [...previousIds.entries()]
    .filter(([id]) => !currentIds.has(id))
    .map(([, file]) => file.name)
  return {
    updated_at: now,
    added_file_samples: added.slice(0, GROUP_KEY_CHANGE_SAMPLE_LIMIT),
    removed_file_samples: removed.slice(0, GROUP_KEY_CHANGE_SAMPLE_LIMIT),
    added_sample_truncated: added.length > GROUP_KEY_CHANGE_SAMPLE_LIMIT,
    removed_sample_truncated: removed.length > GROUP_KEY_CHANGE_SAMPLE_LIMIT,
    file_count_before: previousCount,
    file_count_after: currentCount,
    change_summary: added.length || removed.length
      ? `${added.length} file(s) added; ${removed.length} file(s) removed.`
      : 'No file membership changes detected; metadata refreshed.',
  }
}

function starterPromptsForProfile(groupName, profile) {
  if (profile?.kind === 'sales') return starterPromptsForGroup(groupName)
  if (profile?.kind === 'operational_asset_performance') return operationalStarterPromptsForGroup(groupName)
  if (profile?.kind === 'enterprise_vendor_intelligence') return vendorIntelligenceStarterPromptsForGroup(groupName)
  return [
    'List the available files and tables in this group and briefly explain what each one appears to contain.',
    'Summarize this group. Include row counts if available, important columns, and the most useful first questions to ask.',
    'Show the first records from the main table in this group and explain the likely purpose of the data.',
  ]
}

function buildGroupKey({ project, groupName, purpose, fileMix, files = [], profile = null, existing = null }) {
  const now = new Date().toISOString()
  const safePurpose = String(purpose || existing?.purpose || '').trim()
  const inferredProfile = profile || localGroupProfile(groupName, files, fileMix) || {}
  const fileInventory = groupKeyFileInventory(files)
  const fileCount = groupKeyFileCount(files)
  const domain = inferredProfile.kind === 'sales'
    ? 'sales operations'
    : inferredProfile.kind === 'operational_asset_performance'
    ? 'operational asset performance'
    : inferredProfile.kind === 'enterprise_vendor_intelligence'
    ? 'enterprise vendor intelligence'
    : existing?.domain || 'general data analysis'
  return {
    schema_version: existing?.schema_version || '1.0',
    group_name: groupName,
    project: project?.name || existing?.project || PIPELINE_PROJECT_NAME,
    summary: existing?.summary || safePurpose || `Data group for ${groupName}.`,
    purpose: safePurpose || existing?.purpose || 'Review and query this grouped dataset using its published files, tables, and supporting context.',
    domain: existing?.domain && existing.domain !== 'general data analysis' ? existing.domain : domain,
    group_profile: {
      kind: inferredProfile.kind || 'generic',
      confidence: inferredProfile.confidence || 'low',
      file_mix: inferredProfile.fileMix || fileMix || '',
      file_mix_label: inferredProfile.fileMixLabel || GROUP_FILE_MIX_OPTIONS.find(option => option.id === fileMix)?.label || '',
    },
    file_structure: {
      pattern: existing?.file_structure?.pattern || 'Files selected together by the user during Data Pipeline group setup.',
      content_mix: GROUP_FILE_MIX_OPTIONS.find(option => option.id === fileMix)?.label || existing?.file_structure?.content_mix || '',
      file_type_counts: summarizeFileTypes(files),
      file_count: fileCount,
      file_inventory_sample_count: fileInventory.length,
      combine_strategy: existing?.file_structure?.combine_strategy || (inferredProfile.kind === 'sales'
        ? 'Union compatible sales CSV files into one logical table and preserve branch, source file, product, channel, and customer fields when available.'
        : inferredProfile.kind === 'enterprise_vendor_intelligence'
        ? 'Use vendor IDs, document types, dates, invoice/payment amounts, contract/rate-sheet references, and shared vendor names to connect CSV, text, and JSON records.'
        : 'Use shared identifiers, filenames, table schemas, and supporting text to determine useful joins and analysis paths.'),
    },
    file_inventory: fileInventory,
    inventory_note: `Sample only. Complete membership is stored in project metadata; total files: ${fileCount}.`,
    recent_changes: groupKeyRecentChanges(existing, files, fileInventory, now),
    column_definitions: existing?.column_definitions || inferredProfile.columnDefinitions || {},
    relationships: existing?.relationships || inferredProfile.relationships || [],
    primary_questions: existing?.primary_questions || inferredProfile.primaryQuestions || [
      'What files and tables are available in this group?',
      'Which records, entities, categories, or locations stand out after combining the available evidence?',
      'What follow-up prompts should a user run next?',
    ],
    starter_prompts: existing?.starter_prompts?.length && inferredProfile.kind !== 'enterprise_vendor_intelligence'
      ? existing.starter_prompts
      : starterPromptsForProfile(groupName, inferredProfile),
    safe_language: existing?.safe_language || {
      use: ['review candidates', 'unusual patterns', 'audit signals', 'needs follow-up', 'operational signal'],
      avoid: ['unsupported conclusions', 'definitive accusations without evidence', 'claiming causation without supporting data'],
    },
    generation_notes: {
      generated_by: 'arbiter_data_pipeline',
      user_supplied_context: safePurpose,
      system_inferred: true,
      created_at: existing?.generation_notes?.created_at || now,
      updated_at: now,
      update_reason: existing ? 'group files or setup context changed' : 'initial group setup',
    },
  }
}

function localGroupProfile(groupName, files, fileMix = '') {
  const text = `${groupName} ${(files || []).map(file => file.name || file.key || '').join(' ')}`.toLowerCase()
  const base = {
    fileMix,
    fileMixLabel: GROUP_FILE_MIX_OPTIONS.find(option => option.id === fileMix)?.label || '',
  }
  if (text.includes('electronics') || text.includes('sales') || text.includes('line_revenue')) {
    return {
      ...base,
      kind: 'sales',
      confidence: 'medium',
      starterPrompts: starterPromptsForGroup(groupName),
    }
  }
  if (text.includes('gaming') || text.includes('casino') || text.includes('slot') || text.includes('machine') || text.includes('floor')) {
    return {
      ...base,
      kind: 'operational_asset_performance',
      confidence: 'medium',
      starterPrompts: operationalStarterPromptsForGroup(groupName),
    }
  }
  if (
    text.includes('enterprise vendor')
    || text.includes('vendor intelligence')
    || text.includes('vendor_master')
    || (text.includes('invoice') && text.includes('contract') && text.includes('vendor'))
    || (text.includes('payment_reconciliation') && text.includes('rate_sheet'))
  ) {
    return {
      ...base,
      kind: 'enterprise_vendor_intelligence',
      confidence: 'medium',
      starterPrompts: vendorIntelligenceStarterPromptsForGroup(groupName),
      primaryQuestions: [
        'Which vendors account for the largest invoice or payment totals?',
        'Which vendors have contract, rate sheet, invoice, payment reconciliation, legal review, audit, or security review records that should be read together?',
        'Which vendor relationships, timing patterns, or document clusters are most useful for business review?',
      ],
      relationships: [
        'Join or group records by vendor_id values such as V0066 when available.',
        'Use vendor names embedded in filenames as fallback relationship clues when a table column is not available.',
        'Compare contract, rate sheet, invoice, payment reconciliation, audit, credentialing, legal review, and security review documents by vendor and date.',
      ],
      columnDefinitions: {
        vendor_id: 'Stable vendor identifier such as V0066 when present.',
        vendor_name: 'Vendor display name when available in tables or filenames.',
        amount: 'Invoice, payment, rate, or reconciliation amount depending on source document type.',
        document_type: 'Business document category inferred from filename, table, or content.',
      },
    }
  }
  return fileMix ? { ...base, kind: fileMix, confidence: 'low' } : null
}

function resetLocalGroupForNewUpload(projectTarget, groupTarget, fileMix, purpose = '') {
  if (!groupTarget?.name) return
  const project = projectTarget || defaultProject()
  upsertLocalProject(project)
  const current = readLocalGroups()
  const nextGroup = {
    id: groupTarget.id || `${project.id}::${slugify(groupTarget.name)}`,
    projectId: project.id,
    projectName: project.name,
    name: groupTarget.name,
    type: 'pipeline_upload',
    files: [],
    fileKeys: [],
    groupProfile: localGroupProfile(groupTarget.name, [], fileMix),
    groupKey: buildGroupKey({ project, groupName: groupTarget.name, purpose, fileMix, files: [] }),
    updatedAt: new Date().toISOString(),
  }
  const others = current.filter(group => (
    group.id !== nextGroup.id
    && !(group.name === groupTarget.name && (group.projectId || PIPELINE_PROJECT_ID) === project.id)
  ))
  persistLocalGroups([...others, nextGroup])
}

function upsertLocalGroupFile(projectTarget, groupTarget, fileInfo, fileMix = '', purpose = '') {
  if (!groupTarget?.name || !fileInfo?.key) return
  const project = projectTarget || defaultProject()
  upsertLocalProject(project)
  const current = readLocalGroups()
  const existing = current.find(group => (
    group.id === groupTarget.id
    || (group.name === groupTarget.name && (group.projectId || PIPELINE_PROJECT_ID) === project.id)
  ))
  const nextGroup = {
    ...(existing || {}),
    id: existing?.id || groupTarget.id || `${project.id}::${slugify(groupTarget.name)}`,
    projectId: project.id,
    projectName: project.name,
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
  const inferredProfile = localGroupProfile(nextGroup.name, nextGroup.files, fileMix) || {}
  const existingKind = existing?.groupProfile?.kind || ''
  const shouldUpgradeProfile = inferredProfile.kind && (!existingKind || ['generic', 'csv_text', 'csv_only', 'text_only', 'mixed'].includes(existingKind))
  nextGroup.groupProfile = {
    ...(shouldUpgradeProfile ? inferredProfile : existing?.groupProfile || inferredProfile),
    fileMix: (shouldUpgradeProfile ? inferredProfile.fileMix : existing?.groupProfile?.fileMix) || fileMix,
    fileMixLabel: (shouldUpgradeProfile ? inferredProfile.fileMixLabel : existing?.groupProfile?.fileMixLabel) || GROUP_FILE_MIX_OPTIONS.find(option => option.id === fileMix)?.label || '',
  }
  nextGroup.groupKey = buildGroupKey({
    project,
    groupName: nextGroup.name,
    purpose,
    fileMix: nextGroup.groupProfile.fileMix || fileMix,
    files: nextGroup.files,
    profile: nextGroup.groupProfile,
    existing: existing?.groupKey,
  })
  const others = current.filter(group => (
    group.id !== nextGroup.id
    && !(group.name === nextGroup.name && (group.projectId || PIPELINE_PROJECT_ID) === project.id)
  ))
  persistLocalGroups([...others, nextGroup])
}

function dataGroupOptionsFromLocal() {
  return readLocalGroups().map(group => ({
    id: `local::${group.projectId || PIPELINE_PROJECT_ID}::${group.id || group.name}`,
    localId: group.id,
    projectId: group.projectId || PIPELINE_PROJECT_ID,
    projectName: group.projectName || PIPELINE_PROJECT_NAME,
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

// DocuSearch (Unstructured + Vector): files published into the group folder →
// async data-ingest worker chunks/embeds (Titan) → S3 Vectors. No KB, no scan.
const STEP_DEFS_VECTOR = [
  { key: 'raw',    label: 'Upload',    desc: 'Files uploaded + published into the group folder' },
  { key: 'embed',  label: 'Embed',     desc: 'Worker chunks text and embeds with Titan v2'      },
  { key: 'vector', label: 'S3 Vectors',desc: 'Indexed to the docs-vectors bucket — semantic search ready' },
]

// Structured Analytics (Structured + Vector + Glue): Glue catalog (via publish)
// for Athena SQL, plus the async worker embeds row facts → S3 Vectors.
const STEP_DEFS_ANALYTICS = [
  { key: 'raw',     label: 'Upload',     desc: 'Tabular files uploaded + published into the group folder' },
  { key: 'catalog', label: 'Glue',       desc: 'Glue catalog refreshed — Athena SQL queryable'            },
  { key: 'embed',   label: 'Embed',      desc: 'Worker serializes rows to facts and embeds with Titan v2' },
  { key: 'vector',  label: 'S3 Vectors', desc: 'Indexed to the analytics-vectors bucket — semantic search ready' },
]

function isStructuredUpload(u) {
  return (u.filename || '').toLowerCase().endsWith('.csv')
}

function stepDefsFor(u) {
  return isStructuredUpload(u) ? STEP_DEFS_STRUCTURED : STEP_DEFS
}

function uploadHasPublishedBackendObject(upload) {
  return Boolean(
    upload?.processingStatus?.processed?.exists
    || upload?.processingStatus?.structured?.exists
    || upload?.processingStatus?.status === 'catalog_done'
    || upload?.scanRun?.status === 'COMPLETED'
  )
}

function uploadReadyForGroupPublish(upload) {
  return Boolean(
    upload?.key
    && uploadHasPublishedBackendObject(upload)
  )
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
    s.raw = 'done'
    const status = upload.processingStatus
    if (upload.state === 'upload_failed' && !uploadHasPublishedBackendObject(upload)) {
      s.processed = 'failed'
      return s
    }
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
    if (status.status === 'catalog_failed') s.catalog = 'failed'
    else if (status.status === 'catalog_done' || status.structured?.exists) s.catalog = 'done'
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
  if (upload.state === 'upload_failed' && !uploadHasPublishedBackendObject(upload)) {
    states.raw = 'done'
    states.processed = 'failed'
    return states
  }

  // PUT succeeded; raw is done.
  states.raw = 'done'

  const run = upload.scanRun
  const status = upload.processingStatus
  if (!run && (status?.processed?.exists || status?.raw?.exists)) {
    states.processed = 'done'
    states.kb = 'done'
    states.scan = 'done'
    return states
  }
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

function ProjectSelector({
  creatingNewProject,
  setCreatingNewProject,
  selectedProjectId,
  setSelectedProjectId,
  newProjectName,
  setNewProjectName,
  projectOptions,
}) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
        <div className="min-w-[120px]">
          <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-500">Project</p>
          <p className="mt-1 text-xs text-slate-500">Choose this before ingesting data.</p>
        </div>
        <label className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-700">
          <input
            type="checkbox"
            checked={creatingNewProject}
            onChange={event => setCreatingNewProject(event.target.checked)}
            className="h-4 w-4 rounded border-slate-300 text-indigo-600"
          />
          New Project
        </label>
        {creatingNewProject ? (
          <input
            type="text"
            value={newProjectName}
            onChange={event => setNewProjectName(event.target.value)}
            placeholder="Project name"
            className="min-w-0 flex-1 rounded-lg border border-slate-200 px-3 py-2 text-sm outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
          />
        ) : (
          <select
            value={selectedProjectId}
            onChange={event => setSelectedProjectId(event.target.value)}
            className="min-w-0 flex-1 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
          >
            {projectOptions.map(project => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
        )}
      </div>
    </div>
  )
}

function UploadDropzone({
  onFile,
  disabled,
  creatingNewGroup,
  setCreatingNewGroup,
  newGroupName,
  setNewGroupName,
  newGroupPurpose,
  setNewGroupPurpose,
  groupFileMix,
  setGroupFileMix,
  selectedGroupId,
  setSelectedGroupId,
  groupOptions,
}) {
  const [dragging, setDragging] = useState(false)
  const [capNotice, setCapNotice] = useState('')
  const inputRef = useRef(null)
  const canBrowse = !disabled

  function handleFiles(files) {
    if (!files || !files.length) return
    let list = Array.from(files)
    if (list.length > MAX_UPLOAD_FILES) {
      setCapNotice(`Selected ${list.length} files; only the first ${MAX_UPLOAD_FILES} will be uploaded (per-batch limit).`)
      list = list.slice(0, MAX_UPLOAD_FILES)
    } else {
      setCapNotice('')
    }
    list.forEach((f, index) => onFile(f, { resetNewGroup: creatingNewGroup && index === 0 }))
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
        <select
          value={groupFileMix}
          onChange={event => setGroupFileMix(event.target.value)}
          className="min-w-[220px] rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
          title="Group content mix"
        >
          <option value="">Group contents</option>
          {GROUP_FILE_MIX_OPTIONS.map(option => (
            <option key={option.id} value={option.id}>
              {option.label}
            </option>
          ))}
        </select>
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
      {creatingNewGroup && (
        <textarea
          value={newGroupPurpose}
          onChange={event => setNewGroupPurpose(event.target.value)}
          placeholder="Optional: briefly describe what this group contains and what the user wants to learn from it (improves answer quality)."
          rows={2}
          className="mt-3 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
        />
      )}
      <div className="mt-4 flex flex-col items-center gap-2 rounded-lg border border-slate-100 bg-slate-50 px-4 py-6 text-center">
        <div className="w-12 h-12 rounded-full bg-indigo-50 border border-indigo-200 flex items-center justify-center">
          <Upload size={20} className="text-indigo-600" />
        </div>
        <p className="text-sm font-semibold text-slate-900">
          {disabled ? 'Choose a project, group, and content mix before selecting files' : 'Drop files here to add them to the selected group'}
        </p>
        <p className="text-xs text-slate-500">
          {groupFileMix
            ? GROUP_FILE_MIX_OPTIONS.find(option => option.id === groupFileMix)?.description
            : 'Choose the group content mix before selecting files'}
        </p>
        <p className="text-[10px] text-slate-400">Up to {MAX_UPLOAD_FILES} files per batch.</p>
      </div>
      {capNotice && (
        <p className="mt-2 flex items-center gap-1.5 text-xs text-amber-700">
          <AlertTriangle size={12} /> {capNotice}
        </p>
      )}
      <input
        ref={inputRef}
        type="file"
        multiple
        disabled={disabled}
        accept=".md,.pdf,.docx,.json,.txt,.csv,.xlsx,.xls,.parquet,.png,.jpg,.jpeg,.webp,.tif,.tiff"
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
  const readyForPublish = uploadReadyForGroupPublish(upload)
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
        {readyForPublish && !upload.groupMaterialized && (
          <p className="text-xs text-emerald-700 flex-shrink-0">Ready to publish</p>
        )}
        {!readyForPublish && !finished && structured && statusMessage && (
          <p className="text-xs text-amber-700 flex-shrink-0">{statusMessage}</p>
        )}
        {finished && !structured && upload.scanRun?.totals && (
          <p className="text-xs text-emerald-700 flex-shrink-0">
            {upload.scanRun.totals.conflicts ?? 0} conflicts · {upload.scanRun.totals.compliant ?? 0} compliant
          </p>
        )}
        {!finished && !readyForPublish && (
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
      {upload.error && !readyForPublish && (
        <p className="text-xs text-red-700 mt-2 flex items-center gap-1.5">
          <AlertTriangle size={12} /> {upload.error}
        </p>
      )}
      {!upload.error && upload.statusCheckError && !readyForPublish && (
        <p className="text-xs text-slate-500 mt-2">
          Status check delayed; retrying quietly.
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
    id: 'docusearch',
    title: 'DocuSearch',
    subtitle: 'pdf/docx/txt/json → S3 Vectors · semantic search from s3 vector',
    steps: STEP_DEFS_VECTOR,
    Icon: Boxes,
    accent: { bg: '#f5f3ff', icon: '#7c3aed', border: '#ddd6fe' },
  },
  {
    id: 'structured_sql',
    title: 'Structured Analytics — SQL',
    subtitle: 'CSV only → Glue / Athena SQL · analytics without S3 Vectors',
    steps: STEP_DEFS_STRUCTURED,
    Icon: Database,
    accent: { bg: '#fff7ed', icon: '#ea580c', border: '#fed7aa' },
  },
  {
    id: 'structured_vector',
    title: 'Structured Analytics — Semantic + SQL',
    subtitle: 'Structured + Vector + Glue → Glue + S3 Vectors · Semantic + Analytics search from s3 vector',
    steps: STEP_DEFS_ANALYTICS,
    Icon: Boxes,
    accent: { bg: '#f0fdfa', icon: '#0d9488', border: '#99f6e4' },
  },
]

const STEP_ICON = { raw: Upload, processed: Server, kb: Database, catalog: Database, scan: CheckCircle, embed: Sparkles, vector: Boxes }

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
  const [projectOptions, setProjectOptions] = useState(() => readLocalProjects())
  const [creatingNewProject, setCreatingNewProject] = useState(false)
  const [selectedProjectId, setSelectedProjectId] = useState(PIPELINE_PROJECT_ID)
  const [newProjectName, setNewProjectName] = useState('')
  const [groupOptions, setGroupOptions] = useState([])
  const [groupsLoading, setGroupsLoading] = useState(false)
  const [creatingNewGroup, setCreatingNewGroup] = useState(false)
  const [selectedGroupId, setSelectedGroupId] = useState('')
  const [newGroupName, setNewGroupName] = useState('')
  const [newGroupPurpose, setNewGroupPurpose] = useState('')
  const [groupFileMix, setGroupFileMix] = useState('')
  const [groupPublishStatus, setGroupPublishStatus] = useState({})
  const groupPublishInFlightRef = useRef(new Set())
  // Async S3-Vectors ingestion status per group (DocuSearch / Structured Analytics).
  const [ingestStatus, setIngestStatus] = useState({})
  const ingestInFlightRef = useRef(new Set())

  // Per-upload state machine. We update via a single setState that maps over
  // the existing array, so concurrent polls + new uploads don't race each other.
  const updateUpload = useCallback((id, patch) => {
    setUploads(prev => prev.map(u => u.id === id ? { ...u, ...patch } : u))
  }, [])

  const refreshGroups = useCallback(async () => {
    const local = dataGroupOptionsFromLocal()
    setGroupOptions(local)
    const projectsById = new Map([[PIPELINE_PROJECT_ID, defaultProject()]])
    local.forEach(group => {
      if (group.projectId && group.projectName) {
        projectsById.set(group.projectId, {
          id: group.projectId,
          name: group.projectName,
          source: group.source || 'local',
        })
      }
    })
    setGroupsLoading(true)
    try {
      const data = await listDataGroupingProjects()
      const remote = (data.groups || []).map(group => ({
        ...group,
        id: group.id || `${group.projectId || PIPELINE_PROJECT_ID}::${group.groupName}`,
        projectId: group.projectId || PIPELINE_PROJECT_ID,
        projectName: group.projectName || PIPELINE_PROJECT_NAME,
        source: 'remote',
      }))
      remote.forEach(group => {
        if (group.projectId && group.projectName) {
          projectsById.set(group.projectId, {
            id: group.projectId,
            name: group.projectName,
            source: 'remote',
          })
        }
      })
      const byProjectGroup = new Map()
      ;[...local, ...remote].forEach(group => {
        if (!group?.groupName) return
        byProjectGroup.set(projectKey(group.projectId, group.groupName), group)
      })
      setGroupOptions([...byProjectGroup.values()].sort((a, b) => (
        String(a.projectName || '').localeCompare(String(b.projectName || ''))
        || String(a.groupName).localeCompare(String(b.groupName))
      )))
    } catch {
      setGroupOptions(local)
    } finally {
      const projects = [...projectsById.values()].sort((a, b) => String(a.name).localeCompare(String(b.name)))
      persistLocalProjects(projects)
      setProjectOptions(projects)
      setGroupsLoading(false)
    }
  }, [])

  useEffect(() => {
    refreshGroups()
  }, [refreshGroups])

  useEffect(() => {
    setSelectedGroupId('')
  }, [creatingNewProject, selectedProjectId, newProjectName])

  function currentProjectTarget() {
    if (creatingNewProject) {
      const name = projectNameFromInput(newProjectName)
      if (!name) return null
      return {
        id: slugify(name),
        name,
        source: 'new',
      }
    }
    return projectOptions.find(project => project.id === selectedProjectId) || defaultProject()
  }

  function currentGroupTarget() {
    const project = currentProjectTarget()
    if (!project) return null
    if (creatingNewGroup) {
      const name = groupNameFromInput(newGroupName)
      if (!name) return null
      return {
        id: `${project.id}::${slugify(name)}`,
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

  async function handleFile(file, options = {}) {
    const projectTarget = currentProjectTarget()
    const groupTarget = currentGroupTarget()
    if (!projectTarget || !groupTarget) return
    const publishKey = projectKey(projectTarget.id, groupTarget.name)
    if (creatingNewGroup && options.resetNewGroup) {
      resetLocalGroupForNewUpload(projectTarget, groupTarget, groupFileMix, newGroupPurpose)
      setGroupPublishStatus(prev => {
        const next = { ...prev }
        delete next[publishKey]
        return next
      })
    }
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
      projectId: projectTarget.id,
      projectName: projectTarget.name,
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
    upsertLocalGroupFile(projectTarget, groupTarget, {
      key: pre.key,
      name: file.name,
      size: file.size,
      last_modified: new Date().toISOString(),
    }, groupFileMix, creatingNewGroup ? newGroupPurpose : '')
    refreshGroups()
    // Polling effect will pick this up and update scanRun as the chain progresses.
  }

  function selectedGroupName() {
    if (creatingNewGroup) return groupNameFromInput(newGroupName)
    return groupOptions.find(group => group.id === selectedGroupId)?.groupName || ''
  }

  async function filesReadyForGroupPublish(projectTarget, groupName, files) {
    const publishKey = projectKey(projectTarget?.id, groupName)
    setGroupPublishStatus(prev => ({
      ...prev,
      [publishKey]: {
        projectName: projectTarget?.name || PIPELINE_PROJECT_NAME,
        groupName,
        state: 'checking',
        message: `Checking ${files.length} files before publishing ${groupName}...`,
      },
    }))
    const pending = []
    const failed = []
    const concurrency = 12
    let index = 0
    async function checkNext() {
      while (index < files.length) {
        const file = files[index]
        index += 1
        const key = fileKey(file)
        if (!key) continue
        try {
          const status = await getUploadStatus(key)
          if (!status?.processed?.exists && !status?.structured?.exists) {
            pending.push(file.name || key)
          }
        } catch (err) {
          const message = err.message || 'status check failed'
          if (/401|403|outside caller upload prefix|auth expired/i.test(message)) {
            failed.push(`${file.name || key}: ${message}`)
          } else {
            pending.push(file.name || key)
          }
        }
      }
    }
    await Promise.all(Array.from({ length: Math.min(concurrency, files.length) }, () => checkNext()))
    if (failed.length) {
      setGroupPublishStatus(prev => ({
        ...prev,
        [publishKey]: {
          projectName: projectTarget?.name || PIPELINE_PROJECT_NAME,
          groupName,
          state: 'failed',
          message: `Could not verify ${failed.length} file${failed.length === 1 ? '' : 's'} before publishing. ${failed.slice(0, 2).join(' | ')}`,
        },
      }))
      return false
    }
    if (pending.length) {
      setGroupPublishStatus(prev => ({
        ...prev,
        [publishKey]: {
          projectName: projectTarget?.name || PIPELINE_PROJECT_NAME,
          groupName,
          state: 'waiting',
          message: `${pending.length}/${files.length} file${pending.length === 1 ? '' : 's'} still moving into processed storage.`,
        },
      }))
      return false
    }
    return true
  }

  async function materializeGroup(groupName) {
    const projectTarget = currentProjectTarget()
    if (!projectTarget || !groupName) return
    const publishKey = projectKey(projectTarget.id, groupName)
    if (groupPublishInFlightRef.current.has(publishKey)) return
    const localGroups = readLocalGroups()
    const localGroup = localGroups.find(group => (
      group.name === groupName
      && (group.projectId || PIPELINE_PROJECT_ID) === projectTarget.id
    ))
    const files = (localGroup?.files || []).filter(file => fileKey(file))
    if (!localGroup || !files.length) return

    groupPublishInFlightRef.current.add(publishKey)
    setUploads(prev => prev.map(upload => (
      upload.groupName === groupName && upload.projectId === projectTarget.id
        ? { ...upload, groupMaterializing: true, groupError: null }
        : upload
    )))
    try {
      const ready = await filesReadyForGroupPublish(projectTarget, groupName, files)
      if (!ready) return
      setGroupPublishStatus(prev => ({
        ...prev,
        [publishKey]: {
          projectName: projectTarget.name,
          groupName,
          state: 'publishing',
          message: `Publishing ${files.length} files into ${groupName}...`,
        },
      }))
      const result = await materializeDataGroupingProject({
        projectName: projectTarget.name,
        projectId: projectTarget.id,
        groups: [{
          id: localGroup.id || slugify(groupName),
          name: groupName,
          type: localGroup?.type || 'pipeline_upload',
          groupProfile: localGroup?.groupProfile || localGroupProfile(groupName, files, groupFileMix),
          groupKey: localGroup?.groupKey,
          files,
        }],
        move: false,
        // Vector-ingest groups (DocuSearch / Structured Analytics) index into S3
        // Vectors via the async worker, not the Bedrock KB — skip KB sync for them.
        syncKnowledgeBase: !ingestJobTypeForMix(groupFileMix),
      })
      const materializedGroup = (result?.metadata?.groups || []).find(group => group?.name === groupName)
      const factSources = materializedGroup?.structuredFacts?.counts?.factSources || 0
      const materializationIssues = result?.materializationIssues?.length
        ? result.materializationIssues
        : (materializedGroup?.materializationIssues || [])
      const needsAttention = result?.materializationStatus === 'needs_attention' || materializationIssues.length > 0
      const issueMessage = materializationIssues[0]?.message || 'Glue table verification needs attention before Athena queries are reliable.'
      setGroupPublishStatus(prev => ({
        ...prev,
        [publishKey]: {
          projectName: projectTarget.name,
          groupName,
          state: needsAttention ? 'needs_attention' : 'published',
          message: needsAttention
            ? `${groupName} copied, but structured table verification needs attention: ${issueMessage}`
            : `${groupName} published with ${files.length} files${factSources ? ` and ${factSources} text fact source${factSources === 1 ? '' : 's'} indexed` : ''}.`,
          kbSyncMessage: result?.kbSync?.message || '',
          structuredFactSources: factSources,
          materializationIssues,
        },
      }))
      setUploads(prev => prev.map(upload => (
        upload.groupName === groupName && upload.projectId === projectTarget.id
          ? {
              ...upload,
              groupMaterializing: false,
              groupMaterialized: true,
              groupError: needsAttention ? issueMessage : null,
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
        [publishKey]: {
          projectName: projectTarget.name,
          groupName,
          state: 'failed',
          message,
        },
      }))
      setUploads(prev => prev.map(upload => (
        upload.groupName === groupName && upload.projectId === projectTarget.id
          ? { ...upload, groupMaterializing: false, groupError: message }
          : upload
      )))
    } finally {
      groupPublishInFlightRef.current.delete(publishKey)
      setUploads(prev => prev.map(upload => (
        upload.groupName === groupName && upload.projectId === projectTarget.id && upload.groupMaterializing && !upload.groupMaterialized
          ? { ...upload, groupMaterializing: false }
          : upload
      )))
    }
  }

  // Submit an async S3-Vectors ingestion job for a vector-mix group (DocuSearch
  // or Structured Analytics). The group's files must already be published into
  // projects/<projectId>/<group>/ — the worker reads that prefix. Re-clicking
  // re-ingests (idempotent: deterministic chunk keys re-key in place).
  async function submitIngestion(groupName) {
    const projectTarget = currentProjectTarget()
    const jobType = ingestJobTypeForMix(groupFileMix)
    if (!projectTarget || !groupName || !jobType) return
    const publishKey = projectKey(projectTarget.id, groupName)
    if (ingestInFlightRef.current.has(publishKey)) return
    ingestInFlightRef.current.add(publishKey)
    setIngestStatus(prev => ({
      ...prev,
      [publishKey]: {
        projectName: projectTarget.name, groupName, jobType,
        state: 'submitting', message: `Submitting ${jobType} ingestion for ${groupName}…`,
      },
    }))
    try {
      const res = await triggerDataIngest({
        jobType,
        projectId: projectTarget.id,
        projectName: projectTarget.name,
        groupName,
      })
      setIngestStatus(prev => ({
        ...prev,
        [publishKey]: {
          projectName: projectTarget.name, groupName, jobType,
          state: 'submitted', jobId: res?.job_id, vectorIndex: res?.vector_index,
          message: `Ingestion job ${res?.job_id || ''} queued → S3 Vectors index “${res?.vector_index || ''}”. Track it on the Data Jobs page.`,
        },
      }))
    } catch (err) {
      setIngestStatus(prev => ({
        ...prev,
        [publishKey]: {
          projectName: projectTarget.name, groupName, jobType,
          state: 'failed', message: err.message || 'ingestion submit failed',
        },
      }))
    } finally {
      ingestInFlightRef.current.delete(publishKey)
    }
  }

  // Poll /scan-runs every 5s while any upload is still in-flight. We stop once
  // every upload has either finished or failed, then resume when a new upload
  // appears.
  useEffect(() => {
    const active = uploads.some(u => {
      if (u.state === 'upload_failed') return false
      if (uploadReadyForGroupPublish(u)) return false
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
          !uploadReadyForGroupPublish(u) &&
          !['catalog_done', 'catalog_failed'].includes(u.processingStatus?.status)
        )
        const statusById = {}
        await Promise.all(statusUploads.map(async u => {
          try {
            statusById[u.id] = await getUploadStatus(u.key)
          } catch (err) {
            statusById[u.id] = { status: 'status_check_delayed', message: err.message || 'status check delayed' }
          }
        }))
        // For each upload that has a key but no terminal scanRun, see if a row
        // matching its triggered_by has appeared.
        setUploads(prev => prev.map(u => {
          if (statusById[u.id]) {
            if (statusById[u.id].status === 'status_check_delayed') {
              return {
                ...u,
                statusCheckError: statusById[u.id].message,
                statusCheckDelayedAt: new Date().toISOString(),
              }
            }
            return { ...u, processingStatus: statusById[u.id], statusCheckError: null }
          }
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

  const currentProject = currentProjectTarget()
  const currentProjectReady = Boolean(currentProject?.id && currentProject?.name)
  const filteredGroupOptions = groupOptions.filter(group => (group.projectId || PIPELINE_PROJECT_ID) === currentProject?.id)
  const groupTargetReady = currentProjectReady && (creatingNewGroup ? Boolean(groupNameFromInput(newGroupName)) : Boolean(selectedGroupId)) && Boolean(groupFileMix)
  const currentSelectedGroupName = selectedGroupName()
  const currentPublishKey = currentProject?.id && currentSelectedGroupName ? projectKey(currentProject.id, currentSelectedGroupName) : ''
  const currentPublishStatus = currentPublishKey ? groupPublishStatus[currentPublishKey] : null
  const currentGroupUploads = uploads.filter(upload => (
    upload.projectId === currentProject?.id
    && upload.groupName === currentSelectedGroupName
  ))
  const currentGroupReadyUploads = currentGroupUploads.filter(uploadReadyForGroupPublish)
  const currentGroupFailedUploads = currentGroupUploads.filter(upload => (
    (upload.state === 'upload_failed' || upload.error)
    && !uploadReadyForGroupPublish(upload)
  ))
  const currentLiveBatchReady = !currentGroupUploads.length || (
    currentGroupReadyUploads.length === currentGroupUploads.length
    && !currentGroupFailedUploads.length
  )
  // Vector-ingest (DocuSearch / Structured Analytics) submit gating.
  const currentIngestJobType = ingestJobTypeForMix(groupFileMix)
  const currentIngestStatus = currentPublishKey ? ingestStatus[currentPublishKey] : null
  const currentGroupPublished = ['published', 'needs_attention'].includes(currentPublishStatus?.state)
  const currentExistingGroupSelected = !creatingNewGroup && Boolean(selectedGroupId)
  const canSubmitIngest = Boolean(currentSelectedGroupName)
    && Boolean(currentIngestJobType)
    && (currentGroupPublished || currentExistingGroupSelected)

  return (
    <div className="p-6 space-y-6 page-container">
      <div>
        <h1 className="text-lg font-bold text-slate-900 tracking-tight">Data Pipeline</h1>
        <p className="text-xs text-slate-500 mt-0.5">
          Policy docs → KB ingestion → scan. DocuSearch (pdf/docx/txt/json) + Structured Analytics (csv/excel/parquet) → S3 Vectors semantic search (track jobs on <Link to="/data-jobs" className="font-semibold text-indigo-600 hover:underline">Data Jobs</Link>). Processed in ~30-60s.
        </p>
      </div>

      {/* Upload zone */}
      <ProjectSelector
        creatingNewProject={creatingNewProject}
        setCreatingNewProject={setCreatingNewProject}
        selectedProjectId={selectedProjectId}
        setSelectedProjectId={setSelectedProjectId}
        newProjectName={newProjectName}
        setNewProjectName={setNewProjectName}
        projectOptions={projectOptions}
      />
      <UploadDropzone
        onFile={handleFile}
        disabled={!groupTargetReady}
        creatingNewGroup={creatingNewGroup}
        setCreatingNewGroup={setCreatingNewGroup}
        newGroupName={newGroupName}
        setNewGroupName={setNewGroupName}
        newGroupPurpose={newGroupPurpose}
        setNewGroupPurpose={setNewGroupPurpose}
        groupFileMix={groupFileMix}
        setGroupFileMix={setGroupFileMix}
        selectedGroupId={selectedGroupId}
        setSelectedGroupId={setSelectedGroupId}
        groupOptions={filteredGroupOptions}
      />
      <div className="rounded-xl border border-slate-200 bg-white px-4 py-3 text-xs text-slate-600">
        {groupTargetReady ? (
          <div className="flex flex-wrap items-center justify-between gap-3">
            <span>
              Uploads will be assigned to <span className="font-semibold text-slate-800">{currentProject?.name}</span> / <span className="font-semibold text-slate-800">{currentSelectedGroupName}</span>. Each uploaded file is owned by that group and will be published into group metadata after processing.
              {currentGroupUploads.length ? (
                <span className="ml-2 font-semibold text-slate-700">
                  {currentGroupReadyUploads.length}/{currentGroupUploads.length} ready to publish
                  {currentGroupFailedUploads.length ? ` · ${currentGroupFailedUploads.length} need attention` : ''}
                </span>
              ) : null}
            </span>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => materializeGroup(currentSelectedGroupName)}
                disabled={!currentSelectedGroupName || !currentLiveBatchReady || currentPublishStatus?.state === 'checking' || currentPublishStatus?.state === 'publishing'}
                className="inline-flex items-center gap-1.5 rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-2 text-xs font-semibold text-indigo-700 hover:bg-indigo-100 disabled:cursor-not-allowed disabled:border-slate-200 disabled:bg-slate-100 disabled:text-slate-400"
              >
                {currentPublishStatus?.state === 'checking' || currentPublishStatus?.state === 'publishing' ? (
                  <Loader2 size={13} className="animate-spin" />
                ) : (
                  <Upload size={13} />
                )}
                Publish selected group
              </button>
              {currentIngestJobType && (
                <button
                  type="button"
                  onClick={() => submitIngestion(currentSelectedGroupName)}
                  disabled={!canSubmitIngest || currentIngestStatus?.state === 'submitting'}
                  title={canSubmitIngest
                    ? 'Chunk, embed, and index this group into S3 Vectors'
                    : 'Publish the group first, then submit vector ingestion'}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-violet-200 bg-violet-50 px-3 py-2 text-xs font-semibold text-violet-700 hover:bg-violet-100 disabled:cursor-not-allowed disabled:border-slate-200 disabled:bg-slate-100 disabled:text-slate-400"
                >
                  {currentIngestStatus?.state === 'submitting' ? (
                    <Loader2 size={13} className="animate-spin" />
                  ) : (
                    <Boxes size={13} />
                  )}
                  {currentIngestStatus?.state === 'submitted' ? 'Re-ingest to S3 Vectors' : 'Submit vector ingestion'}
                </button>
              )}
            </div>
          </div>
        ) : (
          <span>
            {groupsLoading ? 'Loading existing groups...' : 'Choose or create a project, then choose or create a group and content mix before selecting files.'}
          </span>
        )}
      </div>
      {Object.keys(groupPublishStatus).length > 0 && (
        <div className="space-y-2">
          {Object.entries(groupPublishStatus).map(([statusKey, status]) => (
            <div
              key={statusKey}
              className={`rounded-xl border px-4 py-3 text-xs ${
                status.state === 'failed'
                  ? 'border-red-200 bg-red-50 text-red-700'
                : status.state === 'needs_attention'
                  ? 'border-amber-200 bg-amber-50 text-amber-800'
                : status.state === 'published'
                  ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
                  : 'border-amber-200 bg-amber-50 text-amber-800'
              }`}
            >
              <div className="flex flex-wrap items-center gap-2">
                {status.state === 'publishing' || status.state === 'checking' ? (
                  <Loader2 size={13} className="animate-spin" />
                ) : status.state === 'failed' || status.state === 'needs_attention' ? (
                  <AlertTriangle size={13} />
                ) : (
                  <CheckCircle size={13} />
                )}
                <span className="font-semibold">{status.projectName ? `${status.projectName} / ` : ''}{status.groupName || statusKey}</span>
                <span>{status.message}</span>
                {status.kbSyncMessage ? <span>KB sync {status.kbSyncMessage}</span> : null}
              </div>
            </div>
          ))}
        </div>
      )}
      {Object.keys(ingestStatus).length > 0 && (
        <div className="space-y-2">
          {Object.entries(ingestStatus).map(([statusKey, status]) => (
            <div
              key={statusKey}
              className={`rounded-xl border px-4 py-3 text-xs ${
                status.state === 'failed'
                  ? 'border-red-200 bg-red-50 text-red-700'
                : status.state === 'submitted'
                  ? 'border-violet-200 bg-violet-50 text-violet-800'
                  : 'border-slate-200 bg-slate-50 text-slate-600'
              }`}
            >
              <div className="flex flex-wrap items-center gap-2">
                {status.state === 'submitting' ? (
                  <Loader2 size={13} className="animate-spin" />
                ) : status.state === 'failed' ? (
                  <AlertTriangle size={13} />
                ) : (
                  <Boxes size={13} />
                )}
                <span className="font-semibold">{status.projectName ? `${status.projectName} / ` : ''}{status.groupName || statusKey}</span>
                <span>{status.message}</span>
                {status.state === 'submitted' && (
                  <Link to="/data-jobs" className="font-semibold text-violet-700 underline underline-offset-2 hover:text-violet-900">
                    View Data Jobs →
                  </Link>
                )}
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

    </div>
  )
}
