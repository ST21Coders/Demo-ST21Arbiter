import { useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle, CheckCircle, Database, Download, FileSpreadsheet, FolderTree,
  Loader2, RefreshCw, Wand2, XCircle,
} from 'lucide-react'
import { listUploadedFiles } from '../hooks/useApi'

const GROUPING_STORAGE_KEY = 'arbiter.dataGrouping.projectMetadata'
const PROJECT_SELECTION_STORAGE_KEY = 'arbiter.dataGrouping.projectSelection'

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

const GROUP_RULES = [
  {
    id: 'AR_Invoices',
    label: 'AR_Invoices',
    type: 'accounts_receivable_invoices',
    matchPattern: 'AR_Invoice*',
    summaryFile: 'SUM_AR_Invoices.csv',
    test: (name) => /^AR_Invoice/i.test(name),
  },
  {
    id: 'AP_Invoices',
    label: 'AP_Invoices',
    type: 'accounts_payable_invoices',
    matchPattern: 'AP_Invoice*',
    summaryFile: 'SUM_AP_Invoices.csv',
    test: (name) => /^AP_Invoice/i.test(name),
  },
]

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

function detectGroups(files) {
  const csvFiles = files.filter(file => /\.csv$/i.test(file.name || ''))
  const groupedKeys = new Set()
  const groups = GROUP_RULES.map(rule => {
    const matches = csvFiles.filter(file => rule.test(file.name || ''))
    matches.forEach(file => groupedKeys.add(file.key || file.name))
    return { ...rule, files: matches }
  }).filter(group => group.files.length > 0)

  const ungrouped = files.filter(file => !groupedKeys.has(file.key || file.name))
  return { groups, ungrouped }
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

function buildMetadata({ projectName, projectId, processedPrefix, projectFiles, groups, summaries }) {
  const createdAt = new Date().toISOString()
  return {
    projectId,
    displayName: projectName,
    createdAt,
    sourcePrefix: processedPrefix,
    processedPrefix,
    groupVersion: '0.1',
    projectFiles: projectFiles.map(file => ({
      name: file.name,
      key: file.key,
      size: file.size,
      lastModified: file.last_modified,
    })),
    dataObjects: groups.map(group => ({
      id: group.id,
      type: group.type,
      groupRule: 'filename_prefix',
      matchPattern: group.matchPattern,
      targetPrefix: `${processedPrefix}${projectId}/${group.id}/`,
      sourceFiles: group.files.map(file => ({
        name: file.name,
        key: file.key,
        size: file.size,
        lastModified: file.last_modified,
      })),
      summaryFile: group.files.length >= 2 ? group.summaryFile : null,
      summaryStatus: summaries[group.id] ? 'generated' : group.files.length >= 2 ? 'ready' : 'not_required',
      recordCount: summaries[group.id]?.rows?.length || 0,
    })),
  }
}

function Stat({ label, value }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">{label}</p>
      <p className="mt-1 text-2xl font-bold text-slate-900">{value}</p>
    </div>
  )
}

function FileRow({ file }) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2">
      <div className="min-w-0">
        <p className="truncate text-sm font-medium text-slate-800" title={file.name}>{file.name}</p>
        <p className="truncate font-mono text-[10px] text-slate-400" title={file.key}>{file.key}</p>
      </div>
      <span className="shrink-0 text-xs text-slate-500">{formatBytes(file.size)}</span>
    </div>
  )
}

function SelectableFileRow({ file, checked, onChange }) {
  const checkboxId = `processed-file-${file.key || file.name}`.replace(/[^a-zA-Z0-9_-]/g, '-')

  return (
    <label
      htmlFor={checkboxId}
      className="flex cursor-pointer items-center gap-3 rounded-lg border border-slate-200 bg-white px-3 py-2 hover:bg-slate-50"
    >
      <input
        id={checkboxId}
        type="checkbox"
        checked={checked}
        onChange={onChange}
        className="h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
      />
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-slate-800" title={file.name}>{file.name}</p>
        <p className="truncate font-mono text-[10px] text-slate-400" title={file.key}>{file.key}</p>
      </div>
      <span className="shrink-0 text-xs text-slate-500">{formatBytes(file.size)}</span>
    </label>
  )
}

