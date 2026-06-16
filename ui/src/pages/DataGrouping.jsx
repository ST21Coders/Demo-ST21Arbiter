import { useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle, CheckCircle, ChevronDown, ChevronRight, Database, Download, Edit3,
  FileSpreadsheet, FolderTree, Loader2, Plus, RefreshCw, Save, Trash2, Wand2, XCircle,
} from 'lucide-react'
import { USE_MOCK } from '../config'
import { listUploadedFiles } from '../hooks/useApi'

const GROUPING_STORAGE_KEY = 'arbiter.dataGrouping.projectMetadata'
const GROUPS_STORAGE_KEY = 'arbiter.dataGrouping.savedGroups'
const METADATA_LEDGER_STORAGE_KEY = 'arbiter.dataGrouping.metadataLedger'
const ASSOCIATION_OPTIONS = ['A', 'B', 'C', 'D', 'E']

const GROUP_TYPE_OPTIONS = [
  { value: 'accounts_receivable_invoices', label: 'AR Invoices', suggestedName: 'AR_Invoices', summaryFile: 'SUM_AR_Invoices.csv' },
  { value: 'accounts_payable_invoices', label: 'AP Invoices', suggestedName: 'AP_Invoices', summaryFile: 'SUM_AP_Invoices.csv' },
  { value: 'spreadsheet_collection', label: 'Spreadsheet collection', suggestedName: 'Spreadsheet_Group', summaryFile: 'SUM_Spreadsheet_Group.csv' },
  { value: 'project_supporting_files', label: 'Supporting files', suggestedName: 'Supporting_Files', summaryFile: '' },
]

const SAMPLE_ROWS = {
  'AR_Invoice_001.csv': [
    { invoice_id: 'AR-1001', customer: 'Atlas Retail', invoice_date: '2026-06-01', amount: '12450.00', status: 'open' },
    { invoice_id: 'AR-1002', customer: 'Beacon Health', invoice_date: '2026-06-02', amount: '8800.00', status: 'paid' },
  ],
  'AR_Invoice_002.csv': [
    { invoice_id: 'AR-1003', customer: 'Canyon Labs', invoice_date: '2026-06-03', amount: '15775.50', status: 'open' },
    { invoice_id: 'AR-1004', customer: 'Dover Foods', invoice_date: '2026-06-04', amount: '4320.00', status: 'open' },
  ],
  'AR_Invoice_003.csv': [
    { invoice_id: 'AR-1005', customer: 'Evergreen Supply', invoice_date: '2026-06-05', amount: '1900.00', status: 'paid' },
  ],
  'AR_Invoice_004.csv': [
    { invoice_id: 'AR-1006', customer: 'Forge Works', invoice_date: '2026-06-06', amount: '11200.00', status: 'open' },
  ],
  'AR_Invoice_005.csv': [
    { invoice_id: 'AR-1007', customer: 'Granite Services', invoice_date: '2026-06-07', amount: '7035.25', status: 'open' },
  ],
  'AP_Invoice_001.csv': [
    { invoice_id: 'AP-2001', vendor: 'Northstar Cloud', invoice_date: '2026-06-01', amount: '6200.00', status: 'approved' },
    { invoice_id: 'AP-2002', vendor: 'Summit Legal', invoice_date: '2026-06-02', amount: '4500.00', status: 'pending' },
  ],
  'AP_Invoice_002.csv': [
    { invoice_id: 'AP-2003', vendor: 'Brightline Data', invoice_date: '2026-06-03', amount: '9750.00', status: 'approved' },
  ],
  'AP_Invoice_003.csv': [
    { invoice_id: 'AP-2004', vendor: 'Pioneer Security', invoice_date: '2026-06-04', amount: '12250.00', status: 'pending' },
  ],
  'AP_Invoice_004.csv': [
    { invoice_id: 'AP-2005', vendor: 'Harbor Hardware', invoice_date: '2026-06-05', amount: '3100.00', status: 'approved' },
  ],
  'AP_Invoice_005.csv': [
    { invoice_id: 'AP-2006', vendor: 'Keystone Audit', invoice_date: '2026-06-06', amount: '8400.00', status: 'approved' },
  ],
}

function slugify(value) {
  return String(value || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '') || 'project'
}

