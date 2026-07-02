import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  AlertTriangle, CheckCircle, ChevronDown, ChevronRight, ClipboardList, Database, Download, Edit3,
  Eye, FileSpreadsheet, FolderTree, Loader2, MessageSquare, Plus, RefreshCw, Save, Search, Trash2, Upload, Wand2, XCircle,
} from 'lucide-react'
import { USE_MOCK } from '../config'
import { analyzeDataGroupingDocuments, getDataGroupingProject, getUploadStatus, listDataGroupingProjects, listUploadedFiles, materializeDataGroupingProject, startDataGroupingCrawler } from '../hooks/useApi'

const GROUPING_STORAGE_KEY = 'arbiter.dataGrouping.v2.projectMetadata'
const GROUPS_STORAGE_KEY = 'arbiter.dataGrouping.v2.savedGroups'
const METADATA_LEDGER_STORAGE_KEY = 'arbiter.dataGrouping.v2.metadataLedger'
const MCP_CHAT_DRAFT_KEY = 'arbiter.mcpChat.sessionDraft.v1'
const GROUP_STATUS_SAMPLE_LIMIT = 60

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

function formatShortDate(value) {
  if (!value) return 'Unknown'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'Unknown'
  return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

function dateSortValue(value) {
  if (!value) return 0
  const time = new Date(value).getTime()
  return Number.isNaN(time) ? 0 : time
}

function fileAddedSortValue(file) {
  return Math.max(
    dateSortValue(file?.groupedAt),
    dateSortValue(file?.addedAt),
    dateSortValue(file?.assignedAt),
    dateSortValue(file?.last_modified),
    dateSortValue(file?.lastModified),
  )
}

function newestFilesFirst(files) {
  return [...(files || [])].sort((a, b) => (
    fileAddedSortValue(b) - fileAddedSortValue(a)
    || String(a?.name || '').localeCompare(String(b?.name || ''))
  ))
}

function fileHasMaterializedEvidence(file) {
  return Boolean(
    file?.projectKey
    || file?.structuredKey
    || file?.glueTableHint
    || file?.sourceKey
    || String(fileKey(file)).startsWith('projects/')
    || String(fileKey(file)).startsWith('structured/')
  )
}

function groupHasStructuredEvidence(group) {
  return Boolean(
    group?.structuredTableHint
    || group?.glueTableHint
    || (Array.isArray(group?.structuredTableHints) && group.structuredTableHints.length)
    || (group?.files || []).some(file => file?.glueTableHint || file?.structuredKey)
  )
}

function normalizedGroupName(value) {
  return String(value || '').trim().toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '')
}

function findRemoteGroupRecord(group, remoteGroups) {
  const localName = normalizedGroupName(group?.name || group?.groupName)
  const localProjectId = String(group?.projectId || '').toLowerCase()
  return (remoteGroups || []).find(remote => {
    const remoteName = normalizedGroupName(remote.groupName || remote.value || remote.name)
    const remoteProjectId = String(remote.projectId || '').toLowerCase()
    return remoteName === localName && (!localProjectId || !remoteProjectId || remoteProjectId === localProjectId)
  })
}

function readinessFromRemoteGroup(group, remoteGroup, localFileCount, localCsvCount) {
  if (!remoteGroup) return null
  const remoteFileCount = Number(remoteGroup.fileCount || 0)
  const remoteCsvCount = Number(remoteGroup.csvCount || 0)
  const remoteTableCount = Number(remoteGroup.tableCount || 0)
  const readyTableCount = Number(remoteGroup.readyTableCount ?? remoteTableCount)
  const expectedTableCount = Number(remoteGroup.expectedTableCount || 0)
  const crawler = remoteGroup.crawler || {}
  const crawlerState = String(crawler.state || '').toUpperCase()
  const lastCrawlStatus = String(crawler.lastCrawlStatus || '').toUpperCase()
  const hasTables = remoteTableCount > 0 || (remoteGroup.tableHints || []).length > 0
  const profileKind = remoteGroup.groupProfile?.kind || group?.groupProfile?.kind || ''
  const needsShapeReadyTables = profileKind === 'sales'
  const localUpdatedAt = dateSortValue(group?.updatedAt)
  const recentlyChanged = localUpdatedAt && Date.now() - localUpdatedAt < 30 * 60 * 1000
  if (!remoteFileCount) {
    return {
      state: 'yellow',
      message: 'Group metadata found; waiting for published file count.',
      remoteGroup,
    }
  }
  if (remoteCsvCount || localCsvCount) {
    if (lastCrawlStatus === 'FAILED') {
      return {
        state: 'red',
        message: crawler.lastCrawlError || 'Glue crawler failed while indexing this group.',
        remoteGroup,
      }
    }
    if (crawlerState === 'RUNNING') {
      return {
        state: 'yellow',
        message: `${remoteFileCount || localFileCount} files published; Glue crawler is still running.`,
        remoteGroup,
      }
    }
    if (expectedTableCount && remoteTableCount < expectedTableCount) {
      return {
        state: 'yellow',
        message: `${remoteFileCount || localFileCount} files published; ${remoteTableCount}/${expectedTableCount} Glue table${expectedTableCount === 1 ? '' : 's'} ready.`,
        remoteGroup,
      }
    }
    if (needsShapeReadyTables && !readyTableCount) {
      return {
        state: 'yellow',
        message: `${remoteFileCount || localFileCount} files published; waiting for sales-shaped Glue columns to be available.`,
        remoteGroup,
      }
    }
    if (!expectedTableCount && !hasTables) {
      return {
        state: 'yellow',
        message: `${remoteFileCount || localFileCount} files published; waiting for Glue table hints.`,
        remoteGroup,
      }
    }
  }
  if (recentlyChanged && localFileCount && remoteFileCount < localFileCount) {
    return {
      state: 'yellow',
      message: `${remoteFileCount}/${localFileCount} files published in group metadata.`,
      remoteGroup,
    }
  }
  return {
    state: 'green',
    message: `${remoteFileCount || localFileCount} files published${hasTables ? `; ${needsShapeReadyTables ? readyTableCount : remoteTableCount || (remoteGroup.tableHints || []).length} Glue table${(needsShapeReadyTables ? readyTableCount : remoteTableCount || (remoteGroup.tableHints || []).length) === 1 ? '' : 's'} ready.` : '.'}`,
    remoteGroup,
  }
}

function groupStatusTone(status) {
  if (!status) return {
    label: 'Status',
    detail: 'Not checked yet',
    className: 'border-slate-200 bg-slate-50 text-slate-600 hover:bg-slate-100',
    dotClassName: 'bg-slate-400',
  }
  if (status.state === 'checking') return {
    label: 'Status',
    detail: 'Checking group readiness',
    className: 'border-amber-200 bg-amber-50 text-amber-700 hover:bg-amber-100',
    dotClassName: 'bg-amber-400',
  }
  if (status.state === 'green') return {
    label: 'Status',
    detail: status.message || 'Group is ready',
    className: 'border-emerald-200 bg-emerald-50 text-emerald-700 hover:bg-emerald-100',
    dotClassName: 'bg-emerald-500',
  }
  if (status.state === 'red') return {
    label: 'Status',
    detail: status.message || 'Group has readiness issues',
    className: 'border-red-200 bg-red-50 text-red-700 hover:bg-red-100',
    dotClassName: 'bg-red-500',
  }
  return {
    label: 'Status',
    detail: status.message || 'Group is still processing',
    className: 'border-amber-200 bg-amber-50 text-amber-700 hover:bg-amber-100',
    dotClassName: 'bg-amber-400',
  }
}

function fileKind(file) {
  const name = String(file?.name || '').toLowerCase()
  if (name.endsWith('.csv')) return 'csv'
  if (name.endsWith('.pdf')) return 'pdf'
  if (name.endsWith('.txt') || name.endsWith('.md')) return 'text'
  if (name.endsWith('.json')) return 'json'
  return 'other'
}

function fileKey(file) {
  return file?.key || file?.sourceKey || file?.source_key || file?.s3_key || file?.path || file?.name || ''
}

function groupFileKeys(group) {
  if (Array.isArray(group.fileKeys)) return group.fileKeys
  return (group.files || []).map(file => fileKey(file)).filter(Boolean)
}