function GroupCard({ group, projectId, processedPrefix, summary, onGenerateSummary }) {
  const rows = summary?.rows || []
  const canSummarize = group.files.length >= 2 && group.files.every(file => /\.csv$/i.test(file.name || ''))
  const targetPrefix = `${processedPrefix}${projectId}/${group.id}/`

  return (
    <article className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <FolderTree size={15} className="text-indigo-600" />
            <h2 className="text-sm font-bold text-slate-900">{group.label}</h2>
          </div>
          <p className="mt-1 text-xs text-slate-500">{group.matchPattern} · {group.type}</p>
        </div>
        <span className="rounded-full border border-indigo-200 bg-indigo-50 px-2.5 py-1 text-[10px] font-bold uppercase tracking-wider text-indigo-700">
          {group.files.length} files
        </span>
      </div>

      <div className="mt-4 grid gap-3 lg:grid-cols-[1fr_1fr]">
        <div>
          <p className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-slate-400">Source files</p>
          <div className="space-y-2">
            {group.files.map(file => <FileRow key={file.key || file.name} file={file} />)}
          </div>
        </div>
        <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
          <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">Target processed structure</p>
          <p className="mt-2 break-all font-mono text-xs text-slate-700">{targetPrefix}</p>
          <div className="mt-3 rounded-lg border border-slate-200 bg-white p-3">
            <p className="text-xs font-semibold text-slate-800">{group.summaryFile}</p>
            <p className="mt-1 text-xs text-slate-500">
              {canSummarize ? 'Summary CSV is generated when the grouped spreadsheet set is confirmed.' : 'Summary not required until the group has two or more CSV files.'}
            </p>
          </div>
          {summary && (
            <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
              <div className="rounded-lg bg-white p-2 border border-slate-200">
                <p className="text-slate-400">Rows</p>
                <p className="font-semibold text-slate-900">{rows.length}</p>
              </div>
              <div className="rounded-lg bg-white p-2 border border-slate-200">
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
          onClick={() => summary && downloadText(group.summaryFile, toCsv(summary.rows), 'text/csv')}
          className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-300"
        >
          <Download size={13} /> Download summary
        </button>
      </div>
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
  const [selectedKeys, setSelectedKeys] = useState([])

  const projectId = slugify(projectName)
  const processedPrefix = 'processed/'
  const selectedKeySet = useMemo(() => new Set(selectedKeys), [selectedKeys])
  const projectFiles = useMemo(
    () => files.filter(file => selectedKeySet.has(file.key || file.name)),
    [files, selectedKeySet],
  )
  const { groups, ungrouped } = useMemo(() => detectGroups(projectFiles), [projectFiles])
  const csvCount = files.filter(file => /\.csv$/i.test(file.name || '')).length

  async function loadFiles() {
    setLoading(true)
    setError('')
    try {
      const data = await listUploadedFiles('processed')
      const nextFiles = data.files || []
      setFiles(nextFiles)
      setSelectedKeys(prev => {
        const availableKeys = nextFiles.map(file => file.key || file.name)
        const availableSet = new Set(availableKeys)
        const retained = prev.filter(key => availableSet.has(key))
        if (retained.length) return retained
        return availableKeys
      })
    } catch (err) {
      setError(err.message || 'Unable to list processed files')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadFiles()
    try {
      const saved = JSON.parse(localStorage.getItem(GROUPING_STORAGE_KEY) || 'null')
      if (saved) setMetadata(saved)
      const savedSelection = JSON.parse(localStorage.getItem(PROJECT_SELECTION_STORAGE_KEY) || 'null')
      if (Array.isArray(savedSelection)) setSelectedKeys(savedSelection)
    } catch {
      /* ignore bad local metadata */
    }
  }, [])

  useEffect(() => {
    localStorage.setItem(PROJECT_SELECTION_STORAGE_KEY, JSON.stringify(selectedKeys))
  }, [selectedKeys])

  useEffect(() => {
    const validGroupIds = new Set(groups.map(group => group.id))
    setSummaries(prev => Object.fromEntries(Object.entries(prev).filter(([groupId]) => validGroupIds.has(groupId))))
  }, [groups])

  function toggleProjectFile(file) {
    const key = file.key || file.name
    setMetadata(null)
    setSelectedKeys(prev => (
      prev.includes(key) ? prev.filter(item => item !== key) : [...prev, key]
    ))
  }

  function selectAllFiles() {
    setMetadata(null)
    setSelectedKeys(files.map(file => file.key || file.name))
  }

  function clearSelectedFiles() {
    setMetadata(null)
    setSelectedKeys([])
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
    const next = buildMetadata({ projectName, projectId, processedPrefix, projectFiles, groups, summaries })
    setMetadata(next)
    localStorage.setItem(GROUPING_STORAGE_KEY, JSON.stringify(next, null, 2))
  }

  function downloadMetadata() {
    const current = metadata || buildMetadata({ projectName, projectId, processedPrefix, projectFiles, groups, summaries })
    downloadText(`${projectId}-data-grouping-metadata.json`, JSON.stringify(current, null, 2), 'application/json')
  }

  return (
    <div className="page-container space-y-6 p-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-500">Processed data intelligence</p>
          <h1 className="mt-1 text-lg font-bold tracking-tight text-slate-900">Data Grouping</h1>
          <p className="mt-1 max-w-3xl text-xs text-slate-500">
            Start from files that already cleared the Data Pipeline into /processed. Create a project, group related spreadsheets into logical data objects, write metadata, and generate summary CSVs.
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
        <Stat label="Project files" value={projectFiles.length} />
        <Stat label="Detected groups" value={groups.length} />
        <Stat label="Summaries" value={Object.keys(summaries).length} />
      </div>

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
              <p>{processedPrefix}{projectId}/AR_Invoices/</p>
              <p>{processedPrefix}{projectId}/AP_Invoices/</p>
              <p>{processedPrefix}{projectId}/metadata/project.json</p>
            </div>
          </div>
        </div>
      </section>

      <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-sm font-bold text-slate-900">Choose processed files for this project</h2>
            <p className="mt-1 text-xs text-slate-500">
              Selected files become the project working set. Grouping, ungrouped files, metadata, and summaries use only this selection.
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={selectAllFiles}
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50"
            >
              <CheckCircle size={13} /> Select all
            </button>
            <button
              type="button"
              onClick={clearSelectedFiles}
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50"
            >
              <XCircle size={13} /> Clear
            </button>
          </div>
        </div>
        <div className="mt-3 grid gap-2 lg:grid-cols-2">
          {files.map(file => (
            <SelectableFileRow
              key={file.key || file.name}
              file={file}
              checked={selectedKeySet.has(file.key || file.name)}
              onChange={() => toggleProjectFile(file)}
            />
          ))}
          {!files.length && (
            <p className="rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs text-slate-500">No processed files found.</p>
          )}
        </div>
        <p className="mt-3 text-xs text-slate-500">
          {projectFiles.length} of {files.length} processed files selected for <span className="font-medium text-slate-700">{projectName}</span>.
          {csvCount ? ` ${csvCount} processed CSV files are available.` : ''}
        </p>
      </section>

      <section className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h2 className="text-sm font-bold text-slate-900">Detected data objects</h2>
            <p className="text-xs text-slate-500">Filename rules are the first signal; metadata becomes the durable truth.</p>
          </div>
          <button
            type="button"
            onClick={createMetadata}
            className="inline-flex items-center gap-1.5 rounded-lg bg-slate-900 px-3 py-2 text-xs font-semibold text-white hover:bg-slate-800"
          >
            <Database size={13} /> Create metadata
          </button>
        </div>
        <div className="grid gap-4">
          {groups.map(group => (
            <GroupCard
              key={group.id}
              group={group}
              projectId={projectId}
              processedPrefix={processedPrefix}
              summary={summaries[group.id]}
              onGenerateSummary={generateSummary}
            />
          ))}
          {!groups.length && (
            <div className="rounded-xl border border-dashed border-slate-300 bg-white p-8 text-center">
              <FileSpreadsheet size={24} className="mx-auto text-slate-400" />
              <p className="mt-2 text-sm font-semibold text-slate-800">No AR/AP invoice groups detected yet.</p>
              <p className="mt-1 text-xs text-slate-500">Upload processed files named AR_Invoice* or AP_Invoice* to see automatic grouping.</p>
            </div>
          )}
        </div>
      </section>

      <section className="grid gap-4 lg:grid-cols-2">
        <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <h2 className="text-sm font-bold text-slate-900">Ungrouped processed files</h2>
          <p className="mt-1 text-xs text-slate-500">These remain in the project, but are not part of a spreadsheet data object yet.</p>
          <div className="mt-3 space-y-2">
            {ungrouped.length ? ungrouped.map(file => <FileRow key={file.key || file.name} file={file} />) : (
              <p className="rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs text-slate-500">No ungrouped files.</p>
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
