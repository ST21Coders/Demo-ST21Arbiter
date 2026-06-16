import { useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle, CheckCircle, ChevronDown, ChevronRight, Database, Download, Edit3,
  Eye, FileSpreadsheet, FolderTree, Loader2, Plus, RefreshCw, Save, Trash2, Upload, Wand2, XCircle,
} from 'lucide-react'
import { USE_MOCK } from '../config'
import { listUploadedFiles, presignUpload, uploadToPresignedUrl } from '../hooks/useApi'

const GROUPING_STORAGE_KEY = 'arbiter.dataGrouping.v2.projectMetadata'
const GROUPS_STORAGE_KEY = 'arbiter.dataGrouping.v2.savedGroups'
const METADATA_LEDGER_STORAGE_KEY = 'arbiter.dataGrouping.v2.metadataLedger'
const LOCAL_PROCESSED_UPLOADS_KEY = 'arbiter.dataGrouping.v2.localProcessedUploads'
const ASSOCIATION_OPTIONS = ['A', 'B', 'C', 'D', 'E']

const GROUP_TYPE_OPTIONS = [
  { value: 'special_project', label: 'Special Project', suggestedName: 'Special_Project', summaryFile: '' },
  { value: 'recurring_project', label: 'Recurring Project', suggestedName: 'Recurring_Project', summaryFile: '' },
  { value: 'audit', label: 'Audit', suggestedName: 'Audit', summaryFile: '' },
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

function parseCsv(text) {
  const rows = []
  let cell = ''
  let row = []
  let quoted = false

  for (let index = 0; index < String(text || '').length; index += 1) {
    const char = text[index]
    const next = text[index + 1]
    if (char === '"' && quoted && next === '"') {
      cell += '"'
      index += 1
    } else if (char === '"') {
      quoted = !quoted
    } else if (char === ',' && !quoted) {
      row.push(cell)
      cell = ''
    } else if ((char === '\n' || char === '\r') && !quoted) {
      if (char === '\r' && next === '\n') index += 1
      row.push(cell)
      if (row.some(value => value.trim())) rows.push(row)
      row = []
      cell = ''
    } else {
      cell += char
    }
  }

  row.push(cell)
  if (row.some(value => value.trim())) rows.push(row)
  if (rows.length < 2) return []

  const headers = rows[0].map(header => header.trim())
  return rows.slice(1).map(values => Object.fromEntries(headers.map((header, index) => [header, values[index] ?? ''])))
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
  return GROUP_TYPE_OPTIONS.find(option => option.value === type) || GROUP_TYPE_OPTIONS[0]
}

function makeSummaryName(groupName, groupType) {
  const option = optionForType(groupType)
  if (option.summaryFile) return option.summaryFile
  return `SUM_${String(groupName || 'Group').replace(/[^a-zA-Z0-9]+/g, '_').replace(/^_+|_+$/g, '')}.csv`
}

function rowsForFile(file) {
  const builtInMockKey = `users/mock/processed/${file.name}`
  if (file.csvText) return parseCsv(file.csvText)
  if (file.key === builtInMockKey) return SAMPLE_ROWS[file.name] || []
  return []
}

function rowsForFiles(files) {
  return files.filter(file => !file.summary).flatMap(file => {
    const rows = rowsForFile(file)
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

function rowsForGroup(group) {
  return rowsForFiles(group.files)
}

function normalizedStatus(value) {
  return String(value || 'Unknown')
    .trim()
    .replace(/_/g, ' ')
    .replace(/\s+/g, ' ')
    .replace(/\b\w/g, letter => letter.toUpperCase()) || 'Unknown'
}

function invoiceAmount(row) {
  const value = row.Invoice_Amount ?? row.invoice_amount ?? row.amount ?? row.Amount ?? 0
  return Number(String(value).replace(/[$,]/g, '')) || 0
}

function statusValue(row) {
  return normalizedStatus(row.Status ?? row.status)
}

function vendorName(row) {
  return String(
    row.Vendor_Name ??
    row.vendor_name ??
    row.Vendor ??
    row.vendor ??
    row.Client_Name ??
    row.client_name ??
    row.Customer_Name ??
    row.customer_name ??
    'Unknown'
  ).trim() || 'Unknown'
}

function departmentName(row) {
  return String(
    row.Department ??
    row.department ??
    row.Department_Name ??
    row.department_name ??
    row.Cost_Center ??
    row.cost_center ??
    row.Business_Unit ??
    row.business_unit ??
    'Unknown'
  ).trim() || 'Unknown'
}

function summarySourceFiles(group) {
  const csvFiles = group.files.filter(file => !file.summary && /\.csv$/i.test(file.name || ''))
  const associatedKeys = new Set((group.associations || []).flatMap(association => association.fileKeys))
  if (!associatedKeys.size) return csvFiles
  return csvFiles.filter(file => associatedKeys.has(fileKey(file)))
}

function loadedRowsForFiles(files) {
  return files.flatMap(file => (
    file.csvText ? parseCsv(file.csvText).map(row => ({ source_file: file.name, ...row })) : []
  ))
}

function recordCountSummaryRows(group) {
  const sourceFiles = summarySourceFiles(group)
  const fileRows = sourceFiles.map(file => ({
    summary_type: 'file',
    dimension: file.name,
    dimension2: '',
    dimension3: '',
    record_count: file.csvText ? parseCsv(file.csvText).length : 'not_loaded',
    invoice_amount: '',
  }))
  const total = fileRows.reduce((sum, row) => sum + (Number(row.record_count) || 0), 0)
  const loadedRows = loadedRowsForFiles(sourceFiles)
  const aggregateRows = (summaryType, valueForRow, secondValueForRow = null, thirdValueForRow = null) => {
    const byDimension = new Map()
    loadedRows.forEach(row => {
      const dimension = valueForRow(row)
      const dimension2 = secondValueForRow ? secondValueForRow(row) : ''
      const dimension3 = thirdValueForRow ? thirdValueForRow(row) : ''
      const key = `${dimension}\u0000${dimension2}\u0000${dimension3}`
      const current = byDimension.get(key) || { dimension, dimension2, dimension3, record_count: 0, invoice_amount: 0 }
      current.record_count += 1
      current.invoice_amount += invoiceAmount(row)
      byDimension.set(key, current)
    })
    return [...byDimension.values()]
      .sort((left, right) => (
        left.dimension.localeCompare(right.dimension)
        || left.dimension2.localeCompare(right.dimension2)
        || left.dimension3.localeCompare(right.dimension3)
      ))
      .map(summary => ({
        summary_type: summaryType,
        dimension: summary.dimension,
        dimension2: summary.dimension2,
        dimension3: summary.dimension3,
        record_count: summary.record_count,
        invoice_amount: summary.invoice_amount.toFixed(2),
      }))
  }

  return [
    ...fileRows,
    { summary_type: 'total', dimension: 'TOTAL RECORDS', dimension2: '', dimension3: '', record_count: total, invoice_amount: '' },
    ...aggregateRows('vendor_department_status', vendorName, departmentName, statusValue),
    ...aggregateRows('status', statusValue),
  ]
}

function invoiceGuideSummaryRows(group) {
  const sourceFiles = summarySourceFiles(group)
  const missingFiles = sourceFiles.filter(file => !file.csvText)
  if (missingFiles.length) {
    return [
      { status: 'not_available', invoice_count: 0, total_invoice_amount: '0.00', average_invoice_amount: '0.00', missing_loaded_csv_files: missingFiles.length },
    ]
  }

  const byStatus = new Map()
  loadedRowsForFiles(sourceFiles).forEach(row => {
    const status = statusValue(row)
    const current = byStatus.get(status) || { status, invoice_count: 0, total_invoice_amount: 0 }
    current.invoice_count += 1
    current.total_invoice_amount += invoiceAmount(row)
    byStatus.set(status, current)
  })

  return [...byStatus.values()]
    .sort((left, right) => right.total_invoice_amount - left.total_invoice_amount || left.status.localeCompare(right.status))
    .map(row => ({
      status: row.status,
      invoice_count: row.invoice_count,
      total_invoice_amount: row.total_invoice_amount.toFixed(2),
      average_invoice_amount: (row.total_invoice_amount / row.invoice_count).toFixed(2),
    }))
}

function columnsForCsv(file) {
  const rows = rowsForFile(file)
  if (rows[0]) return Object.keys(rows[0])
  return ['source_file', 'source_key', 'note']
}

function validateGroup(group) {
  const csvFiles = group.files.filter(file => !file.summary && /\.csv$/i.test(file.name || ''))
  const instructionFiles = group.files.filter(file => /\.(pdf|docx|txt|md)$/i.test(file.name || ''))
  const csvColumnSignatures = csvFiles.map(file => columnsForCsv(file).join('|'))
  const uniqueColumnSignatures = new Set(csvColumnSignatures)
  const associatedFileKeys = new Set((group.associations || []).flatMap(association => association.fileKeys))
  const unassociatedCsvCount = csvFiles.filter(file => !associatedFileKeys.has(fileKey(file))).length

  return [
    {
      status: csvFiles.length >= 2 ? 'pass' : 'fail',
      label: `${csvFiles.length} CSV files`,
      detail: csvFiles.length >= 2 ? 'Ready for grouped CSV summary.' : 'Add at least two CSV files before summarizing.',
    },
    {
      status: instructionFiles.length ? 'pass' : 'warn',
      label: `${instructionFiles.length} instruction file${instructionFiles.length === 1 ? '' : 's'}`,
      detail: instructionFiles.length ? 'Instructions are attached to this group.' : 'Attach a PDF, DOCX, TXT, or MD guide when available.',
    },
    {
      status: uniqueColumnSignatures.size <= 1 ? 'pass' : 'warn',
      label: uniqueColumnSignatures.size <= 1 ? 'CSV schemas align' : 'CSV schemas differ',
      detail: uniqueColumnSignatures.size <= 1 ? 'Detected CSV columns are compatible for summary.' : 'Review columns before generating the final summary.',
    },
    {
      status: unassociatedCsvCount === 0 || !(group.associations || []).length ? 'pass' : 'warn',
      label: `${unassociatedCsvCount} unassociated CSV${unassociatedCsvCount === 1 ? '' : 's'}`,
      detail: (group.associations || []).length ? 'Associations can drive more specific summaries.' : 'No associations yet; summary will use all CSV files together.',
    },
  ]
}

function amountTotal(rows) {
  return rows.reduce((sum, row) => sum + invoiceAmount(row) + (Number(row.total_invoice_amount) || 0), 0)
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

function mergeFiles(baseFiles, localFiles) {
  const seen = new Set()
  return [...baseFiles, ...localFiles].filter(file => {
    const key = fileKey(file)
    if (!key || seen.has(key)) return false
    seen.add(key)
    return true
  })
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
  const isSummary = Boolean(file.summary)
  return (
    <div className={`flex items-center justify-between gap-3 rounded-lg border px-3 py-2 ${isSummary ? 'border-indigo-200 bg-indigo-50' : 'border-slate-200 bg-slate-50'}`}>
      <div className="min-w-0">
        <p className="truncate text-sm font-medium text-slate-800" title={file.name}>
          {file.name}
          {isSummary && <span className="ml-2 rounded-full border border-indigo-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-indigo-700">Summary</span>}
        </p>
        <p className="truncate font-mono text-[10px] text-slate-400" title={file.key}>{file.key}</p>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <span className="text-xs text-slate-500">{formatBytes(file.size)}</span>
        {action}
      </div>
    </div>
  )
}

function ProcessedFileRow({ file, status, groupName, onAdd }) {
  const isAvailable = status === 'available'
  const statusStyles = {
    available: 'border-emerald-200 bg-emerald-50 text-emerald-700',
    selected: 'border-indigo-200 bg-indigo-50 text-indigo-700',
    grouped: 'border-slate-200 bg-slate-100 text-slate-600',
  }
  const statusLabel = {
    available: 'Available',
    selected: 'Selected in current group',
    grouped: `In ${groupName || 'saved group'}`,
  }[status]

  return (
    <div className="flex items-center gap-3 rounded-lg border border-slate-200 bg-white px-3 py-2">
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-slate-800" title={file.name}>{file.name}</p>
        <p className="truncate font-mono text-[10px] text-slate-400" title={file.key}>{file.key}</p>
      </div>
      <span className="shrink-0 text-xs text-slate-500">{formatBytes(file.size)}</span>
      <span className={`shrink-0 rounded-full border px-2 py-1 text-[10px] font-semibold ${statusStyles[status]}`}>
        {statusLabel}
      </span>
      {isAvailable && (
        <button
          type="button"
          onClick={onAdd}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-indigo-600 px-2.5 py-1.5 text-xs font-semibold text-white hover:bg-indigo-700"
        >
          <Plus size={13} /> Add
        </button>
      )}
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
  onSaveAssociation, onDeleteAssociation, onDisassociateFile, onLoadCsvContents, collapsed, onToggleCollapse,
}) {
  const [activeAction, setActiveAction] = useState('validate')
  const [instructionPreviewFile, setInstructionPreviewFile] = useState(null)
  const groupCsvInputRef = useRef(null)
  const rows = summary?.rows || []
  const csvFiles = group.files.filter(file => !file.summary && /\.csv$/i.test(file.name || ''))
  const loadedCsvCount = csvFiles.filter(file => file.csvText).length
  const instructionFiles = group.files.filter(file => /\.(pdf|docx|txt|md)$/i.test(file.name || ''))
  const usingInstructionGuide = instructionFiles.length > 0
  const canAssociate = csvFiles.length >= 2
  const validation = validateGroup(group)
  const previewRows = rowsForGroup(group).slice(0, 5)
  const currentSummarySourceFiles = summarySourceFiles(group)
  const currentMissingCsvFileCount = currentSummarySourceFiles.filter(file => !file.csvText).length
  const summarySourceFileCount = summary?.sourceFileCount ?? currentSummarySourceFiles.length
  const summarySourceRowCount = summary?.sourceRowCount ?? loadedRowsForFiles(currentSummarySourceFiles).length
  const missingCsvFileCount = summary?.missingCsvFileCount ?? currentMissingCsvFileCount
  const canGenerateSummary = currentSummarySourceFiles.length >= 2 && currentMissingCsvFileCount === 0
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
                {group.files.map(file => {
                  const summaryCsvText = file.summary ? (file.csvText || (summary ? toCsv(summary.rows) : '')) : ''
                  return (
                    <FileRow
                      key={fileKey(file)}
                      file={file}
                      action={file.summary && summaryCsvText ? (
                        <button
                          type="button"
                          onClick={() => downloadText(file.name, summaryCsvText, 'text/csv')}
                          className="inline-flex items-center gap-1.5 rounded-lg border border-indigo-200 bg-white px-2.5 py-1.5 text-xs font-semibold text-indigo-700 hover:bg-indigo-50"
                        >
                          <Download size={13} /> Download
                        </button>
                      ) : null}
                    />
                  )
                })}
              </div>
            </div>
            <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
              <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">Target processed structure</p>
              <p className="mt-2 break-all font-mono text-xs text-slate-700">{targetPrefix}</p>
              <div className="mt-3 rounded-lg border border-slate-200 bg-white p-3">
                <p className="text-xs font-semibold text-slate-800">{summaryFile}</p>
                <p className="mt-1 text-xs text-slate-500">
                  {currentSummarySourceFiles.length >= 2
                    ? canGenerateSummary
                      ? 'Current summary uses loaded CSV rows to create file, total, vendor-department-status, and status totals.'
                      : 'Load this group\'s CSV contents before generating a real summary.'
                    : 'Add at least two CSV files before generating a spreadsheet summary.'}
                </p>
              </div>
              {summary && (
                <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
                  <div className="rounded-lg border border-slate-200 bg-white p-2">
                    <p className="text-slate-400">Rows</p>
                    <p className="font-semibold text-slate-900">{rows.length}</p>
                  </div>
                  <div className="rounded-lg border border-slate-200 bg-white p-2">
                    <p className="text-slate-400">Loaded records</p>
                    <p className="font-semibold text-slate-900">{summarySourceRowCount}</p>
                  </div>
                </div>
              )}
            </div>
          </div>

          <div className="mt-4 rounded-lg border border-slate-200 bg-slate-50 p-3">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">Group actions</p>
                <p className="mt-1 text-xs text-slate-500">Review instructions, validate the group, preview combined rows, then generate a deterministic row-count summary CSV.</p>
              </div>
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => setActiveAction('instructions')}
                className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-xs font-semibold ${activeAction === 'instructions' ? 'bg-slate-900 text-white' : 'border border-slate-200 bg-white text-slate-700 hover:bg-slate-50'}`}
              >
                <FileSpreadsheet size={13} /> Review instructions
              </button>
              <button
                type="button"
                onClick={() => setActiveAction('validate')}
                className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-xs font-semibold ${activeAction === 'validate' ? 'bg-slate-900 text-white' : 'border border-slate-200 bg-white text-slate-700 hover:bg-slate-50'}`}
              >
                <CheckCircle size={13} /> Validate group
              </button>
              <button
                type="button"
                onClick={() => setActiveAction('preview')}
                className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-xs font-semibold ${activeAction === 'preview' ? 'bg-slate-900 text-white' : 'border border-slate-200 bg-white text-slate-700 hover:bg-slate-50'}`}
              >
                <Database size={13} /> Preview combined CSV
              </button>
              <input
                ref={groupCsvInputRef}
                type="file"
                multiple
                accept=".csv"
                className="hidden"
                onChange={(event) => {
                  onLoadCsvContents(group.id, event.target.files)
                  event.target.value = ''
                }}
              />
              <button
                type="button"
                onClick={() => groupCsvInputRef.current?.click()}
                className="inline-flex items-center gap-1.5 rounded-lg border border-indigo-200 bg-white px-3 py-2 text-xs font-semibold text-indigo-700 hover:bg-indigo-50"
              >
                <Upload size={13} /> Load CSV contents
              </button>
              <button
                type="button"
                disabled={!canGenerateSummary}
                onClick={() => {
                  onGenerateSummary(group)
                  setActiveAction('summary')
                }}
                className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-2 text-xs font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-slate-200 disabled:text-slate-500"
              >
                <Wand2 size={13} /> Generate summary CSV
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

            {activeAction === 'instructions' && (
              <div className="mt-3 rounded-lg border border-slate-200 bg-white p-3">
                <p className="text-xs font-bold text-slate-800">Attached instructions</p>
                <div className="mt-2 space-y-2">
                  {instructionFiles.map(file => (
                    <FileRow
                      key={fileKey(file)}
                      file={file}
                      action={(
                        <button
                          type="button"
                          onClick={() => setInstructionPreviewFile(file)}
                          className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-50"
                        >
                          <Eye size={13} /> Quick view
                        </button>
                      )}
                    />
                  ))}
                  {!instructionFiles.length && (
                    <p className="rounded-lg border border-dashed border-slate-300 bg-slate-50 p-3 text-xs text-slate-500">No instruction file attached to this group yet.</p>
                  )}
                </div>
                <p className="mt-2 text-xs text-slate-500">Next backend step: extract the PDF guide text and use it as context for summary generation.</p>
              </div>
            )}

            {activeAction === 'validate' && (
              <div className="mt-3 grid gap-2 md:grid-cols-2">
                {validation.map(check => (
                  <div key={check.label} className={`rounded-lg border p-3 ${check.status === 'pass' ? 'border-emerald-200 bg-emerald-50' : check.status === 'fail' ? 'border-red-200 bg-red-50' : 'border-amber-200 bg-amber-50'}`}>
                    <p className={`text-xs font-bold ${check.status === 'pass' ? 'text-emerald-800' : check.status === 'fail' ? 'text-red-800' : 'text-amber-800'}`}>{check.label}</p>
                    <p className={`mt-1 text-xs ${check.status === 'pass' ? 'text-emerald-700' : check.status === 'fail' ? 'text-red-700' : 'text-amber-700'}`}>{check.detail}</p>
                  </div>
                ))}
              </div>
            )}

            {activeAction === 'preview' && (
              <div className="mt-3 overflow-auto rounded-lg border border-slate-200 bg-white">
                <table className="min-w-full text-left text-xs">
                  <thead className="bg-slate-50 text-slate-500">
                    <tr>
                      {Object.keys(previewRows[0] || { note: '' }).map(header => (
                        <th key={header} className="px-3 py-2 font-semibold">{header}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {previewRows.map((row, index) => (
                      <tr key={`${row.source_file || 'row'}-${index}`} className="border-t border-slate-100">
                        {Object.keys(previewRows[0] || { note: '' }).map(header => (
                          <td key={header} className="max-w-[220px] truncate px-3 py-2 text-slate-700" title={String(row[header] ?? '')}>{row[header] ?? ''}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
                <p className="border-t border-slate-100 px-3 py-2 text-xs text-slate-500">Previewing first {previewRows.length} combined source rows. Generate summary CSV counts records per associated CSV and totals them.</p>
              </div>
            )}

            {activeAction === 'summary' && (
              <div className="mt-3 rounded-lg border border-indigo-200 bg-white p-3">
                <p className="text-xs font-bold text-indigo-800">{summary ? 'Summary CSV generated' : 'Generating summary CSV'}</p>
                <p className="mt-1 text-xs text-slate-500">
                  {summary
                    ? missingCsvFileCount
                      ? `${summaryFile} cannot produce a real count yet: ${missingCsvFileCount} associated CSV files need loaded contents.`
                      : `${summaryFile} contains per-file record counts from ${summarySourceFileCount} associated CSV files and ${summarySourceRowCount} loaded source rows.`
                    : 'Click Generate summary CSV to count loaded records across associated CSV files.'}
                </p>
              </div>
            )}
            {loadedCsvCount < csvFiles.length && (
              <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
                {loadedCsvCount} of {csvFiles.length} CSV files have loaded row content. Use Load CSV contents and select the CSV files in this group before generating a real record count.
              </div>
            )}
          </div>

          {canAssociate && (
            <AssociationBuilder
              group={group}
              onSaveAssociation={onSaveAssociation}
              onDeleteAssociation={onDeleteAssociation}
              onDisassociateFile={onDisassociateFile}
            />
          )}
          {instructionPreviewFile && (
            <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/60 p-4">
              <div className="flex max-h-[88vh] w-full max-w-3xl flex-col overflow-hidden rounded-xl border border-slate-200 bg-white shadow-xl">
                <div className="flex items-start justify-between gap-3 border-b border-slate-200 p-4">
                  <div className="min-w-0">
                    <p className="text-sm font-bold text-slate-900">Instruction quick view</p>
                    <p className="mt-1 truncate font-mono text-xs text-slate-500" title={instructionPreviewFile.key}>{instructionPreviewFile.name}</p>
                  </div>
                  <button
                    type="button"
                    onClick={() => setInstructionPreviewFile(null)}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-50"
                  >
                    <XCircle size={13} /> Close
                  </button>
                </div>
                <div className="overflow-auto p-4">
                  <div className="rounded-lg border border-slate-200 bg-slate-50 p-4">
                    <p className="text-xs font-semibold text-slate-700">PDF preview placeholder</p>
                    <p className="mt-2 text-xs text-slate-500">
                      Local quick view is ready to host the PDF renderer. In the real S3-backed version, this modal will load the guide with a signed URL and render it inline.
                    </p>
                    <div className="mt-3 rounded-lg border border-slate-200 bg-white p-3 text-xs">
                      <p className="font-semibold text-slate-800">File</p>
                      <p className="mt-1 break-all font-mono text-slate-500">{instructionPreviewFile.key}</p>
                      <p className="mt-3 font-semibold text-slate-800">Expected Arbiter instruction</p>
                      <p className="mt-1 text-slate-600">Combine all CSV files, group by Status, then calculate invoice count, total Invoice Amount, and average Invoice Amount.</p>
                    </div>
                  </div>
                </div>
              </div>
            </div>
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
  const [uploading, setUploading] = useState(false)
  const [uploadMessage, setUploadMessage] = useState('')
  const [editingGroupId, setEditingGroupId] = useState(null)
  const [draftName, setDraftName] = useState('')
  const [draftType, setDraftType] = useState('special_project')
  const [draftKeys, setDraftKeys] = useState([])
  const [collapsedGroupIds, setCollapsedGroupIds] = useState([])
  const uploadInputRef = useRef(null)

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
  const assignedGroupByKey = useMemo(() => {
    const entries = []
    hydratedGroups.forEach(group => {
      group.fileKeys.forEach(key => entries.push([key, group]))
    })
    return new Map(entries)
  }, [hydratedGroups])
  const draftKeySet = useMemo(() => new Set(draftKeys), [draftKeys])
  const editingGroup = hydratedGroups.find(group => group.id === editingGroupId)
  const ungroupedFiles = useMemo(() => files.filter(file => !assignedKeySet.has(fileKey(file))), [files, assignedKeySet])
  const selectedDraftFiles = useMemo(() => files.filter(file => draftKeySet.has(fileKey(file))), [files, draftKeySet])
  const csvCount = files.filter(file => /\.csv$/i.test(file.name || '')).length
  const csvDraftCount = selectedDraftFiles.filter(file => /\.csv$/i.test(file.name || '')).length

  async function loadFiles() {
    setLoading(true)
    setError('')
    try {
      const data = await listUploadedFiles('processed')
      const localFiles = JSON.parse(localStorage.getItem(LOCAL_PROCESSED_UPLOADS_KEY) || '[]')
      setFiles(mergeFiles(data.files || [], Array.isArray(localFiles) ? localFiles : []))
    } catch (err) {
      setError(err.message || 'Unable to list processed files')
    } finally {
      setLoading(false)
    }
  }

  async function handleProcessedUpload(fileList) {
    const selectedFiles = Array.from(fileList || [])
    if (!selectedFiles.length) return
    setUploading(true)
    setError('')
    setUploadMessage('')
    try {
      if (USE_MOCK) {
        const uploadedFiles = await Promise.all(selectedFiles.map(async (file, index) => {
          const isCsv = /\.csv$/i.test(file.name)
          return {
            key: `users/mock/processed/${Date.now()}-${index}-${file.name}`,
            name: file.name,
            size: file.size,
            last_modified: new Date().toISOString(),
            csvText: isCsv ? await file.text() : undefined,
          }
        }))
        const previousLocalFiles = JSON.parse(localStorage.getItem(LOCAL_PROCESSED_UPLOADS_KEY) || '[]')
        const nextLocalFiles = mergeFiles(Array.isArray(previousLocalFiles) ? previousLocalFiles : [], uploadedFiles)
        localStorage.setItem(LOCAL_PROCESSED_UPLOADS_KEY, JSON.stringify(nextLocalFiles))
        setFiles(prev => mergeFiles(prev, uploadedFiles))
        setUploadMessage(`${selectedFiles.length} file${selectedFiles.length === 1 ? '' : 's'} added to local /processed.`)
        return
      }

      for (const file of selectedFiles) {
        const pre = await presignUpload({ filename: file.name, contentType: file.type })
        const res = await uploadToPresignedUrl(pre.url, pre.headers, file)
        if (!res.ok) throw new Error(`${file.name}: upload returned ${res.status}`)
      }
      setUploadMessage(`${selectedFiles.length} file${selectedFiles.length === 1 ? '' : 's'} uploaded to the pipeline. Refresh after processing completes.`)
      await loadFiles()
    } catch (err) {
      setError(err.message || 'Unable to upload file')
    } finally {
      setUploading(false)
      if (uploadInputRef.current) uploadInputRef.current.value = ''
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
    return GROUP_TYPE_OPTIONS.find(option => !usedTypes.has(option.value))?.value || 'special_project'
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
    setDraftType(optionForType(group.type).value)
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

  async function loadGroupCsvContents(groupId, fileList) {
    const selectedFiles = Array.from(fileList || []).filter(file => /\.csv$/i.test(file.name))
    if (!selectedFiles.length) return
    setMetadata(null)
    setError('')
    try {
      const loadedCsvFiles = await Promise.all(selectedFiles.map(async file => ({
        key: `users/mock/processed/${Date.now()}-${file.name}`,
        name: file.name,
        size: file.size,
        last_modified: new Date().toISOString(),
        csvText: await file.text(),
      })))
      const csvByName = new Map(loadedCsvFiles.map(file => [file.name, file]))
      setGroups(prev => {
        const nextGroups = prev.map(group => {
          if (group.id !== groupId) return group
          const existingNames = new Set((group.files || []).map(file => file.name))
          const filesWithLoadedContent = (group.files || []).map(file => {
            const loaded = csvByName.get(file.name)
            return loaded ? { ...file, ...loaded, key: file.key || loaded.key } : file
          })
          const newFiles = loadedCsvFiles.filter(file => !existingNames.has(file.name))
          return {
            ...group,
            fileKeys: [...groupFileKeys(group), ...newFiles.map(file => fileKey(file))],
            files: [...filesWithLoadedContent, ...newFiles],
            updatedAt: new Date().toISOString(),
          }
        })
        persistGroups(nextGroups)
        return nextGroups
      })
      setUploadMessage(`${selectedFiles.length} CSV file${selectedFiles.length === 1 ? '' : 's'} copied into the group with loaded row content.`)
    } catch (err) {
      setError(err.message || 'Unable to load CSV contents')
    }
  }

  function generateSummary(group) {
    const sourceFiles = summarySourceFiles(group)
    const sourceRows = loadedRowsForFiles(sourceFiles)
    const missingCsvFiles = sourceFiles.filter(file => !file.csvText)
    const instructionFiles = group.files.filter(file => /\.(pdf|docx|txt|md)$/i.test(file.name || ''))
    const usingInstructionGuide = instructionFiles.length > 0
    const summaryFile = makeSummaryName(group.name, group.type)
    const summaryKey = `${processedPrefix}${projectId}/${group.name}/${summaryFile}`
    if (sourceFiles.length < 2 || missingCsvFiles.length) {
      setSummaries(prev => ({
        ...prev,
        [group.id]: {
          file: summaryFile,
          rows: [],
          sourceFileCount: sourceFiles.length,
          sourceRowCount: sourceRows.length,
          missingCsvFileCount: missingCsvFiles.length,
          generatedAt: new Date().toISOString(),
          usingInstructionGuide,
        },
      }))
      setGroups(prev => {
        const nextGroups = prev.map(item => {
          if (item.id !== group.id) return item
          const filesWithoutOldSummary = (item.files || []).filter(file => !(file.summary && file.name === summaryFile))
          const fileKeysWithoutOldSummary = groupFileKeys(item).filter(key => key !== summaryKey)
          return {
            ...item,
            fileKeys: fileKeysWithoutOldSummary,
            files: filesWithoutOldSummary,
            updatedAt: new Date().toISOString(),
          }
        })
        persistGroups(nextGroups)
        return nextGroups
      })
      setUploadMessage(
        sourceFiles.length < 2
          ? 'Summary not generated: add or associate at least two CSV files first.'
          : `Summary not generated: ${missingCsvFiles.length} CSV file${missingCsvFiles.length === 1 ? '' : 's'} need loaded row content first.`,
      )
      return
    }
    const rows = recordCountSummaryRows(group)
    const csvText = toCsv(rows)
    const summaryObject = {
      key: summaryKey,
      name: summaryFile,
      size: csvText.length,
      last_modified: new Date().toISOString(),
      summary: true,
      csvText,
      generatedFromGroupId: group.id,
    }
    setMetadata(null)
    setGroups(prev => {
      const nextGroups = prev.map(item => {
        if (item.id !== group.id) return item
        const filesWithoutOldSummary = (item.files || []).filter(file => !(file.summary && file.name === summaryFile))
        const fileKeysWithoutOldSummary = groupFileKeys(item).filter(key => key !== summaryKey)
        return {
          ...item,
          fileKeys: [...fileKeysWithoutOldSummary, summaryKey],
          files: [...filesWithoutOldSummary, summaryObject],
          updatedAt: new Date().toISOString(),
        }
      })
      persistGroups(nextGroups)
      return nextGroups
    })
    setSummaries(prev => ({
      ...prev,
      [group.id]: {
        generatedAt: new Date().toISOString(),
        file: summaryObject,
        calculation: usingInstructionGuide
          ? 'Instruction guide detected. Verification summary counts loaded data records by associated CSV before applying higher-order invoice calculations.'
          : 'Count loaded data records across associated CSV files.',
        instructionFiles: instructionFiles.map(file => ({ name: file.name, key: file.key })),
        sourceFileCount: sourceFiles.length,
        sourceRowCount: sourceRows.length,
        missingCsvFileCount: missingCsvFiles.length,
        rows,
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
        <div className="flex flex-wrap gap-2">
          <input
            ref={uploadInputRef}
            type="file"
            multiple
            accept=".md,.pdf,.docx,.json,.txt,.csv"
            className="hidden"
            onChange={(event) => handleProcessedUpload(event.target.files)}
          />
          <button
            type="button"
            onClick={() => uploadInputRef.current?.click()}
            disabled={uploading}
            className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-2 text-xs font-semibold text-white shadow-sm hover:bg-indigo-700 disabled:opacity-60"
          >
            {uploading ? <Loader2 size={13} className="animate-spin" /> : <Upload size={13} />}
            Upload file
          </button>
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
      </div>

      <div className="grid gap-3 md:grid-cols-4">
        <Stat label="Processed files" value={files.length} />
        <Stat label="Available files" value={ungroupedFiles.length} />
        <Stat label="Saved groups" value={hydratedGroups.length} />
        <Stat label="Summaries" value={Object.keys(summaries).length} />
      </div>

      {USE_MOCK && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800">
          This local server is running in mock mode. Uploaded files are added to a local /processed list for this demo.
        </div>
      )}

      {uploadMessage && (
        <div className="rounded-xl border border-emerald-200 bg-emerald-50 p-4 text-sm text-emerald-800">
          {uploadMessage}
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
              onLoadCsvContents={loadGroupCsvContents}
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
          <h2 className="text-sm font-bold text-slate-900">Add files from /processed</h2>
          <p className="mt-1 text-xs text-slate-500">All processed files appear here. Available files can be added; selected or saved-group files are shown with status.</p>
          <div className="mt-3 max-h-[360px] space-y-2 overflow-auto pr-1">
            {files.length ? files.map(file => {
              const key = fileKey(file)
              const assignedGroup = assignedGroupByKey.get(key)
              const status = draftKeySet.has(key) ? 'selected' : assignedGroup ? 'grouped' : 'available'
              return (
                <ProcessedFileRow
                  key={key}
                  file={file}
                  status={status}
                  groupName={assignedGroup?.name}
                  onAdd={() => addDraftFile(file)}
                />
              )
            }) : (
              <p className="rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs text-slate-500">No processed files found.</p>
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