function isDocumentAnalysisFile(file) {
  return /\.(txt|md|json|pdf)$/i.test(file?.name || '')
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

function normalizedFieldName(name) {
  return String(name || '').trim().toLowerCase().replace(/[^a-z0-9]/g, '')
}

function rowValue(row, candidates) {
  for (const candidate of candidates) {
    if (row[candidate] !== undefined && row[candidate] !== '') return row[candidate]
  }
  const normalizedCandidates = new Set(candidates.map(normalizedFieldName))
  const matchedKey = Object.keys(row).find(key => normalizedCandidates.has(normalizedFieldName(key)))
  return matchedKey ? row[matchedKey] : undefined
}

function invoiceAmount(row) {
  const value = rowValue(row, ['Invoice_Amount', 'invoice_amount', 'Invoice Amount', 'amount', 'Amount']) ?? 0
  return Number(String(value).replace(/[$,]/g, '')) || 0
}

function statusValue(row) {
  return normalizedStatus(rowValue(row, ['Status', 'status']))
}

function vendorName(row) {
  return String(
    rowValue(row, [
      'Vendor_Name',
      'vendor_name',
      'Vendor Name',
      'Vendor',
      'vendor',
      'Client_Name',
      'client_name',
      'Client Name',
      'Customer_Name',
      'customer_name',
      'Customer Name',
    ]) ??
    'Unknown'
  ).trim() || 'Unknown'
}

function departmentName(row) {
  return String(
    rowValue(row, [
      'Department',
      'department',
      'Department_Name',
      'department_name',
      'Department Name',
      'Dept',
      'dept',
      'Dept_Name',
      'dept_name',
      'Dept Name',
      'Cost_Center',
      'cost_center',
      'Cost Center',
      'Business_Unit',
      'business_unit',
      'Business Unit',
    ]) ??
    'No Department Found'
  ).trim() || 'No Department Found'
}

function summarySourceFiles(group) {
  return group.files.filter(file => !file.summary && /\.csv$/i.test(file.name || ''))
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

function normalizeProfileColumn(value) {
  return String(value || '').trim().toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '')
}

function salesStarterPrompts(groupName) {
  return [
    `For this ${groupName} group, rank stores from highest to lowest total sales. Include branch city, branch state, total revenue, units sold, transaction count, top category, and a short explanation.`,
    `For this ${groupName} group, rank product categories by revenue and units sold. Include part category, total revenue, units sold, average unit price, and the leading branch if available.`,
    `For this ${groupName} group, identify the top products by revenue. Include Part_SKU, Part_Name, Part_Category, total revenue, units sold, average unit price, and the stores where each product is strongest.`,
    `For this ${groupName} group, compare sales channels by revenue, units sold, transaction count, and average line revenue. Include a short explanation of channel mix.`,
    `For this ${groupName} group, analyze gross margin using Unit_Cost and Unit_Price. Rank stores or products by estimated margin dollars and margin percent.`,
    `For this ${groupName} group, find underperforming stores by total revenue and units sold. Include branch city, branch state, total revenue, units sold, transaction count, and likely next review question.`,
  ]
}

function genericStarterPrompts(groupName) {
  return [
    `List the available files and tables in this ${groupName} group and briefly explain what each one appears to contain.`,
    `Summarize this ${groupName} group. Include row counts if available, important columns, and the most useful first questions to ask.`,
    `Show the first records from the main table in this ${groupName} group and explain the likely purpose of the data.`,
  ]
}

function buildGroupProfile(group) {
  const groupName = group?.name || 'selected'
  const columns = new Set()
  ;(group?.files || []).forEach(file => {
    if (/\.csv$/i.test(file?.name || '')) {
      columnsForCsv(file).forEach(column => {
        const normalized = normalizeProfileColumn(column)
        if (normalized) columns.add(normalized)
      })
    }
  })
  const columnList = [...columns].sort()
  const salesColumns = new Set([
    'sale_id', 'sales_date', 'branch_city', 'branch_state', 'part_sku',
    'part_name', 'part_category', 'quantity_sold', 'unit_cost', 'unit_price',
    'line_revenue', 'sales_channel', 'customer_type',
  ])
  const salesMatches = columnList.filter(column => salesColumns.has(column)).length
  if (salesMatches >= 8 || (normalizeProfileColumn(groupName).includes('sales') && columns.has('line_revenue'))) {
    return {
      kind: 'sales',
      confidence: salesMatches >= 8 ? 'high' : 'medium',
      columns: columnList,
      starterPrompts: salesStarterPrompts(groupName),
    }
  }
  return {
    kind: 'generic',
    confidence: 'low',
    columns: columnList,
    starterPrompts: genericStarterPrompts(groupName),
  }
}

function validateGroup(group) {
  const csvFiles = group.files.filter(file => !file.summary && /\.csv$/i.test(file.name || ''))
  const instructionFiles = group.files.filter(file => /\.(pdf|docx|txt|md)$/i.test(file.name || ''))
  const csvColumnSignatures = csvFiles.map(file => columnsForCsv(file).join('|'))
  const uniqueColumnSignatures = new Set(csvColumnSignatures)

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
      groupProfile: group.groupProfile || buildGroupProfile(group),
      groupRule: 'manual_file_selection',
      targetPrefix: `${processedPrefix}${projectId}/${group.name}/`,
      sourceFiles: group.files.map(file => ({
        name: file.name,
        key: file.key,
        size: file.size,
        lastModified: file.last_modified,
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

function stripRetiredGroupFields(group) {
  const retiredKey = ['assoc', 'iations'].join('')
  return Object.fromEntries(Object.entries(group).filter(([key]) => key !== retiredKey))
}

function persistGroups(groups) {
  localStorage.setItem(GROUPS_STORAGE_KEY, JSON.stringify(groups.map(stripRetiredGroupFields)))
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
  const kind = fileKind(file)
  return (
    <div className={`grid grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-3 rounded-lg border px-3 py-2 ${isSummary ? 'border-indigo-200 bg-indigo-50' : 'border-slate-200 bg-slate-50'}`}>
      <div className="min-w-0">
        <p className="truncate text-xs font-semibold text-slate-800" title={file.name}>
          {file.name}
          {isSummary && <span className="ml-2 rounded-full border border-indigo-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-indigo-700">Summary</span>}
        </p>
        <p className="truncate font-mono text-[10px] text-slate-400" title={file.key}>{file.key}</p>
      </div>
      <div className="hidden shrink-0 items-center gap-2 text-[10px] text-slate-500 sm:flex">
        <span className="rounded-full border border-slate-200 bg-white px-2 py-1 font-semibold uppercase text-slate-500">{kind}</span>
        <span>{formatBytes(file.size)}</span>
      </div>
      <div className="flex shrink-0 items-center gap-2">{action}</div>
    </div>
  )
}

function ProcessedFileRow({ file, status, groupName, onToggle, disabled = false }) {
  const isAvailable = status === 'available'
  const isSelected = status === 'selected'
  const canToggle = !disabled && (isAvailable || isSelected)
  const kind = fileKind(file)
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
    <label className={`grid items-center gap-3 rounded-lg border px-3 py-2 ${canToggle ? 'cursor-pointer border-slate-200 bg-white hover:border-indigo-200 hover:bg-indigo-50' : 'cursor-not-allowed border-slate-200 bg-slate-50'}`}
           style={{ gridTemplateColumns: 'auto minmax(0,1fr) auto auto' }}>
      <input
        type="checkbox"
        checked={isSelected}
        disabled={!canToggle}
        onChange={() => onToggle(file)}
        className="h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500 disabled:cursor-not-allowed"
        aria-label={`${isSelected ? 'Remove' : 'Select'} ${file.name}`}
      />
      <div className="min-w-0 flex-1">
        <p className="truncate text-xs font-semibold text-slate-800" title={file.name}>{file.name}</p>
        <p className="truncate font-mono text-[10px] text-slate-400" title={file.key}>{file.key}</p>
      </div>
      <div className="hidden shrink-0 items-center gap-2 text-[10px] text-slate-500 md:flex">
        <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-1 font-semibold uppercase text-slate-500">{kind}</span>
        <span>{formatBytes(file.size)}</span>
        <span>{formatShortDate(file.last_modified)}</span>
      </div>
      <span className={`shrink-0 rounded-full border px-2 py-1 text-[10px] font-semibold ${statusStyles[status]}`}>
        {statusLabel}
      </span>
    </label>
  )
}

function SelectedDraftFileRow({ file, onRemove }) {
  return (
    <div className="flex items-center gap-2 rounded-md border border-indigo-100 bg-indigo-50/70 px-2 py-1">
      <div className="min-w-0 flex-1">
        <p className="truncate text-[10px] font-medium leading-3 text-slate-800" title={file.name}>{file.name}</p>
        <p className="truncate font-mono text-[8px] leading-3 text-indigo-400" title={file.key}>{file.key}</p>
      </div>
      <span className="shrink-0 text-[9px] leading-3 text-slate-500">{formatBytes(file.size)}</span>
      <button
        type="button"
        onClick={onRemove}
        className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded border border-red-100 bg-white text-red-700 hover:bg-red-50"
        title={`Remove ${file.name}`}
        aria-label={`Remove ${file.name}`}
      >
        <Trash2 size={10} />
      </button>
    </div>
  )
}

function GroupCard({
  group, projectId, processedPrefix, summary, onGenerateSummary, onEdit, onDelete,
  onLoadCsvContents, analysis, analysisError, analyzing, publishing, onAnalyzeDocuments, collapsed, onToggleCollapse,
}) {
  const [activeAction, setActiveAction] = useState(USE_MOCK ? 'validate' : 'instructions')
  const [instructionPreviewFile, setInstructionPreviewFile] = useState(null)
  const groupCsvInputRef = useRef(null)
  const rows = summary?.rows || []
  const csvFiles = group.files.filter(file => !file.summary && /\.csv$/i.test(file.name || ''))
  const documentFiles = group.files.filter(file => !file.summary && isDocumentAnalysisFile(file))
  const loadedCsvCount = csvFiles.filter(file => file.csvText).length
  const instructionFiles = group.files.filter(file => /\.(pdf|docx|txt|md)$/i.test(file.name || ''))
  const usingInstructionGuide = instructionFiles.length > 0
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
    <article id={`group-${group.id}`} className="scroll-mt-4 rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <FolderTree size={15} className="text-indigo-600" />
            <h2 className="text-sm font-bold text-slate-900">{group.name}</h2>
          </div>
          <p className="mt-1 text-xs text-slate-500">{optionForType(group.type).label} · {group.files.length} files · {csvFiles.length} CSV · {documentFiles.length} documents</p>
          {publishing && (
            <p className="mt-1 inline-flex items-center gap-1.5 text-xs font-semibold text-indigo-700">
              <Loader2 size={12} className="animate-spin" /> Publishing to S3
            </p>
          )}
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
            disabled={publishing}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-50"
          >
            <Edit3 size={13} /> Edit
          </button>
          <button
            type="button"
            onClick={() => onDelete(group.id)}
            disabled={publishing}
            className="inline-flex items-center gap-1.5 rounded-lg border border-red-200 bg-white px-2.5 py-1.5 text-xs font-semibold text-red-700 hover:bg-red-50"
          >
            <Trash2 size={13} /> Delete
          </button>
        </div>
      </div>

      {collapsed ? (
        <div className="mt-3 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-500">
          {group.files.length} files hidden · {csvFiles.length} CSV · {documentFiles.length} documents
        </div>
      ) : (
        <>
          <div className="mt-4 grid gap-3 lg:grid-cols-[1fr_1fr]">
            <div>
              <p className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-slate-400">Files in group</p>
              <div className="max-h-40 space-y-1 overflow-auto pr-1">
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
                  {!USE_MOCK
                    ? 'Live S3 mode: saving this group publishes it to S3 and routes CSVs through Glue/Athena.'
                    : currentSummarySourceFiles.length >= 2
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
                <p className="mt-1 text-xs text-slate-500">
                  {USE_MOCK
                    ? 'Review instructions, validate the group, preview combined rows, then generate a deterministic row-count summary CSV.'
                    : 'Review instructions and keep the group ready for Glue/Athena cataloging.'}
                </p>
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
              {USE_MOCK ? (
                <>
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
                </>
              ) : (
                <span className="inline-flex items-center rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs font-semibold text-amber-800">
                  Saving this group publishes its current files to S3
                </span>
              )}
              <button
                type="button"
                disabled={!documentFiles.length || analyzing}
                onClick={() => {
                  onAnalyzeDocuments(group)
                  setActiveAction('portfolio')
                }}
                className="inline-flex items-center gap-1.5 rounded-lg border border-emerald-200 bg-white px-3 py-2 text-xs font-semibold text-emerald-700 hover:bg-emerald-50 disabled:cursor-not-allowed disabled:border-slate-200 disabled:text-slate-300"
              >
                {analyzing ? <Loader2 size={13} className="animate-spin" /> : <ClipboardList size={13} />}
                Analyze project documents
              </button>
              {analysis && (
                <button
                  type="button"
                  onClick={() => setActiveAction('portfolio')}
                  className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-xs font-semibold ${activeAction === 'portfolio' ? 'bg-slate-900 text-white' : 'border border-slate-200 bg-white text-slate-700 hover:bg-slate-50'}`}
                >
                  <Eye size={13} /> View portfolio analysis
                </button>
              )}
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

            {USE_MOCK && activeAction === 'validate' && (
              <div className="mt-3 grid gap-2 md:grid-cols-2">
                {validation.map(check => (
                  <div key={check.label} className={`rounded-lg border p-3 ${check.status === 'pass' ? 'border-emerald-200 bg-emerald-50' : check.status === 'fail' ? 'border-red-200 bg-red-50' : 'border-amber-200 bg-amber-50'}`}>
                    <p className={`text-xs font-bold ${check.status === 'pass' ? 'text-emerald-800' : check.status === 'fail' ? 'text-red-800' : 'text-amber-800'}`}>{check.label}</p>
                    <p className={`mt-1 text-xs ${check.status === 'pass' ? 'text-emerald-700' : check.status === 'fail' ? 'text-red-700' : 'text-amber-700'}`}>{check.detail}</p>
                  </div>
                ))}
              </div>
            )}

            {USE_MOCK && activeAction === 'preview' && (
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
                <p className="border-t border-slate-100 px-3 py-2 text-xs text-slate-500">Previewing first {previewRows.length} combined source rows. Generate summary CSV counts records per CSV and totals them.</p>
              </div>
            )}

            {USE_MOCK && activeAction === 'summary' && (
              <div className="mt-3 rounded-lg border border-indigo-200 bg-white p-3">
                <p className="text-xs font-bold text-indigo-800">{summary ? 'Summary CSV generated' : 'Generating summary CSV'}</p>
                <p className="mt-1 text-xs text-slate-500">
                  {summary
                    ? missingCsvFileCount
                      ? `${summaryFile} cannot produce a real count yet: ${missingCsvFileCount} CSV files need loaded contents.`
                      : `${summaryFile} contains per-file record counts from ${summarySourceFileCount} CSV files and ${summarySourceRowCount} loaded source rows.`
                    : 'Click Generate summary CSV to count loaded records across CSV files.'}
                </p>
              </div>
            )}
            {activeAction === 'portfolio' && (
              <div className="mt-3 rounded-lg border border-emerald-200 bg-white p-3">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <p className="text-xs font-bold text-emerald-800">Project portfolio analysis</p>
                    <p className="mt-1 text-xs text-slate-500">
                      {analysis
                        ? `${analysis.documentCount} document${analysis.documentCount === 1 ? '' : 's'} analyzed.`
                        : analyzing
                        ? `Analyzing ${documentFiles.length} project document${documentFiles.length === 1 ? '' : 's'} now.`
                        : documentFiles.length
                        ? 'Click Analyze project documents to create a deterministic portfolio report.'
                        : 'No .txt, .md, .json, or .pdf files are available in this group.'}
                    </p>
                  </div>
                  {analysis?.markdown && (
                    <button
                      type="button"
                      onClick={() => downloadText(`${group.name}-portfolio-analysis.md`, analysis.markdown, 'text/markdown')}
                      className="inline-flex items-center gap-1.5 rounded-lg border border-emerald-200 bg-emerald-50 px-2.5 py-1.5 text-xs font-semibold text-emerald-700 hover:bg-emerald-100"
                    >
                      <Download size={13} /> Markdown
                    </button>
                  )}
                </div>
                {analysisError && (
                  <div className="mt-3 rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700">
                    {analysisError}
                  </div>
                )}
                {analyzing && (
                  <div className="mt-3 inline-flex items-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs font-semibold text-emerald-800">
                    <Loader2 size={13} className="animate-spin" /> Reading documents from S3 and building the deterministic report.
                  </div>
                )}
                {analysis && (
                  <div className="mt-3 space-y-3">
                    <div className="grid gap-2 md:grid-cols-3">
                      <div className="rounded-lg border border-slate-200 bg-slate-50 p-2 text-xs">
                        <p className="text-slate-400">Documents</p>
                        <p className="font-semibold text-slate-900">{analysis.documentCount}</p>
                      </div>
                      <div className="rounded-lg border border-slate-200 bg-slate-50 p-2 text-xs">
                        <p className="text-slate-400">Potential overlaps</p>
                        <p className="font-semibold text-slate-900">{analysis.overlaps?.length || 0}</p>
                      </div>
                      <div className="rounded-lg border border-slate-200 bg-slate-50 p-2 text-xs">
                        <p className="text-slate-400">Skipped files</p>
                        <p className="font-semibold text-slate-900">{analysis.skipped?.length || 0}</p>
                      </div>
                    </div>
                    <div className="overflow-auto rounded-lg border border-slate-200">
                      <table className="min-w-full text-left text-xs">
                        <thead className="bg-slate-50 text-slate-500">
                          <tr>
                            <th className="px-3 py-2 font-semibold">Project</th>
                            <th className="px-3 py-2 font-semibold">Risk</th>
                            <th className="px-3 py-2 font-semibold">Goal</th>
                            <th className="px-3 py-2 font-semibold">Missing</th>
                          </tr>
                        </thead>
                        <tbody>
                          {(analysis.projects || []).map(project => (
                            <tr key={project.key} className="border-t border-slate-100">
                              <td className="max-w-[220px] px-3 py-2 font-medium text-slate-800">{project.title}</td>
                              <td className="px-3 py-2 capitalize text-slate-700">{project.riskLevel}</td>
                              <td className="max-w-[320px] truncate px-3 py-2 text-slate-600" title={project.goals?.[0] || ''}>{project.goals?.[0] || 'Not stated'}</td>
                              <td className="max-w-[240px] truncate px-3 py-2 text-slate-600" title={(project.missingInformation || []).join(', ')}>{(project.missingInformation || []).join(', ') || 'None'}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    <div className="rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs text-slate-700">
                      <p className="font-semibold text-slate-900">Recommended action plan</p>
                      <ul className="mt-2 list-disc space-y-1 pl-4">
                        {(analysis.actionPlan || []).map(item => <li key={item}>{item}</li>)}
                      </ul>
                    </div>
                  </div>
                )}
              </div>
            )}
            {USE_MOCK && loadedCsvCount < csvFiles.length && (
              <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
                {loadedCsvCount} of {csvFiles.length} CSV files have loaded row content. Use Load CSV contents and select the CSV files in this group before generating a real record count.
              </div>
            )}
          </div>

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
  const navigate = useNavigate()
  const [projectName, setProjectName] = useState('Vendor Audit June 2026')
  const [files, setFiles] = useState([])
  const [filesTruncated, setFilesTruncated] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [summaries, setSummaries] = useState({})
  const [documentAnalyses, setDocumentAnalyses] = useState({})
  const [metadata, setMetadata] = useState(null)
  const [persistedProject, setPersistedProject] = useState(null)
  const [metadataLedger, setMetadataLedger] = useState({ version: '0.1', projects: {} })
  const [metadataWrite, setMetadataWrite] = useState(null)
  const [s3Materialize, setS3Materialize] = useState(null)
  const [groups, setGroups] = useState([])
  const [groupsLoaded, setGroupsLoaded] = useState(false)
  const [materializing, setMaterializing] = useState(false)
  const [crawling, setCrawling] = useState(false)
  const [publishingGroupIds, setPublishingGroupIds] = useState([])
  const [analyzingGroupIds, setAnalyzingGroupIds] = useState([])
  const [analysisErrors, setAnalysisErrors] = useState({})
  const [uploadMessage, setUploadMessage] = useState('')
  const [editingGroupId, setEditingGroupId] = useState(null)
  const [draftName, setDraftName] = useState('')
  const [draftType, setDraftType] = useState('special_project')
  const [draftKeys, setDraftKeys] = useState([])
  const [collapsedGroupIds, setCollapsedGroupIds] = useState([])
  const [fileSearch, setFileSearch] = useState('')
  const [fileStatusFilter, setFileStatusFilter] = useState('available')
  const [fileTypeFilter, setFileTypeFilter] = useState('all')
  const [showSavedGroups, setShowSavedGroups] = useState(false)
  const [selectedManageGroupId, setSelectedManageGroupId] = useState('')
  const [selectedManageFileKeys, setSelectedManageFileKeys] = useState([])
  const [groupFileSearch, setGroupFileSearch] = useState('')
  const [groupReadiness, setGroupReadiness] = useState({})
  const autoStatusKeyRef = useRef('')

  const projectId = slugify(projectName)
  const processedPrefix = 'projects/'
  const fileMap = useMemo(() => new Map(files.map(file => [fileKey(file), file])), [files])
  const activeGroups = useMemo(
    () => groups.filter(group => group.projectId === projectId),
    [groups, projectId],
  )
  const hydratedGroups = useMemo(() => (
    activeGroups.map(group => ({
      ...group,
      fileKeys: groupFileKeys(group),
      files: groupFileKeys(group)
        .map(key => fileMap.get(key) || (group.files || []).find(file => fileKey(file) === key))
        .filter(Boolean),
    }))
  ), [activeGroups, fileMap])
  const queryableGroups = useMemo(() => (
    groups.map(group => ({
      ...group,
      fileKeys: groupFileKeys(group),
      files: groupFileKeys(group)
        .map(key => fileMap.get(key) || (group.files || []).find(file => fileKey(file) === key))
        .filter(Boolean),
    })).sort((a, b) => (
      dateSortValue(b.updatedAt) - dateSortValue(a.updatedAt)
      || String(a.name || '').localeCompare(String(b.name || ''))
    ))
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
  const canSelectDraftFiles = Boolean(draftName.trim())
  const isLockedFileKey = (key) => {
    if (!key) return false
    const assignedGroup = assignedGroupByKey.get(key)
    return Boolean(assignedGroup && assignedGroup.id !== editingGroupId)
  }
  const selectedDraftFiles = useMemo(() => files.filter(file => draftKeySet.has(fileKey(file))), [files, draftKeySet])
  const csvCount = files.filter(file => /\.csv$/i.test(file.name || '')).length
  const csvDraftCount = selectedDraftFiles.filter(file => /\.csv$/i.test(file.name || '')).length
  const documentCount = files.filter(file => isDocumentAnalysisFile(file)).length
  const groupedFileCount = new Set(queryableGroups.flatMap(group => group.fileKeys || [])).size
  const filteredFiles = useMemo(() => {
    const query = fileSearch.trim().toLowerCase()
    return files.filter(file => {
      const key = fileKey(file)
      const assignedGroup = assignedGroupByKey.get(key)
      const status = draftKeySet.has(key) ? 'selected' : isLockedFileKey(key) ? 'grouped' : 'available'
      if (fileStatusFilter === 'available' && status === 'grouped') return false
      if (fileStatusFilter !== 'all' && fileStatusFilter !== 'available' && status !== fileStatusFilter) return false
      if (fileTypeFilter !== 'all' && fileKind(file) !== fileTypeFilter) return false
      if (!query) return true
      return `${file.name || ''} ${file.key || ''} ${assignedGroup?.name || ''}`.toLowerCase().includes(query)
    })
  }, [files, assignedGroupByKey, draftKeySet, fileSearch, fileStatusFilter, fileTypeFilter, editingGroupId])
  const visibleSelectableCount = filteredFiles.filter(file => !isLockedFileKey(fileKey(file))).length
  const visibleUncheckedAddableCount = filteredFiles.filter(file => {
    const key = fileKey(file)
    return !draftKeySet.has(key) && !isLockedFileKey(key)
  }).length
  const visibleSelectedCount = filteredFiles.filter(file => draftKeySet.has(fileKey(file))).length
  const addableFileCount = files.filter(file => !isLockedFileKey(fileKey(file))).length
  const uncheckedAddableFileCount = files.filter(file => {
    const key = fileKey(file)
    return !draftKeySet.has(key) && !isLockedFileKey(key)
  }).length
  const selectedManageGroup = useMemo(() => (
    queryableGroups.find(group => group.id === selectedManageGroupId) || queryableGroups[0] || null
  ), [queryableGroups, selectedManageGroupId])
  const selectedManageFileKeySet = useMemo(() => new Set(selectedManageFileKeys), [selectedManageFileKeys])
  const visibleGroupFiles = useMemo(() => {
    const query = groupFileSearch.trim().toLowerCase()
    const groupFiles = newestFilesFirst((selectedManageGroup?.files || []).filter(file => !file.summary))
    if (!query) return groupFiles
    return groupFiles.filter(file => `${file.name || ''} ${file.key || ''}`.toLowerCase().includes(query))
  }, [selectedManageGroup, groupFileSearch])
  const selectedManageCsvCount = (selectedManageGroup?.files || []).filter(file => !file.summary && /\.csv$/i.test(file.name || '')).length
  const selectedManageDocCount = (selectedManageGroup?.files || []).filter(file => !file.summary && !/\.csv$/i.test(file.name || '')).length
  const visibleGroupFileKeys = visibleGroupFiles.map(file => fileKey(file)).filter(Boolean)
  const visibleSelectedGroupFileCount = visibleGroupFileKeys.filter(key => selectedManageFileKeySet.has(key)).length
  const selectedGroupReadiness = selectedManageGroup ? groupReadiness[selectedManageGroup.id] : null
  const selectedGroupStatusTone = groupStatusTone(selectedGroupReadiness)

  async function loadFiles() {
    setLoading(true)
    setError('')
    try {
      const data = await listUploadedFiles('processed')
      setFiles(data.files || [])
      setFilesTruncated(Boolean(data.truncated))
    } catch (err) {
      setError(err.message || 'Unable to list processed files')
    } finally {
      setLoading(false)
    }
  }

  async function loadPersistedProject() {
    try {
      const data = await getDataGroupingProject(projectId)
      setPersistedProject(data)
    } catch {
      setPersistedProject(null)
    }
  }

  useEffect(() => {
    loadFiles()
    try {
      const savedGroups = JSON.parse(localStorage.getItem(GROUPS_STORAGE_KEY) || '[]')
      if (Array.isArray(savedGroups)) {
        const restoredGroups = savedGroups.map(stripRetiredGroupFields)
        setGroups(restoredGroups)
        setCollapsedGroupIds(restoredGroups.map(group => group.id).filter(Boolean))
      }
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
    loadPersistedProject()
  }, [projectId])

  useEffect(() => {
    if (groupsLoaded) persistGroups(groups)
  }, [groups, groupsLoaded])

  useEffect(() => {
    const validGroupIds = new Set(hydratedGroups.map(group => group.id))
    setSummaries(prev => Object.fromEntries(Object.entries(prev).filter(([groupId]) => validGroupIds.has(groupId))))
    setDocumentAnalyses(prev => Object.fromEntries(Object.entries(prev).filter(([groupId]) => validGroupIds.has(groupId))))
    setAnalysisErrors(prev => Object.fromEntries(Object.entries(prev).filter(([groupId]) => validGroupIds.has(groupId))))
  }, [hydratedGroups])

  useEffect(() => {
    if (!queryableGroups.length) {
      setSelectedManageGroupId('')
      setSelectedManageFileKeys([])
      return
    }
    const groupExists = queryableGroups.some(group => group.id === selectedManageGroupId)
    if (!groupExists) {
      setSelectedManageGroupId(queryableGroups[0].id)
      setSelectedManageFileKeys([])
    }
  }, [queryableGroups, selectedManageGroupId])

  useEffect(() => {
    if (!selectedManageGroup) {
      setSelectedManageFileKeys([])
      return
    }
    const validFileKeys = new Set((selectedManageGroup.files || []).map(file => fileKey(file)).filter(Boolean))
    setSelectedManageFileKeys(prev => prev.filter(key => validFileKeys.has(key)))
  }, [selectedManageGroup])

  useEffect(() => {
    if (!selectedManageGroup?.id) return
    const fileCount = (selectedManageGroup.files || []).filter(file => !file.summary && fileKey(file)).length
    const statusKey = `${selectedManageGroup.id}|${selectedManageGroup.updatedAt || ''}|${fileCount}`
    if (autoStatusKeyRef.current === statusKey) return
    autoStatusKeyRef.current = statusKey
    checkGroupReadiness(selectedManageGroup)
  }, [selectedManageGroup?.id, selectedManageGroup?.updatedAt, selectedManageGroup?.files?.length])

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

  function startNewProject() {
    const stamp = new Date().toISOString().replace(/[-:]/g, '').slice(0, 13)
    setProjectName(`New Project ${stamp}`)
    setMetadata(null)
    setPersistedProject(null)
    setS3Materialize(null)
    setUploadMessage('New project started. It has no groups until files are added through Data Pipeline.')
    setSelectedManageGroupId('')
    setSelectedManageFileKeys([])
    setGroupFileSearch('')
    resetDraft('special_project')
  }

  function changeDraftType(type) {
    setDraftType(type)
  }

  function removeDraftFile(file) {
    const key = fileKey(file)
    setMetadata(null)
    setDraftKeys(prev => prev.filter(item => item !== key))
  }

  function toggleDraftFile(file) {
    if (!canSelectDraftFiles) return
    const key = fileKey(file)
    if (isLockedFileKey(key) && !draftKeySet.has(key)) return
    setMetadata(null)
    setDraftKeys(prev => (
      prev.includes(key)
        ? prev.filter(item => item !== key)
        : [...prev, key]
    ))
  }

  function selectFilesForDraft(nextFiles) {
    if (!canSelectDraftFiles) return
    const keys = nextFiles
      .map(file => fileKey(file))
      .filter(key => key && !isLockedFileKey(key))
    if (!keys.length) return
    setMetadata(null)
    setDraftKeys(prev => [...new Set([...prev, ...keys])])
  }

  function selectVisibleAddableFiles() {
    selectFilesForDraft(filteredFiles)
  }

  function selectAllAddableFiles() {
    selectFilesForDraft(files)
  }

  function clearDraftFiles() {
    setMetadata(null)
    setDraftKeys([])
  }

  function groupPublishPayload(group) {
    return {
      id: group.id,
      name: group.name,
      type: group.type,
      groupProfile: group.groupProfile || buildGroupProfile(group),
      files: (group.files || [])
        .filter(file => !file.summary)
        .map(file => ({
          key: file.key,
          name: file.name,
          size: file.size,
          last_modified: file.last_modified,
        })),
    }
  }

  async function publishGroupToS3(group) {
    if (!group?.id) return
    const targetProjectName = group.projectName || projectName
    const targetProjectId = group.projectId || slugify(targetProjectName)
    setPublishingGroupIds(prev => (prev.includes(group.id) ? prev : [...prev, group.id]))
    setError('')
    try {
      const result = await materializeDataGroupingProject({
        projectName: targetProjectName,
        projectId: targetProjectId,
        groups: [groupPublishPayload(group)],
        move: false,
      })
      setS3Materialize(result)
      setPersistedProject(result.metadata ? {
        ...result.metadata,
        exists: true,
        assignedSourceKeys: (result.metadata.groups || [])
          .flatMap(item => (item.files || []).map(file => file.sourceKey).filter(Boolean)),
      } : persistedProject)
      const tableHints = [...new Set((result.structuredCopies || []).map(copy => copy.glueTableHint).filter(Boolean))]
      setUploadMessage(`${group.name} published to S3. ${result.structuredCopies?.length || 0} CSV file${result.structuredCopies?.length === 1 ? '' : 's'} mirrored into ${tableHints.length || 0} Glue-ready table folder${tableHints.length === 1 ? '' : 's'}${result.crawlerStarted ? `; crawler ${result.crawlerMessage || 'started'}.` : '.'}`)
    } catch (err) {
      setError(err.message || `Unable to publish ${group.name}`)
    } finally {
      setPublishingGroupIds(prev => prev.filter(groupId => groupId !== group.id))
    }
  }

  async function removeGroupFromS3(group) {
    if (!group?.id) return
    const targetProjectName = group.projectName || projectName
    const targetProjectId = group.projectId || slugify(targetProjectName)
    setPublishingGroupIds(prev => (prev.includes(group.id) ? prev : [...prev, group.id]))
    setError('')
    try {
      const result = await materializeDataGroupingProject({
        projectName: targetProjectName,
        projectId: targetProjectId,
        deleteGroups: [groupPublishPayload(group)],
        move: false,
      })
      setS3Materialize(result)
      setPersistedProject(result.metadata ? {
        ...result.metadata,
        exists: true,
        assignedSourceKeys: (result.metadata.groups || [])
          .flatMap(item => (item.files || []).map(file => file.sourceKey).filter(Boolean)),
      } : persistedProject)
      setUploadMessage(`${group.name} removed from S3 project storage${result.crawlerStarted ? `; crawler ${result.crawlerMessage || 'started'}.` : '.'}`)
    } catch (err) {
      setError(err.message || `Unable to remove ${group.name} from S3`)
    } finally {
      setPublishingGroupIds(prev => prev.filter(groupId => groupId !== group.id))
    }
  }

  function removeGroupLocally(groupId) {
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
    setDocumentAnalyses(prev => {
      const next = { ...prev }
      delete next[groupId]
      return next
    })
    setAnalysisErrors(prev => {
      const next = { ...prev }
      delete next[groupId]
      return next
    })
    setGroupReadiness(prev => {
      const next = { ...prev }
      delete next[groupId]
      return next
    })
    setSelectedManageFileKeys([])
    if (selectedManageGroupId === groupId) setSelectedManageGroupId('')
    if (editingGroupId === groupId) resetDraft()
  }

  async function deleteGroup(groupId) {
    const groupToDelete = queryableGroups.find(group => group.id === groupId)
      || hydratedGroups.find(group => group.id === groupId)
    if (!groupToDelete) return
    const confirmed = window.confirm(`Delete the ${groupToDelete.name} group? This removes the group metadata and Glue-ready structured folders. Source files remain in /processed and can be regrouped.`)
    if (!confirmed) return

    setError('')
    setUploadMessage('')
    setPublishingGroupIds(prev => (prev.includes(groupId) ? prev : [...prev, groupId]))
    try {
      const targetProjectName = groupToDelete.projectName || projectName
      const targetProjectId = groupToDelete.projectId || slugify(targetProjectName)
      const result = await materializeDataGroupingProject({
        projectName: targetProjectName,
        projectId: targetProjectId,
        deleteGroups: [groupPublishPayload(groupToDelete)],
        move: false,
      })
      setS3Materialize(result)
      setPersistedProject(result.metadata ? {
        ...result.metadata,
        exists: true,
        assignedSourceKeys: (result.metadata.groups || [])
          .flatMap(item => (item.files || []).map(file => file.sourceKey).filter(Boolean)),
      } : persistedProject)
      removeGroupLocally(groupId)
      setUploadMessage(`${groupToDelete.name} deleted. Source files remain in /processed and can be assigned to another group.`)
    } catch (err) {
      setError(err.message || `Unable to delete ${groupToDelete.name}`)
    } finally {
      setPublishingGroupIds(prev => prev.filter(item => item !== groupId))
    }
  }

  function saveGroup() {
    if (!draftName.trim() || !selectedDraftFiles.length) return
    const savedFileKeys = selectedDraftFiles.map(file => fileKey(file))
    const nextGroup = {
      id: editingGroupId || `${slugify(draftName)}-${Date.now()}`,
      projectId,
      projectName,
      name: draftName.trim().replace(/\s+/g, '_'),
      type: draftType,
      fileKeys: savedFileKeys,
      files: selectedDraftFiles,
      groupProfile: buildGroupProfile({ name: draftName.trim().replace(/\s+/g, '_'), files: selectedDraftFiles }),
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
    if (!editingGroupId) {
      setCollapsedGroupIds(prev => (prev.includes(nextGroup.id) ? prev : [...prev, nextGroup.id]))
    }
    publishGroupToS3(nextGroup)
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

  function toggleManageFile(file) {
    const key = fileKey(file)
    if (!key) return
    setSelectedManageFileKeys(prev => (
      prev.includes(key)
        ? prev.filter(item => item !== key)
        : [...prev, key]
    ))
  }

  function selectVisibleGroupFiles() {
    setSelectedManageFileKeys(prev => [...new Set([...prev, ...visibleGroupFileKeys])])
  }

  function clearSelectedGroupFiles() {
    setSelectedManageFileKeys([])
  }

  function releaseSelectedGroupFiles() {
    if (!selectedManageGroup || !selectedManageFileKeys.length) return
    const releaseKeys = new Set(selectedManageFileKeys)
    let nextGroup = null
    setMetadata(null)
    setGroups(prev => {
      const nextGroups = prev.map(group => {
        if (group.id !== selectedManageGroup.id) return group
        const nextFiles = (group.files || []).filter(file => !releaseKeys.has(fileKey(file)))
        nextGroup = {
          ...group,
          fileKeys: groupFileKeys(group).filter(key => !releaseKeys.has(key)),
          files: nextFiles,
          groupProfile: buildGroupProfile({ ...group, files: nextFiles }),
          updatedAt: new Date().toISOString(),
        }
        return nextGroup
      })
      persistGroups(nextGroups)
      return nextGroups
    })
    setSelectedManageFileKeys([])
    setUploadMessage(`${releaseKeys.size} file${releaseKeys.size === 1 ? '' : 's'} released from ${selectedManageGroup.name}.`)
    if (nextGroup) publishGroupToS3(nextGroup)
  }

  async function checkGroupReadiness(group) {
    if (!group?.id) return
    const groupFiles = (group.files || []).filter(file => !file.summary && fileKey(file))
    const sampledFiles = groupFiles.slice(0, GROUP_STATUS_SAMPLE_LIMIT)
    const sampledCsvCount = sampledFiles.filter(file => /\.csv$/i.test(file.name || '')).length
    const totalCsvCount = groupFiles.filter(file => /\.csv$/i.test(file.name || '')).length
    setGroupReadiness(prev => ({
      ...prev,
      [group.id]: {
        state: 'checking',
        checkedAt: new Date().toISOString(),
        message: `Checking ${sampledFiles.length}${groupFiles.length > sampledFiles.length ? ` of ${groupFiles.length}` : ''} files...`,
      },
    }))
    if (!groupFiles.length) {
      setGroupReadiness(prev => ({
        ...prev,
        [group.id]: {
          state: 'red',
          checkedAt: new Date().toISOString(),
          message: 'No files found in this group.',
          totalFiles: 0,
        },
      }))
      return
    }
    try {
      try {
        const remote = await listDataGroupingProjects()
        const remoteGroup = findRemoteGroupRecord(group, remote?.groups || [])
        const remoteReadiness = readinessFromRemoteGroup(group, remoteGroup, groupFiles.length, totalCsvCount)
        if (remoteReadiness) {
          setGroupReadiness(prev => ({
            ...prev,
            [group.id]: {
              state: remoteReadiness.state,
              checkedAt: new Date().toISOString(),
              message: remoteReadiness.message,
              totalFiles: groupFiles.length,
              sampledFiles: 0,
              totalCsvCount,
              sampledCsvCount: 0,
              readyCount: remoteReadiness.state === 'green' ? groupFiles.length : 0,
              processingCount: remoteReadiness.state === 'yellow' ? Math.max(0, groupFiles.length - Number(remoteGroup?.fileCount || 0)) : 0,
              failedCount: 0,
              remoteFileCount: Number(remoteGroup?.fileCount || 0),
              remoteCsvCount: Number(remoteGroup?.csvCount || 0),
              remoteTableCount: Number(remoteGroup?.tableCount || 0),
              remoteExpectedTableCount: Number(remoteGroup?.expectedTableCount || 0),
              remotePendingTableCount: (remoteGroup?.pendingTableHints || []).length,
            },
          }))
          return
        }
      } catch {
        // Fall back to per-upload status checks when project metadata is unavailable.
      }
      const statuses = await Promise.all(sampledFiles.map(async file => {
        if (fileHasMaterializedEvidence(file)) {
          return { file, status: { status: 'materialized', processed: { exists: true }, structured: file.glueTableHint || file.structuredKey ? { exists: true } : null } }
        }
        try {
          return { file, status: await getUploadStatus(fileKey(file)) }
        } catch (err) {
          const message = err.message || 'status check failed'
          if (/outside caller upload prefix|outside allowed processed prefixes|403/i.test(message)) {
            return { file, status: { status: 'saved_group_file', processed: { exists: true }, inferred: true } }
          }
          return { file, error: message }
        }
      }))
      const failed = statuses.filter(item => item.error)
      const ready = statuses.filter(({ file, status }) => {
        if (!status) return false
        if (status.status === 'materialized') return true
        if (status.status === 'saved_group_file') return true
        if (/\.csv$/i.test(file.name || '')) {
          return Boolean(status.structured?.exists || status.status === 'catalog_done')
        }
        return Boolean(status.processed?.exists || status.raw?.exists)
      })
      const uncertain = statuses.filter(({ file, status }) => (
        status?.status === 'catalog_failed'
        || (/\.csv$/i.test(file.name || '') && status && !status.structured?.exists && status.status !== 'catalog_done' && status.status !== 'materialized' && status.status !== 'saved_group_file')
      ))
      const processing = statuses.length - ready.length - failed.length
      const hasUnsampledFiles = groupFiles.length > sampledFiles.length
      const state = failed.length ? 'red' : uncertain.length || processing ? 'yellow' : 'green'
      const message = failed.length
        ? `${failed.length} sampled file${failed.length === 1 ? '' : 's'} need attention.`
        : uncertain.length
          ? `${ready.length}/${statuses.length} sampled files ready; ${uncertain.length} need indexing confirmation.`
        : processing
          ? `${ready.length}/${statuses.length} sampled files ready; ${processing} still processing.`
        : hasUnsampledFiles
            ? `${ready.length}/${statuses.length} sampled files ready; ${groupFiles.length - sampledFiles.length} additional files are published but not sampled.`
            : `${ready.length}/${statuses.length} files ready.`
      setGroupReadiness(prev => ({
        ...prev,
        [group.id]: {
          state,
          checkedAt: new Date().toISOString(),
          message,
          totalFiles: groupFiles.length,
          sampledFiles: statuses.length,
          totalCsvCount,
          sampledCsvCount,
          readyCount: ready.length,
          processingCount: processing,
          failedCount: failed.length,
          uncertainCount: uncertain.length,
        },
      }))
    } catch (err) {
      setGroupReadiness(prev => ({
        ...prev,
        [group.id]: {
          state: 'red',
          checkedAt: new Date().toISOString(),
          message: err.message || 'Unable to check group status.',
          totalFiles: groupFiles.length,
        },
      }))
    }
  }

  async function analyzeProjectDocuments(group) {
    const filesForAnalysis = (group.files || [])
      .filter(file => !file.summary && isDocumentAnalysisFile(file))
      .map(file => ({
        key: file.key,
        name: file.name,
        size: file.size,
        last_modified: file.last_modified,
      }))
    if (!filesForAnalysis.length) return
    setError('')
    setUploadMessage(`Analyzing ${filesForAnalysis.length} project document${filesForAnalysis.length === 1 ? '' : 's'} in ${group.name}...`)
    setAnalysisErrors(prev => {
      const next = { ...prev }
      delete next[group.id]
      return next
    })
    setAnalyzingGroupIds(prev => (prev.includes(group.id) ? prev : [...prev, group.id]))
    try {
      const result = await analyzeDataGroupingDocuments({
        groupName: group.name,
        files: filesForAnalysis,
      })
      setDocumentAnalyses(prev => ({ ...prev, [group.id]: result }))
      setUploadMessage(`Portfolio analysis created for ${result.documentCount} document${result.documentCount === 1 ? '' : 's'} in ${group.name}.`)
    } catch (err) {
      const message = err.message || 'Unable to analyze project documents'
      setError(message)
      setAnalysisErrors(prev => ({ ...prev, [group.id]: message }))
    } finally {
      setAnalyzingGroupIds(prev => prev.filter(groupId => groupId !== group.id))
    }
  }

  function querySavedGroup(group) {
    if (!group?.name) return
    const selectedDataGroupId = `local::${group.id || group.name}`
    try {
      sessionStorage.setItem(MCP_CHAT_DRAFT_KEY, JSON.stringify({
        selectedServerId: 'structured',
        selectedDataGroupId,
        activeSessionId: null,
        activeSessionTitle: null,
        messages: [],
      }))
    } catch {
      // If session storage is unavailable, still take the user to chat.
    }
    setShowSavedGroups(false)
    navigate('/mcp-chat')
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
          ? 'Summary not generated: add at least two CSV files first.'
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
          ? 'Instruction guide detected. Verification summary counts loaded data records by CSV before applying higher-order invoice calculations.'
          : 'Count loaded data records across CSV files.',
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

  async function writeProjectToS3() {
    if (!hydratedGroups.length) return
    setMaterializing(true)
    setError('')
    setS3Materialize(null)
    try {
      const payloadGroups = hydratedGroups.map(group => ({
        id: group.id,
        name: group.name,
        type: group.type,
        files: (group.files || [])
          .filter(file => !file.summary)
          .map(file => ({
            key: file.key,
            name: file.name,
            size: file.size,
            last_modified: file.last_modified,
          })),
      }))
      const result = await materializeDataGroupingProject({
        projectName,
        projectId,
        groups: payloadGroups,
        move: false,
      })
      setS3Materialize(result)
      setPersistedProject(result.metadata ? {
        ...result.metadata,
        exists: true,
        assignedSourceKeys: (result.metadata.groups || [])
          .flatMap(item => (item.files || []).map(file => file.sourceKey).filter(Boolean)),
      } : persistedProject)
      setUploadMessage(`All groups republished to S3 at ${result.projectPrefix}. ${result.structuredCopies?.length || 0} CSV file${result.structuredCopies?.length === 1 ? '' : 's'} mirrored for Glue.`)
    } catch (err) {
      setError(err.message || 'Unable to republish groups to S3')
    } finally {
      setMaterializing(false)
    }
  }

  async function startCrawlerIndexing() {
    setCrawling(true)
    setError('')
    try {
      const result = await startDataGroupingCrawler()
      setS3Materialize(prev => ({ ...(prev || {}), ...result }))
      setUploadMessage(`Glue/Athena indexing ${result.crawlerMessage === 'already_running' ? 'is already running' : 'started'} with ${result.crawlerName}.`)
    } catch (err) {
      setError(err.message || 'Unable to start Glue crawler')
    } finally {
      setCrawling(false)
    }
  }

  function collapseAllGroups() {
    setCollapsedGroupIds(hydratedGroups.map(group => group.id).filter(Boolean))
  }

  function expandAllGroups() {
    setCollapsedGroupIds([])
  }

  return (
    <div className="page-container space-y-6 p-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-500">Processed data intelligence</p>
          <h1 className="mt-1 text-lg font-bold tracking-tight text-slate-900">Data Grouping</h1>
          <p className="mt-1 max-w-3xl text-xs text-slate-500">
            Inspect existing data groups, review their files, query them with Structured Data Specialist, or release files back out of a group.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex flex-wrap items-center gap-2 rounded-xl border border-slate-200 bg-white p-2 shadow-sm">
            <span className="px-1 text-[10px] font-semibold uppercase tracking-wider text-slate-400">Automatic publishing</span>
            <button
              type="button"
              onClick={loadFiles}
              disabled={loading}
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-60"
            >
              {loading ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
              Refresh /processed files
            </button>
            <button
              type="button"
              onClick={writeProjectToS3}
              disabled={materializing || !hydratedGroups.length}
              className="inline-flex items-center gap-1.5 rounded-lg bg-slate-900 px-3 py-2 text-xs font-semibold text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-200 disabled:text-slate-500"
            >
              {materializing ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />}
              Republish all groups
            </button>
            <button
              type="button"
              onClick={startCrawlerIndexing}
              disabled={crawling || !hydratedGroups.length}
              className="inline-flex items-center gap-1.5 rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-2 text-xs font-semibold text-indigo-700 hover:bg-indigo-100 disabled:cursor-not-allowed disabled:border-slate-200 disabled:bg-slate-100 disabled:text-slate-400"
            >
              {crawling ? <Loader2 size={13} className="animate-spin" /> : <Database size={13} />}
              Re-index Athena
            </button>
            <button
              type="button"
              onClick={() => setShowSavedGroups(true)}
              disabled={!queryableGroups.length}
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-300"
            >
              <MessageSquare size={13} /> Query saved group
            </button>
          </div>
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-4">
        <Stat label="Saved groups" value={queryableGroups.length} />
        <Stat label="Grouped files" value={groupedFileCount} />
        <Stat label="Selected CSV" value={selectedManageCsvCount} />
        <Stat label="Selected docs" value={selectedManageDocCount} />
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

      {s3Materialize && (
        <div className="rounded-xl border border-indigo-200 bg-indigo-50 p-4 text-sm text-indigo-900">
          <p className="font-semibold">S3 project materialized</p>
          <div className="mt-2 space-y-1 font-mono text-xs">
            <p>s3://{s3Materialize.bucket}/{s3Materialize.projectPrefix}</p>
            <p>s3://{s3Materialize.bucket}/{s3Materialize.metadataKey}</p>
            {s3Materialize.crawlerStarted && <p>Glue crawler: {s3Materialize.crawlerMessage || 'started'}</p>}
          </div>
        </div>
      )}

      {error && (
        <div className="flex items-center gap-2 rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700">
          <AlertTriangle size={16} /> {error}
        </div>
      )}

      {showSavedGroups && (
        <div
          className="fixed inset-0 z-50 flex items-start justify-center overflow-auto bg-slate-900/35 p-4 pt-16"
          onClick={(event) => {
            if (event.target === event.currentTarget) setShowSavedGroups(false)
          }}
        >
          <section className="w-full max-w-3xl rounded-xl border border-slate-200 bg-white shadow-2xl">
            <div className="flex items-start justify-between gap-3 border-b border-slate-100 p-4">
              <div>
                <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">Saved groups</p>
                <h2 className="mt-1 text-sm font-bold text-slate-900">Query a saved data group</h2>
                <p className="mt-1 text-xs text-slate-500">Choose a group to open MCP Chat with Structured Data Specialist and that group selected.</p>
              </div>
              <button
                type="button"
                onClick={() => setShowSavedGroups(false)}
                className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-slate-200 bg-white text-slate-500 hover:bg-slate-50"
                aria-label="Close saved groups"
              >
                <XCircle size={15} />
              </button>
            </div>
            <div className="max-h-[60vh] space-y-2 overflow-auto p-4">
              {queryableGroups.map(group => {
                const csvCount = (group.files || []).filter(file => /\.csv$/i.test(file.name || '')).length
                const documentCount = (group.files || []).length - csvCount
                return (
                  <div key={group.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-slate-200 bg-slate-50 p-3">
                    <div className="min-w-0">
                      <p className="truncate text-sm font-semibold text-slate-900">{group.name}</p>
                      <p className="mt-1 text-[11px] text-slate-500">
                        {group.projectName || group.projectId || 'Saved group'} · {(group.files || []).length} files · {csvCount} CSV · {documentCount} docs · {formatShortDate(group.updatedAt)}
                      </p>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      <button
                        type="button"
                        onClick={() => querySavedGroup(group)}
                        className="inline-flex items-center gap-1.5 rounded-lg bg-slate-900 px-3 py-2 text-xs font-semibold text-white hover:bg-slate-800"
                      >
                        <MessageSquare size={13} /> Query
                      </button>
                      <button
                        type="button"
                        onClick={() => deleteGroup(group.id)}
                        disabled={publishingGroupIds.includes(group.id)}
                        className="inline-flex items-center gap-1.5 rounded-lg border border-red-200 bg-white px-3 py-2 text-xs font-semibold text-red-600 hover:bg-red-50"
                      >
                        {publishingGroupIds.includes(group.id) ? <Loader2 size={13} className="animate-spin" /> : <Trash2 size={13} />}
                        Delete
                      </button>
                    </div>
                  </div>
                )
              })}
            </div>
          </section>
        </div>
      )}

      <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(320px,0.7fr)]">
          <div>
            <div className="flex items-center justify-between gap-3">
              <label className="text-[10px] font-semibold uppercase tracking-wider text-slate-400" htmlFor="project-name">Project</label>
              <button
                type="button"
                onClick={startNewProject}
                className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-50"
              >
                <Plus size={13} /> New project
              </button>
            </div>
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
                <p className="font-sans text-slate-500">No groups yet</p>
              )}
              <p>{processedPrefix}{projectId}/metadata/project.json</p>
            </div>
          </div>
        </div>
      </section>

      <section id="group-file-manager" className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-sm font-bold text-slate-900">Select an existing group</h2>
            <p className="mt-1 text-xs text-slate-500">Examine the files currently owned by a group, then release selected files if they need to be regrouped.</p>
          </div>
          {selectedManageGroup && (
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={() => checkGroupReadiness(selectedManageGroup)}
                disabled={selectedGroupReadiness?.state === 'checking'}
                title={selectedGroupStatusTone.detail}
                className={`inline-flex items-center gap-1.5 rounded-lg border px-3 py-2 text-xs font-semibold disabled:cursor-wait disabled:opacity-80 ${selectedGroupStatusTone.className}`}
              >
                {selectedGroupReadiness?.state === 'checking' ? (
                  <Loader2 size={13} className="animate-spin" />
                ) : (
                  <span className={`h-2.5 w-2.5 rounded-full ${selectedGroupStatusTone.dotClassName}`} />
                )}
                Status
              </button>
              <button
                type="button"
                onClick={() => querySavedGroup(selectedManageGroup)}
                className="inline-flex items-center gap-1.5 rounded-lg bg-slate-900 px-3 py-2 text-xs font-semibold text-white hover:bg-slate-800"
              >
                <MessageSquare size={13} /> Query group
              </button>
              <button
                type="button"
                onClick={() => publishGroupToS3(selectedManageGroup)}
                disabled={publishingGroupIds.includes(selectedManageGroup.id)}
                className="inline-flex items-center gap-1.5 rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-2 text-xs font-semibold text-indigo-700 hover:bg-indigo-100 disabled:cursor-not-allowed disabled:border-slate-200 disabled:bg-slate-100 disabled:text-slate-400"
              >
                {publishingGroupIds.includes(selectedManageGroup.id) ? <Loader2 size={13} className="animate-spin" /> : <Upload size={13} />}
                Republish group
              </button>
              <button
                type="button"
                onClick={() => deleteGroup(selectedManageGroup.id)}
                disabled={publishingGroupIds.includes(selectedManageGroup.id)}
                className="inline-flex items-center gap-1.5 rounded-lg border border-red-200 bg-white px-3 py-2 text-xs font-semibold text-red-600 hover:bg-red-50 disabled:cursor-not-allowed disabled:border-slate-200 disabled:text-slate-300"
              >
                {publishingGroupIds.includes(selectedManageGroup.id) ? <Loader2 size={13} className="animate-spin" /> : <Trash2 size={13} />}
                Delete group
              </button>
            </div>
          )}
        </div>

        <div className="mt-4 grid gap-3 lg:grid-cols-[minmax(240px,360px)_minmax(0,1fr)]">
          <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
            <label className="text-[10px] font-semibold uppercase tracking-wider text-slate-400" htmlFor="manage-group-select">Group</label>
            <select
              id="manage-group-select"
              value={selectedManageGroup?.id || ''}
              onChange={(event) => {
                setSelectedManageGroupId(event.target.value)
                setSelectedManageFileKeys([])
                setGroupFileSearch('')
              }}
              disabled={!queryableGroups.length}
              className="mt-2 w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-900 outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100 disabled:cursor-not-allowed disabled:text-slate-400"
            >
              {queryableGroups.length ? queryableGroups.map(group => (
                <option key={group.id} value={group.id}>{group.name} ({(group.files || []).filter(file => !file.summary).length} files)</option>
              )) : (
                <option value="">No saved groups</option>
              )}
            </select>
            {selectedManageGroup ? (
              <div className="mt-3 space-y-2 text-xs text-slate-600">
                <div className="rounded-lg border border-slate-200 bg-white p-3">
                  <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">Selected group</p>
                  <p className="mt-1 truncate text-sm font-semibold text-slate-900" title={selectedManageGroup.name}>{selectedManageGroup.name}</p>
                  <p className="mt-1 text-[11px] text-slate-500">
                    {(selectedManageGroup.files || []).filter(file => !file.summary).length} files · {selectedManageCsvCount} CSV · {selectedManageDocCount} docs
                  </p>
                  <p className="mt-2 flex items-center gap-1.5 text-[11px] font-medium text-slate-600">
                    <span className={`h-2 w-2 rounded-full ${selectedGroupStatusTone.dotClassName}`} />
                    {selectedGroupStatusTone.detail}
                  </p>
                </div>
                <p className="leading-5">
                  Releasing a file removes it from this group’s ownership list and republishes the group metadata. The source file remains in /processed.
                </p>
              </div>
            ) : (
              <p className="mt-3 rounded-lg border border-slate-200 bg-white p-3 text-xs text-slate-500">
                Create or select groups from Data Pipeline, then return here to examine them.
              </p>
            )}
          </div>

          <div className="min-w-0 rounded-lg border border-slate-200 bg-white p-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <label className="relative block min-w-[220px] flex-1">
                <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
                <input
                  value={groupFileSearch}
                  onChange={(event) => setGroupFileSearch(event.target.value)}
                  placeholder="Search files in selected group"
                  disabled={!selectedManageGroup}
                  className="w-full rounded-lg border border-slate-200 py-2 pl-9 pr-3 text-xs font-medium text-slate-900 outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100 disabled:cursor-not-allowed disabled:bg-slate-50 disabled:text-slate-400"
                />
              </label>
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={selectVisibleGroupFiles}
                  disabled={!visibleGroupFileKeys.length || visibleSelectedGroupFileCount === visibleGroupFileKeys.length}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-300"
                >
                  <CheckCircle size={13} /> Select visible
                </button>
                <button
                  type="button"
                  onClick={clearSelectedGroupFiles}
                  disabled={!selectedManageFileKeys.length}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-300"
                >
                  <XCircle size={13} /> Clear
                </button>
                <button
                  type="button"
                  onClick={releaseSelectedGroupFiles}
                  disabled={!selectedManageFileKeys.length || publishingGroupIds.includes(selectedManageGroup?.id)}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-red-200 bg-white px-3 py-2 text-xs font-semibold text-red-600 hover:bg-red-50 disabled:cursor-not-allowed disabled:border-slate-200 disabled:text-slate-300"
                >
                  {publishingGroupIds.includes(selectedManageGroup?.id) ? <Loader2 size={13} className="animate-spin" /> : <Trash2 size={13} />}
                  Release selected
                </button>
              </div>
            </div>
            <div className="mt-3 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs font-semibold text-slate-600">
              {visibleGroupFiles.length} shown · {visibleSelectedGroupFileCount} selected
            </div>
            <div className="mt-3 max-h-[520px] space-y-1.5 overflow-auto pr-1">
              {selectedManageGroup ? (
                visibleGroupFiles.length ? visibleGroupFiles.map(file => {
                  const key = fileKey(file)
                  return (
                    <label key={key} className="grid cursor-pointer items-center gap-3 rounded-lg border border-slate-200 bg-white px-3 py-2 hover:border-indigo-200 hover:bg-indigo-50" style={{ gridTemplateColumns: 'auto minmax(0,1fr) auto' }}>
                      <input
                        type="checkbox"
                        checked={selectedManageFileKeySet.has(key)}
                        onChange={() => toggleManageFile(file)}
                        className="h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
                        aria-label={`Select ${file.name}`}
                      />
                      <div className="min-w-0">
                        <p className="truncate text-xs font-semibold text-slate-800" title={file.name}>{file.name}</p>
                        <p className="truncate font-mono text-[10px] text-slate-400" title={file.key}>{file.key}</p>
                      </div>
                      <div className="hidden shrink-0 items-center gap-2 text-[10px] text-slate-500 md:flex">
                        <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-1 font-semibold uppercase text-slate-500">{fileKind(file)}</span>
                        <span>{formatBytes(file.size)}</span>
                        <span>{formatShortDate(file.last_modified)}</span>
                      </div>
                    </label>
                  )
                }) : (
                  <p className="rounded-lg border border-slate-200 bg-white p-3 text-xs text-slate-500">No files match the selected group/search.</p>
                )
              ) : (
                <p className="rounded-lg border border-slate-200 bg-white p-3 text-xs text-slate-500">No saved group selected.</p>
              )}
            </div>
          </div>
        </div>
      </section>

      {false && (
        <>
      <section className="grid gap-4">
        <div id="create-group" className="scroll-mt-4 rounded-xl border border-slate-200 bg-white p-3 shadow-sm">
          <div className="grid items-end gap-3 lg:grid-cols-[minmax(160px,0.7fr)_minmax(240px,1fr)_220px_auto]">
            <div>
              <h2 className="text-sm font-bold text-slate-900">{editingGroupId ? 'Edit group' : 'Create group'}</h2>
              <p className="mt-1 text-[11px] leading-4 text-slate-500">Name first, then check files below.</p>
            </div>
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
            <div className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs font-semibold text-slate-600">
              {draftKeys.length} selected · {csvDraftCount} CSV
            </div>
          </div>

          <div className={`grid gap-3 ${selectedDraftFiles.length ? 'mt-3' : 'mt-2'}`}>
            {selectedDraftFiles.length > 0 ? (
              <div>
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">Files selected</p>
                {draftKeys.length > 0 && (
                  <button
                    type="button"
                    onClick={clearDraftFiles}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-50"
                  >
                    <XCircle size={13} /> Clear
                  </button>
                )}
              </div>
              <div className="space-y-2">
                {selectedDraftFiles.map(file => (
                  <SelectedDraftFileRow
                    key={fileKey(file)}
                    file={file}
                    onRemove={() => removeDraftFile(file)}
                  />
                ))}
              </div>
              </div>
            ) : (
              <p className="text-[11px] leading-4 text-slate-500">
                {canSelectDraftFiles ? 'No files selected yet.' : 'Enter a group name to unlock file selection.'}
              </p>
            )}

            {editingGroupId && (
              <p className="rounded-lg border border-indigo-200 bg-indigo-50 p-3 text-xs text-indigo-700">
                Editing this group releases its current files only inside this editor. Save group to keep changes, or cancel edit to leave the saved group unchanged.
              </p>
            )}

            <div className="flex flex-wrap items-center justify-end gap-2 border-t border-slate-100 pt-3">
              {editingGroupId && (
                <button
                  type="button"
                  onClick={() => resetDraft()}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50"
                >
                  <XCircle size={13} /> Cancel edit
                </button>
              )}
              {!editingGroupId && (draftName || draftKeys.length > 0) && (
                <button
                  type="button"
                  onClick={() => startNewGroup()}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50"
                >
                  <RefreshCw size={13} /> Reset
                </button>
              )}
              <button
                type="button"
                onClick={saveGroup}
                disabled={!draftName.trim() || !draftKeys.length}
                className="inline-flex items-center gap-1.5 rounded-lg bg-slate-900 px-3 py-2 text-xs font-semibold text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-200 disabled:text-slate-500"
              >
                {editingGroupId ? <Save size={13} /> : <Plus size={13} />}
                {editingGroupId ? 'Save group' : 'New group'}
              </button>
            </div>
          </div>
        </div>

      </section>

      <section className="grid gap-4 lg:grid-cols-2">
        <div id="processed-files" className="scroll-mt-4 rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 className="text-sm font-bold text-slate-900">Select files from /processed</h2>
              <p className="mt-1 text-xs text-slate-500">
                {canSelectDraftFiles
                  ? 'Check files into the draft group. A file can belong to only one saved group.'
                  : 'Enter a group name before selecting files.'}
              </p>
            </div>
            <div className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs font-semibold text-slate-600">
              {filteredFiles.length} shown · {visibleSelectableCount} selectable · {visibleSelectedCount} checked
            </div>
          </div>
          <div className="mt-3 grid gap-2 md:grid-cols-[minmax(0,1fr)_auto_auto]">
            <label className="relative block">
              <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
              <input
                value={fileSearch}
                onChange={(event) => setFileSearch(event.target.value)}
                placeholder="Search filename, key, or group"
                className="w-full rounded-lg border border-slate-200 py-2 pl-9 pr-3 text-xs font-medium text-slate-900 outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
              />
            </label>
            <select
              value={fileStatusFilter}
              onChange={(event) => setFileStatusFilter(event.target.value)}
              className="rounded-lg border border-slate-200 px-3 py-2 text-xs font-semibold text-slate-700 outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
            >
              <option value="available">Addable + checked</option>
              <option value="all">All statuses</option>
              <option value="selected">Checked</option>
              <option value="grouped">Grouped</option>
            </select>
            <select
              value={fileTypeFilter}
              onChange={(event) => setFileTypeFilter(event.target.value)}
              className="rounded-lg border border-slate-200 px-3 py-2 text-xs font-semibold text-slate-700 outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
            >
              <option value="all">All types</option>
              <option value="csv">CSV</option>
              <option value="pdf">PDF</option>
              <option value="text">Text</option>
              <option value="json">JSON</option>
              <option value="other">Other</option>
            </select>
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={selectVisibleAddableFiles}
              disabled={!canSelectDraftFiles || !visibleUncheckedAddableCount}
              className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-2 text-xs font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-slate-200 disabled:text-slate-500"
            >
              <Plus size={13} /> Check visible addable
            </button>
            <button
              type="button"
              onClick={selectAllAddableFiles}
              disabled={!canSelectDraftFiles || !uncheckedAddableFileCount}
              className="inline-flex items-center gap-1.5 rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-2 text-xs font-semibold text-indigo-700 hover:bg-indigo-100 disabled:cursor-not-allowed disabled:border-slate-200 disabled:bg-slate-100 disabled:text-slate-400"
            >
              <Plus size={13} /> Check all addable
            </button>
            <button
              type="button"
              onClick={clearDraftFiles}
              disabled={!draftKeys.length}
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-300"
            >
              <XCircle size={13} /> Clear checked
            </button>
          </div>
          {filesTruncated && (
            <p className="mt-2 rounded-lg border border-amber-200 bg-amber-50 p-2 text-xs text-amber-700">
              Showing the newest processed files. Older files are still in S3 but may require search/filtering in a later version.
            </p>
          )}
          <div className="mt-3 max-h-[520px] space-y-1.5 overflow-auto pr-1">
            {filteredFiles.length ? filteredFiles.map(file => {
              const key = fileKey(file)
              const assignedGroup = assignedGroupByKey.get(key)
              const status = draftKeySet.has(key) ? 'selected' : isLockedFileKey(key) ? 'grouped' : 'available'
              return (
                <ProcessedFileRow
                  key={key}
                  file={file}
                  status={status}
                  groupName={assignedGroup?.name}
                  onToggle={toggleDraftFile}
                  disabled={!canSelectDraftFiles}
                />
              )
            }) : (
              <p className="rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs text-slate-500">No processed files match the current filters.</p>
            )}
          </div>
        </div>

        <div id="metadata-preview" className="scroll-mt-4 rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h2 className="text-sm font-bold text-slate-900">Metadata preview</h2>
              <p className="mt-1 text-xs text-slate-500">Stored locally for now; ready to become /processed project metadata.</p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={createMetadata}
                disabled={!hydratedGroups.length}
                className="inline-flex items-center gap-1.5 rounded-lg bg-slate-900 px-3 py-2 text-xs font-semibold text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-200 disabled:text-slate-500"
              >
                <Database size={13} /> Create metadata
              </button>
              <button
                type="button"
                onClick={downloadMetadata}
                className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50"
              >
                <Download size={13} /> JSON
              </button>
            </div>
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
        </>
      )}
    </div>
  )
}