function formatBytes(bytes) {
  const value = Number(bytes) || 0
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / (1024 * 1024)).toFixed(1)} MB`
}

function fileKey(file) {
  return file?.key || file?.name || ''
}

function groupFileKeys(group) {
  if (Array.isArray(group.fileKeys)) return group.fileKeys
  return (group.files || []).map(file => fileKey(file)).filter(Boolean)
}

function csvEscape(value) {
  const text = String(value ?? '')
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
}

function toCsv(rows) {
  if (!rows.length) return ''
  const headers = [...new Set(rows.flatMap(row => Object.keys(row)))]
  return [
    headers.map(csvEscape).join(','),
    ...rows.map(row => headers.map(header => csvEscape(row[header])).join(',')),
  ].join('\n')
}

function downloadText(filename, text, type = 'text/plain') {
  const blob = new Blob([text], { type })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

function optionForType(type) {
  return GROUP_TYPE_OPTIONS.find(option => option.value === type) || GROUP_TYPE_OPTIONS[2]
}

function makeSummaryName(groupName, groupType) {
  const option = optionForType(groupType)
  if (option.summaryFile) return option.summaryFile
  return `SUM_${String(groupName || 'Group').replace(/[^a-zA-Z0-9]+/g, '_').replace(/^_+|_+$/g, '')}.csv`
}

function rowsForGroup(group) {
  return group.files.flatMap(file => {
    const rows = SAMPLE_ROWS[file.name] || []
    if (!rows.length) {
      return [{
        source_file: file.name,
        source_key: file.key,
        note: 'File content not loaded in this local grouping preview',
      }]
    }
    return rows.map(row => ({ source_file: file.name, ...row }))
  })
}

function amountTotal(rows) {
  return rows.reduce((sum, row) => sum + (Number(row.amount) || 0), 0)
}

function buildMetadata({ projectName, projectId, processedPrefix, groups, summaries }) {
  const createdAt = new Date().toISOString()
  const metadataKey = `${processedPrefix}${projectId}/metadata/project.json`
  return {
    projectId,
    displayName: projectName,
    createdAt,
    sourcePrefix: processedPrefix,
    processedPrefix,
    metadataKey,
    metadataStorageMode: 'local_s3_simulation',
    groupVersion: '0.3',
    simulatedS3Writes: [
      {
        operation: 'put_object',
        key: metadataKey,
        contentType: 'application/json',
        note: 'This simulates the project metadata object that will be written to S3 in the backend version.',
      },
      ...groups.flatMap(group => group.files.map(file => ({
        operation: 'copy_object_with_metadata',
        key: file.key,
        metadataDirective: 'REPLACE',
        simulatedMetadata: {
          'arbiter-project-id': projectId,
          'arbiter-group-id': group.id,
          'arbiter-group-name': group.name,
          'arbiter-group-type': group.type,
        },
      }))),
    ],
    dataObjects: groups.map(group => ({
      id: group.id,
      name: group.name,
      type: group.type,
      groupRule: 'manual_file_selection',
      targetPrefix: `${processedPrefix}${projectId}/${group.name}/`,
      sourceFiles: group.files.map(file => ({
        name: file.name,
        key: file.key,
        size: file.size,
        lastModified: file.last_modified,
      })),
      associations: (group.associations || []).map(association => ({
        id: association.id,
        label: association.label,
        fileKeys: association.fileKeys,
      })),
      summaryFile: group.files.filter(file => /\.csv$/i.test(file.name || '')).length >= 2 ? makeSummaryName(group.name, group.type) : null,
      summaryStatus: summaries[group.id] ? 'generated' : 'not_generated',
      recordCount: summaries[group.id]?.rows?.length || 0,
    })),
  }
}

function mergeMetadataLedger(ledger, metadata) {
  const previousProject = ledger.projects?.[metadata.projectId] || {}
  const revision = {
    createdAt: metadata.createdAt,
    metadataKey: metadata.metadataKey,
    groupCount: metadata.dataObjects.length,
    fileCount: metadata.dataObjects.reduce((sum, object) => sum + object.sourceFiles.length, 0),
  }

  return {
    version: '0.1',
    updatedAt: metadata.createdAt,
    projects: {
      ...(ledger.projects || {}),
      [metadata.projectId]: {
        ...previousProject,
        ...metadata,
        revisions: [...(previousProject.revisions || []), revision].slice(-10),
      },
    },
  }
}

function persistGroups(groups) {
  localStorage.setItem(GROUPS_STORAGE_KEY, JSON.stringify(groups))
}

function Stat({ label, value }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">{label}</p>
      <p className="mt-1 text-2xl font-bold text-slate-900">{value}</p>
    </div>
  )
}

function FileRow({ file, action }) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2">
      <div className="min-w-0">
        <p className="truncate text-sm font-medium text-slate-800" title={file.name}>{file.name}</p>
        <p className="truncate font-mono text-[10px] text-slate-400" title={file.key}>{file.key}</p>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <span className="text-xs text-slate-500">{formatBytes(file.size)}</span>
        {action}
      </div>
    </div>
  )
}

function AvailableFileRow({ file, onAdd }) {
  return (
    <div className="flex items-center gap-3 rounded-lg border border-slate-200 bg-white px-3 py-2">
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-slate-800" title={file.name}>{file.name}</p>
        <p className="truncate font-mono text-[10px] text-slate-400" title={file.key}>{file.key}</p>
      </div>
      <span className="shrink-0 text-xs text-slate-500">{formatBytes(file.size)}</span>
      <button
        type="button"
        onClick={onAdd}
        className="inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-indigo-600 px-2.5 py-1.5 text-xs font-semibold text-white hover:bg-indigo-700"
      >
        <Plus size={13} /> Add
      </button>
    </div>
  )
}

function SelectedDraftFileRow({ file, onRemove }) {
  return (
    <div className="flex items-center gap-3 rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-2">
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-slate-800" title={file.name}>{file.name}</p>
        <p className="truncate font-mono text-[10px] text-indigo-400" title={file.key}>{file.key}</p>
      </div>
      <span className="shrink-0 text-xs text-slate-500">{formatBytes(file.size)}</span>
      <button
        type="button"
        onClick={onRemove}
        className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-red-200 bg-white px-2.5 py-1.5 text-xs font-semibold text-red-700 hover:bg-red-50"
      >
        <Trash2 size={13} /> Remove
      </button>
    </div>
  )
}

function AssociationBuilder({ group, onSaveAssociation, onDeleteAssociation, onDisassociateFile }) {
  const csvFiles = group.files.filter(file => /\.csv$/i.test(file.name || ''))
  const [label, setLabel] = useState('A')
  const [selectedKeys, setSelectedKeys] = useState([])
  const selectedKeySet = useMemo(() => new Set(selectedKeys), [selectedKeys])
  const associatedByKey = useMemo(() => {
    const entries = []
    ;(group.associations || []).forEach(association => {
      association.fileKeys.forEach(key => entries.push([key, association]))
    })
    return new Map(entries)
  }, [group.associations])
  const selectedCount = selectedKeys.length

  useEffect(() => {
    setLabel('A')
    setSelectedKeys([])
  }, [group.id])

  function toggleFile(file) {
    const key = fileKey(file)
    setSelectedKeys(prev => (
      prev.includes(key) ? prev.filter(item => item !== key) : [...prev, key]
    ))
  }

  function saveAssociation() {
    if (selectedKeys.length < 2) return
    onSaveAssociation(group.id, {
      id: `${label}-${Date.now()}`,
      label,
      fileKeys: selectedKeys,
      createdAt: new Date().toISOString(),
    })
    setSelectedKeys([])
  }

  return (
    <div className="mt-4 rounded-lg border border-slate-200 bg-slate-50 p-3">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">CSV associations</p>
          <p className="mt-1 text-xs text-slate-500">Choose A-E, then select two or more CSV files in this group.</p>
        </div>
        <div className="flex items-end gap-2">
          <label className="text-[10px] font-semibold uppercase tracking-wider text-slate-400" htmlFor={`association-${group.id}`}>
            Association
            <select
              id={`association-${group.id}`}
              value={label}
              onChange={(event) => setLabel(event.target.value)}
              className="mt-1 block rounded-lg border border-slate-200 bg-white px-2.5 py-2 text-xs font-semibold text-slate-800 outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
            >
              {ASSOCIATION_OPTIONS.map(option => <option key={option} value={option}>{option}</option>)}
            </select>
          </label>
          <button
            type="button"
            onClick={saveAssociation}
            disabled={selectedCount < 2}
            className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-2 text-xs font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-slate-200 disabled:text-slate-500"
          >
            <Save size={13} /> Save association
          </button>
        </div>
      </div>

      <div className="mt-3 grid gap-2 md:grid-cols-2">
        {csvFiles.map(file => {
          const key = fileKey(file)
          const association = associatedByKey.get(key)
          if (association) {
            return (
              <div key={key} className="flex items-center gap-2 rounded-lg border border-slate-200 bg-slate-100 px-3 py-2 text-slate-500">
                <input
                  type="checkbox"
                  checked
                  disabled
                  className="h-4 w-4 rounded border-slate-300 text-slate-400"
                />
                <span className="rounded-md border border-slate-300 bg-white px-2 py-0.5 text-[10px] font-bold text-slate-500">
                  {association.label}
                </span>
                <span className="min-w-0 flex-1 truncate text-xs font-medium" title={file.name}>{file.name}</span>
                <button
                  type="button"
                  onClick={() => onDisassociateFile(group.id, association.id, key)}
                  className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-semibold text-slate-600 hover:bg-slate-50"
                >
                  <XCircle size={13} /> Disassociate
                </button>
              </div>
            )
          }
          return (
            <label key={key} className="flex cursor-pointer items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2">
              <input
                type="checkbox"
                checked={selectedKeySet.has(key)}
                onChange={() => toggleFile(file)}
                className="h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
              />
              <span className="min-w-0 flex-1 truncate text-xs font-medium text-slate-700" title={file.name}>{file.name}</span>
            </label>
          )
        })}
        {!csvFiles.length && (
          <p className="rounded-lg border border-slate-200 bg-white p-3 text-xs text-slate-500">No CSV files in this group.</p>
        )}
      </div>

      {(group.associations || []).length > 0 && (
        <div className="mt-3 space-y-2">
          {(group.associations || []).map(association => (
            <div key={association.id} className="flex items-start justify-between gap-3 rounded-lg border border-indigo-200 bg-white p-3">
              <div className="min-w-0">
                <p className="text-xs font-bold text-indigo-700">Association {association.label}</p>
                <p className="mt-1 text-xs text-slate-500">
                  {association.fileKeys
                    .map(key => group.files.find(file => fileKey(file) === key)?.name || key)
                    .join(', ')}
                </p>
              </div>
              <button
                type="button"
                onClick={() => onDeleteAssociation(group.id, association.id)}
                className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-red-200 bg-white px-2.5 py-1.5 text-xs font-semibold text-red-700 hover:bg-red-50"
              >
                <Trash2 size={13} /> Delete
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function GroupCard({
  group, projectId, processedPrefix, summary, onGenerateSummary, onEdit, onDelete,
  onSaveAssociation, onDeleteAssociation, onDisassociateFile, collapsed, onToggleCollapse,
}) {
  const rows = summary?.rows || []
  const csvFiles = group.files.filter(file => /\.csv$/i.test(file.name || ''))
  const canSummarize = csvFiles.length >= 2
  const summaryFile = makeSummaryName(group.name, group.type)
  const targetPrefix = `${processedPrefix}${projectId}/${group.name}/`

  return (
    <article className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <FolderTree size={15} className="text-indigo-600" />
            <h2 className="text-sm font-bold text-slate-900">{group.name}</h2>
          </div>
          <p className="mt-1 text-xs text-slate-500">{optionForType(group.type).label} · {group.files.length} files · {csvFiles.length} CSV</p>
        </div>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => onToggleCollapse(group.id)}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-50"
          >
            {collapsed ? <ChevronRight size={13} /> : <ChevronDown size={13} />}
            {collapsed ? 'Expand' : 'Collapse'}
          </button>
          <button
            type="button"
            onClick={() => onEdit(group)}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-50"
          >
            <Edit3 size={13} /> Edit
          </button>
          <button
            type="button"
            onClick={() => onDelete(group.id)}
            className="inline-flex items-center gap-1.5 rounded-lg border border-red-200 bg-white px-2.5 py-1.5 text-xs font-semibold text-red-700 hover:bg-red-50"
          >
            <Trash2 size={13} /> Delete
          </button>
        </div>
      </div>

      {collapsed ? (
        <div className="mt-3 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-500">
          {group.files.length} files hidden · {csvFiles.length} CSV · {(group.associations || []).length} associations
        </div>
      ) : (
        <>
          <div className="mt-4 grid gap-3 lg:grid-cols-[1fr_1fr]">
            <div>
              <p className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-slate-400">Files in group</p>
              <div className="space-y-2">
                {group.files.map(file => <FileRow key={fileKey(file)} file={file} />)}
              </div>
            </div>
            <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
              <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">Target processed structure</p>
              <p className="mt-2 break-all font-mono text-xs text-slate-700">{targetPrefix}</p>
              <div className="mt-3 rounded-lg border border-slate-200 bg-white p-3">
                <p className="text-xs font-semibold text-slate-800">{summaryFile}</p>
                <p className="mt-1 text-xs text-slate-500">
                  {canSummarize ? 'Stage two can summarize this group because it has two or more CSV files.' : 'Add at least two CSV files before generating a spreadsheet summary.'}
                </p>
              </div>
              {summary && (
                <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
                  <div className="rounded-lg border border-slate-200 bg-white p-2">
                    <p className="text-slate-400">Rows</p>
                    <p className="font-semibold text-slate-900">{rows.length}</p>
                  </div>
                  <div className="rounded-lg border border-slate-200 bg-white p-2">
                    <p className="text-slate-400">Amount total</p>
                    <p className="font-semibold text-slate-900">${amountTotal(rows).toLocaleString(undefined, { maximumFractionDigits: 2 })}</p>
                  </div>
                </div>
              )}
            </div>
          </div>

          <div className="mt-4 flex flex-wrap gap-2">
            <button
              type="button"
              disabled={!canSummarize}
              onClick={() => onGenerateSummary(group)}
              className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-2 text-xs font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-slate-200 disabled:text-slate-500"
            >
              <Wand2 size={13} /> Generate summary
            </button>
            <button
              type="button"
              disabled={!summary}
              onClick={() => summary && downloadText(summaryFile, toCsv(summary.rows), 'text/csv')}
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-300"
            >
              <Download size={13} /> Download summary
            </button>
          </div>
          {canSummarize && (
            <AssociationBuilder
              group={group}
              onSaveAssociation={onSaveAssociation}
              onDeleteAssociation={onDeleteAssociation}
              onDisassociateFile={onDisassociateFile}
            />
          )}
        </>
      )}
    </article>
  )
}

export default function DataGrouping() {
  const [projectName, setProjectName] = useState('Vendor Audit June 2026')
  const [files, setFiles] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [summaries, setSummaries] = useState({})
  const [metadata, setMetadata] = useState(null)
  const [metadataLedger, setMetadataLedger] = useState({ version: '0.1', projects: {} })
  const [metadataWrite, setMetadataWrite] = useState(null)
  const [groups, setGroups] = useState([])
  const [groupsLoaded, setGroupsLoaded] = useState(false)
  const [editingGroupId, setEditingGroupId] = useState(null)
  const [draftName, setDraftName] = useState('')
  const [draftType, setDraftType] = useState('accounts_receivable_invoices')
  const [draftKeys, setDraftKeys] = useState([])
  const [collapsedGroupIds, setCollapsedGroupIds] = useState([])

  const projectId = slugify(projectName)
  const processedPrefix = 'processed/'
  const fileMap = useMemo(() => new Map(files.map(file => [fileKey(file), file])), [files])
  const hydratedGroups = useMemo(() => (
    groups.map(group => ({
      ...group,
      fileKeys: groupFileKeys(group),
      files: groupFileKeys(group)
        .map(key => fileMap.get(key) || (group.files || []).find(file => fileKey(file) === key))
        .filter(Boolean),
    }))
  ), [groups, fileMap])
  const assignedKeySet = useMemo(
    () => new Set(hydratedGroups.flatMap(group => group.fileKeys)),
    [hydratedGroups],
  )
  const draftKeySet = useMemo(() => new Set(draftKeys), [draftKeys])
  const editingGroup = hydratedGroups.find(group => group.id === editingGroupId)
  const availableFiles = useMemo(() => (
    files.filter(file => {
      const key = fileKey(file)
      return !assignedKeySet.has(key) || editingGroup?.files.some(groupFile => fileKey(groupFile) === key)
    })
  ), [files, assignedKeySet, editingGroup])
  const addableFiles = useMemo(
    () => availableFiles.filter(file => !draftKeySet.has(fileKey(file))),
    [availableFiles, draftKeySet],
  )
  const ungroupedFiles = useMemo(() => files.filter(file => !assignedKeySet.has(fileKey(file))), [files, assignedKeySet])
  const selectedDraftFiles = useMemo(() => files.filter(file => draftKeySet.has(fileKey(file))), [files, draftKeySet])
  const csvCount = files.filter(file => /\.csv$/i.test(file.name || '')).length
  const csvDraftCount = selectedDraftFiles.filter(file => /\.csv$/i.test(file.name || '')).length

  async function loadFiles() {
    setLoading(true)
    setError('')
    try {
      const data = await listUploadedFiles('processed')
      setFiles(data.files || [])
    } catch (err) {
      setError(err.message || 'Unable to list processed files')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadFiles()
    try {
      const savedGroups = JSON.parse(localStorage.getItem(GROUPS_STORAGE_KEY) || '[]')
      if (Array.isArray(savedGroups)) setGroups(savedGroups)
      const savedMetadata = JSON.parse(localStorage.getItem(GROUPING_STORAGE_KEY) || 'null')
      if (savedMetadata) setMetadata(savedMetadata)
      const savedLedger = JSON.parse(localStorage.getItem(METADATA_LEDGER_STORAGE_KEY) || 'null')
      if (savedLedger?.projects) setMetadataLedger(savedLedger)
    } catch {
      /* ignore bad local grouping state */
    } finally {
      setGroupsLoaded(true)
    }
  }, [])

  useEffect(() => {
    if (groupsLoaded) persistGroups(groups)
  }, [groups, groupsLoaded])

  useEffect(() => {
    const validGroupIds = new Set(hydratedGroups.map(group => group.id))
    setSummaries(prev => Object.fromEntries(Object.entries(prev).filter(([groupId]) => validGroupIds.has(groupId))))
  }, [hydratedGroups])

  function nextGroupType(groupsToCheck = hydratedGroups) {
    const usedTypes = new Set(groupsToCheck.map(group => group.type))
    return GROUP_TYPE_OPTIONS.find(option => !usedTypes.has(option.value))?.value || 'spreadsheet_collection'
  }

  function resetDraft(type = draftType, name = '') {
    setEditingGroupId(null)
    setDraftType(type)
    setDraftName(name)
    setDraftKeys([])
  }

  function startNewGroup(type = nextGroupType()) {
    setMetadata(null)
    resetDraft(type)
  }

  function changeDraftType(type) {
    setDraftType(type)
  }

  function addDraftFile(file) {
    const key = fileKey(file)
    setMetadata(null)
    setDraftKeys(prev => (prev.includes(key) ? prev : [...prev, key]))
  }

  function removeDraftFile(file) {
    const key = fileKey(file)
    setMetadata(null)
    setDraftKeys(prev => prev.filter(item => item !== key))
  }

  function saveGroup() {
    if (!draftName.trim() || !selectedDraftFiles.length) return
    const savedFileKeys = selectedDraftFiles.map(file => fileKey(file))
    const nextGroup = {
      id: editingGroupId || `${slugify(draftName)}-${Date.now()}`,
      name: draftName.trim().replace(/\s+/g, '_'),
      type: draftType,
      fileKeys: savedFileKeys,
      files: selectedDraftFiles,
      associations: (editingGroup?.associations || [])
        .map(association => ({
          ...association,
          fileKeys: association.fileKeys.filter(key => savedFileKeys.includes(key)),
        }))
        .filter(association => association.fileKeys.length >= 2),
      updatedAt: new Date().toISOString(),
    }
    setMetadata(null)
    const previewGroups = [...hydratedGroups.filter(group => group.id !== nextGroup.id), nextGroup]
    setGroups(prev => {
      const others = prev.filter(group => group.id !== nextGroup.id)
      const nextGroups = [...others, nextGroup]
      persistGroups(nextGroups)
      return nextGroups
    })
    startNewGroup(nextGroupType(previewGroups))
  }

  function editGroup(group) {
    setCollapsedGroupIds(prev => prev.filter(groupId => groupId !== group.id))
    setEditingGroupId(group.id)
    setDraftName(group.name)
    setDraftType(group.type)
    setDraftKeys(group.files.map(file => fileKey(file)))
  }

  function toggleGroupCollapse(groupId) {
    setCollapsedGroupIds(prev => (
      prev.includes(groupId)
        ? prev.filter(id => id !== groupId)
        : [...prev, groupId]
    ))
  }

  function deleteGroup(groupId) {
    setMetadata(null)
    setGroups(prev => {
      const nextGroups = prev.filter(group => group.id !== groupId)
      persistGroups(nextGroups)
      return nextGroups
    })
    setSummaries(prev => {
      const next = { ...prev }
      delete next[groupId]
      return next
    })
    if (editingGroupId === groupId) resetDraft()
  }

  function saveAssociation(groupId, association) {
    setMetadata(null)
    setGroups(prev => {
      const nextGroups = prev.map(group => {
        if (group.id !== groupId) return group
        const associations = [
          ...(group.associations || [])
            .filter(item => item.label !== association.label)
            .map(item => ({
              ...item,
              fileKeys: item.fileKeys.filter(key => !association.fileKeys.includes(key)),
            }))
            .filter(item => item.fileKeys.length >= 2),
          association,
        ]
        return { ...group, associations, updatedAt: new Date().toISOString() }
      })
      persistGroups(nextGroups)
      return nextGroups
    })
  }

  function deleteAssociation(groupId, associationId) {
    setMetadata(null)
    setGroups(prev => {
      const nextGroups = prev.map(group => {
        if (group.id !== groupId) return group
        return {
          ...group,
          associations: (group.associations || []).filter(item => item.id !== associationId),
          updatedAt: new Date().toISOString(),
        }
      })
      persistGroups(nextGroups)
      return nextGroups
    })
  }

  function disassociateFile(groupId, associationId, keyToRemove) {
    setMetadata(null)
    setGroups(prev => {
      const nextGroups = prev.map(group => {
        if (group.id !== groupId) return group
        return {
          ...group,
          associations: (group.associations || [])
            .map(item => (
              item.id === associationId
                ? { ...item, fileKeys: item.fileKeys.filter(key => key !== keyToRemove) }
                : item
            ))
            .filter(item => item.fileKeys.length >= 2),
          updatedAt: new Date().toISOString(),
        }
      })
      persistGroups(nextGroups)
      return nextGroups
    })
  }

  function generateSummary(group) {
    setMetadata(null)
    setSummaries(prev => ({
      ...prev,
      [group.id]: {
        generatedAt: new Date().toISOString(),
        rows: rowsForGroup(group),
      },
    }))
  }

  function createMetadata() {
    const next = buildMetadata({ projectName, projectId, processedPrefix, groups: hydratedGroups, summaries })
    const nextLedger = mergeMetadataLedger(metadataLedger, next)
    setMetadata(next)
    setMetadataLedger(nextLedger)
    setMetadataWrite({
      key: next.metadataKey,
      at: next.createdAt,
      groupCount: next.dataObjects.length,
      fileMetadataUpdates: next.simulatedS3Writes.filter(write => write.operation === 'copy_object_with_metadata').length,
    })
    localStorage.setItem(GROUPING_STORAGE_KEY, JSON.stringify(next, null, 2))
    localStorage.setItem(METADATA_LEDGER_STORAGE_KEY, JSON.stringify(nextLedger, null, 2))
  }

  function downloadMetadata() {
    const current = metadata || buildMetadata({ projectName, projectId, processedPrefix, groups: hydratedGroups, summaries })
    downloadText(`${projectId}-data-grouping-metadata.json`, JSON.stringify(current, null, 2), 'application/json')
  }

  return (
    <div className="page-container space-y-6 p-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-500">Processed data intelligence</p>
          <h1 className="mt-1 text-lg font-bold tracking-tight text-slate-900">Data Grouping</h1>
          <p className="mt-1 max-w-3xl text-xs text-slate-500">
            Start from files that already cleared the Data Pipeline into /processed. Create editable project data groups first; later, groups with two or more CSV files can produce summary spreadsheets.
          </p>
        </div>
        <button
          type="button"
          onClick={loadFiles}
          disabled={loading}
          className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-700 shadow-sm hover:bg-slate-50 disabled:opacity-60"
        >
          {loading ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
          Refresh processed files
        </button>
      </div>

      <div className="grid gap-3 md:grid-cols-4">
        <Stat label="Processed files" value={files.length} />
        <Stat label="Available files" value={ungroupedFiles.length} />
        <Stat label="Saved groups" value={hydratedGroups.length} />
        <Stat label="Summaries" value={Object.keys(summaries).length} />
      </div>

      {USE_MOCK && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800">
          This local server is running in mock mode, so it will not show newly uploaded S3 files. Use the signed-in live app to see real /processed contents.
        </div>
      )}

      {error && (
        <div className="flex items-center gap-2 rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700">
          <AlertTriangle size={16} /> {error}
        </div>
      )}

      <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(320px,0.7fr)]">
          <div>
            <label className="text-[10px] font-semibold uppercase tracking-wider text-slate-400" htmlFor="project-name">Project</label>
            <input
              id="project-name"
              value={projectName}
              onChange={(event) => {
                setMetadata(null)
                setProjectName(event.target.value)
              }}
              className="mt-2 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm font-medium text-slate-900 outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
            />
            <p className="mt-2 text-xs text-slate-500">
              Project ID: <span className="font-mono text-slate-700">{projectId}</span>
            </p>
          </div>
          <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
            <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">Project S3 plan</p>
            <div className="mt-2 space-y-1 font-mono text-xs text-slate-700">
              <p>{processedPrefix}{projectId}/</p>
              {hydratedGroups.length ? hydratedGroups.map(group => (
                <p key={group.id}>{processedPrefix}{projectId}/{group.name}/</p>
              )) : (
                <>
                  <p>{processedPrefix}{projectId}/AR_Invoices/</p>
                  <p>{processedPrefix}{projectId}/AP_Invoices/</p>
                </>
              )}
              <p>{processedPrefix}{projectId}/metadata/project.json</p>
            </div>
          </div>
        </div>
      </section>

      <section className="grid gap-4 xl:grid-cols-[minmax(360px,0.9fr)_minmax(0,1.1fr)]">
        <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 className="text-sm font-bold text-slate-900">{editingGroupId ? 'Edit group' : 'Create group'}</h2>
              <p className="mt-1 text-xs text-slate-500">Click New group, name it, choose a type, then add files not already in another group.</p>
            </div>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => startNewGroup()}
                className="inline-flex items-center gap-1.5 rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-2 text-xs font-semibold text-indigo-700 hover:bg-indigo-100"
              >
                <Plus size={13} /> New group
              </button>
              {editingGroupId && (
              <button
                type="button"
                onClick={() => resetDraft()}
                className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50"
              >
                <XCircle size={13} /> Cancel edit
              </button>
              )}
            </div>
          </div>

          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            <div>
              <label className="text-[10px] font-semibold uppercase tracking-wider text-slate-400" htmlFor="group-name">Group name</label>
              <input
                id="group-name"
                value={draftName}
                onChange={(event) => setDraftName(event.target.value)}
                className="mt-2 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm font-medium text-slate-900 outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
              />
            </div>
            <div>
              <label className="text-[10px] font-semibold uppercase tracking-wider text-slate-400" htmlFor="group-type">Group type</label>
              <select
                id="group-type"
                value={draftType}
                onChange={(event) => changeDraftType(event.target.value)}
                className="mt-2 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm font-medium text-slate-900 outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
              >
                {GROUP_TYPE_OPTIONS.map(option => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </div>
          </div>

          <div className="mt-4 flex items-center justify-between gap-3">
            <p className="text-xs text-slate-500">{draftKeys.length} selected · {csvDraftCount} CSV</p>
            <button
              type="button"
              onClick={saveGroup}
              disabled={!draftName.trim() || !draftKeys.length}
              className="inline-flex items-center gap-1.5 rounded-lg bg-slate-900 px-3 py-2 text-xs font-semibold text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-200 disabled:text-slate-500"
            >
              {editingGroupId ? <Save size={13} /> : <Plus size={13} />}
              {editingGroupId ? 'Save group' : 'Create group'}
            </button>
          </div>

          <div className="mt-4 grid gap-4">
            <div>
              <p className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-slate-400">Files in this group</p>
              <div className="space-y-2">
                {selectedDraftFiles.map(file => (
                  <SelectedDraftFileRow
                    key={fileKey(file)}
                    file={file}
                    onRemove={() => removeDraftFile(file)}
                  />
                ))}
                {!selectedDraftFiles.length && (
                  <p className="rounded-lg border border-dashed border-slate-300 bg-slate-50 p-3 text-xs text-slate-500">No files selected for this group yet.</p>
                )}
              </div>
            </div>

            {editingGroupId && (
              <p className="rounded-lg border border-indigo-200 bg-indigo-50 p-3 text-xs text-indigo-700">
                Editing this group releases its current files only inside this editor. Save group to keep changes, or cancel edit to leave the saved group unchanged.
              </p>
            )}
          </div>
        </div>

        <div className="space-y-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h2 className="text-sm font-bold text-slate-900">Saved project groups</h2>
              <p className="text-xs text-slate-500">Each file can belong to one group in this version.</p>
            </div>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                onClick={createMetadata}
                disabled={!hydratedGroups.length}
                className="inline-flex items-center gap-1.5 rounded-lg bg-slate-900 px-3 py-2 text-xs font-semibold text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-200 disabled:text-slate-500"
              >
                <Database size={13} /> Create metadata
              </button>
            </div>
          </div>
          {hydratedGroups.map(group => (
            <GroupCard
              key={group.id}
              group={group}
              projectId={projectId}
              processedPrefix={processedPrefix}
              summary={summaries[group.id]}
              onGenerateSummary={generateSummary}
              onEdit={editGroup}
              onDelete={deleteGroup}
              onSaveAssociation={saveAssociation}
              onDeleteAssociation={deleteAssociation}
              onDisassociateFile={disassociateFile}
              collapsed={collapsedGroupIds.includes(group.id)}
              onToggleCollapse={toggleGroupCollapse}
            />
          ))}
          {!hydratedGroups.length && (
            <div className="rounded-xl border border-dashed border-slate-300 bg-white p-8 text-center">
              <FileSpreadsheet size={24} className="mx-auto text-slate-400" />
              <p className="mt-2 text-sm font-semibold text-slate-800">No project groups yet.</p>
              <p className="mt-1 text-xs text-slate-500">Select processed files and create the first logical data group.</p>
            </div>
          )}
        </div>
      </section>

      <section className="grid gap-4 lg:grid-cols-2">
        <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <h2 className="text-sm font-bold text-slate-900">Add ungrouped files to current group</h2>
          <p className="mt-1 text-xs text-slate-500">Only files not saved in another group appear here. Click Add to move a file into the current create/edit form.</p>
          <div className="mt-3 max-h-[360px] space-y-2 overflow-auto pr-1">
            {addableFiles.length ? addableFiles.map(file => (
              <AvailableFileRow
                key={fileKey(file)}
                file={file}
                onAdd={() => addDraftFile(file)}
              />
            )) : (
              <p className="rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs text-slate-500">No files available to add. Files already saved in another group are hidden.</p>
            )}
          </div>
        </div>

        <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h2 className="text-sm font-bold text-slate-900">Metadata preview</h2>
              <p className="mt-1 text-xs text-slate-500">Stored locally for now; ready to become /processed project metadata.</p>
            </div>
            <button
              type="button"
              onClick={downloadMetadata}
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50"
            >
              <Download size={13} /> JSON
            </button>
          </div>
          {metadata ? (
            <div className="mt-3 rounded-lg border border-emerald-200 bg-emerald-50 p-3">
              <p className="flex items-center gap-1.5 text-xs font-semibold text-emerald-800">
                <CheckCircle size={13} /> Metadata created for {metadata.dataObjects.length} data objects
              </p>
              {metadataWrite && (
                <div className="mt-3 rounded-lg border border-emerald-200 bg-white p-3 text-xs text-emerald-800">
                  <p className="font-semibold">Simulated S3 metadata write</p>
                  <p className="mt-1 break-all font-mono text-[10px]">{metadataWrite.key}</p>
                  <p className="mt-1">{metadataWrite.groupCount} groups stored · {metadataWrite.fileMetadataUpdates} file metadata updates appended to local ledger</p>
                </div>
              )}
              <pre className="mt-3 max-h-72 overflow-auto whitespace-pre-wrap rounded-lg bg-white p-3 font-mono text-[10px] text-slate-700">
                {JSON.stringify(metadata, null, 2)}
              </pre>
            </div>
          ) : (
            <p className="mt-3 rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs text-slate-500">Create metadata to preview the project grouping document.</p>
          )}
        </div>
      </section>
    </div>
  )
}
