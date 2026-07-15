import { useState, useEffect, useCallback, useRef } from 'react'
import { API_URL, CHAT_URL, USE_MOCK } from '../config'
import {
  MOCK_CONFLICTS, MOCK_CHANGE_REQUESTS, MOCK_AUDIT, MOCK_TOKEN_USAGE, mockImpactAnalysis,
  MOCK_REPORT_CATALOG, MOCK_REPORT_CATEGORIES, mockGenerateReport, mockDriftScan,
} from '../mockData'
import { authHeaders, refresh, signIn } from './useAuth'

// Wrap every API Gateway call with the Cognito IdToken in the
// Authorization header. On 401, attempt a single token refresh, then
// retry; if refresh fails, redirect to the hosted UI.
async function apiFetch(path, options = {}) {
  const doFetch = (extraAuth) => fetch(`${API_URL}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(extraAuth || authHeaders()),
      ...(options.headers || {}),
    },
  })
  let res = await doFetch()
  if (res.status === 401) {
    const newToken = await refresh()
    if (!newToken) { signIn(); throw new Error('Auth expired') }
    res = await doFetch({ Authorization: `Bearer ${newToken}` })
  }
  const responseText = await res.text()
  let payload = null
  if (responseText) {
    try {
      payload = JSON.parse(responseText)
    } catch {
      payload = null
    }
  }
  if (!res.ok) {
    const detail = payload?.error || payload?.message || responseText || res.statusText
    throw new Error(`${res.status} ${detail}`)
  }
  return payload || {}
}

function joinUrl(base, path) {
  return `${String(base || '').replace(/\/+$/, '')}/${String(path || '').replace(/^\/+/, '')}`
}

async function functionUrlFetch(path, options = {}) {
  const base = CHAT_URL || API_URL
  const doFetch = (extraAuth) => fetch(joinUrl(base, path), {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(extraAuth || authHeaders()),
      ...(options.headers || {}),
    },
  })
  let res = await doFetch()
  if (res.status === 401) {
    const newToken = await refresh()
    if (!newToken) { signIn(); throw new Error('Auth expired') }
    res = await doFetch({ Authorization: `Bearer ${newToken}` })
  }
  const responseText = await res.text()
  let payload = null
  if (responseText) {
    try {
      payload = JSON.parse(responseText)
    } catch {
      payload = null
    }
  }
  if (!res.ok) {
    const detail = payload?.error || payload?.message || responseText || res.statusText
    throw new Error(`${res.status} ${detail}`)
  }
  return payload || {}
}

// Simulated scan delay for mock mode
function sleep(ms) { return new Promise(r => setTimeout(r, ms)) }

// Trigger a browser download for a generated report. Works for both same-origin
// blob URLs (mock mode, where the `download` attribute names the file) and
// cross-origin presigned S3 URLs (live mode, where S3's Content-Disposition
// header forces the attachment download).
export function triggerDownload(url, filename) {
  if (!url) return
  const a = document.createElement('a')
  a.href = url
  if (filename) a.download = filename
  a.target = '_blank'
  a.rel = 'noopener'
  document.body.appendChild(a)
  a.click()
  a.remove()
}

export function useFindings() {
  const [findings, setFindings] = useState([])
  const [loading, setLoading] = useState(false)
  const [scanning, setScanning] = useState(false)

  const load = useCallback(async (filters = {}) => {
    setLoading(true)
    try {
      if (USE_MOCK) {
        await sleep(300)
        let data = [...MOCK_CONFLICTS]
        if (filters.severity) data = data.filter(f => f.severity === filters.severity)
        if (filters.status) data = data.filter(f => f.status === filters.status)
        setFindings(data)
      } else {
        const qs = new URLSearchParams(filters).toString()
        const data = await apiFetch(`/findings${qs ? '?' + qs : ''}`)
        setFindings(data.findings || [])
      }
    } finally { setLoading(false) }
  }, [])

  const runScan = useCallback(async () => {
    setScanning(true)
    try {
      if (USE_MOCK) {
        await sleep(2500)
        setFindings(MOCK_CONFLICTS)
      } else {
        await apiFetch('/scan', { method: 'POST', body: JSON.stringify({ source: 'all' }) })
        await sleep(3000)
        await load()
      }
    } finally { setScanning(false) }
  }, [load])

  return { findings, loading, scanning, load, runScan }
}

export function useChangeRequests() {
  const [changeRequests, setChangeRequests] = useState([])
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      if (USE_MOCK) {
        await sleep(200)
        setChangeRequests(MOCK_CHANGE_REQUESTS)
      } else {
        const data = await apiFetch('/actions')
        setChangeRequests(data.change_requests || [])
      }
    } finally { setLoading(false) }
  }, [])

  const createAction = useCallback(async (payload) => {
    if (USE_MOCK) {
      await sleep(800)
      // Mirror the backend: denormalize the linked finding's team ownership onto
      // the CR so mock mode renders the same routing as live.
      const src = MOCK_CONFLICTS.find(c => c.conflict_id === payload.conflict_id)
      const ownership = src ? {
        owner_team: src.owner_team, consumer_team: src.consumer_team,
        platform_team: src.platform_team, routed_team: src.owner_team,
        tags: src.tags, jira_project_key: 'DEVARBITER',
      } : {}
      const cr = {
        cr_id: `CR-${Date.now()}`,
        status: payload.target_environment === 'DEV' ? 'AUTO_APPROVED' : 'PENDING_APPROVAL',
        ...payload,
        ...ownership,
        created_at: new Date().toISOString(),
        approvers: payload.target_environment === 'PROD' ? [
          { role: 'ciso', email: 'ciso@meridianinsurance.com', status: 'PENDING', description: 'CISO approval required' },
          { role: 'vp_security', email: 'vpe@meridianinsurance.com', status: 'PENDING', description: 'VP Engineering approval required' },
        ] : [],
        total_approvers_needed: 2,
        total_approvals_received: 0,
      }
      setChangeRequests(prev => [cr, ...prev])
      return cr
    }
    const result = await apiFetch('/actions', { method: 'POST', body: JSON.stringify(payload) })
    await load()
    return result
  }, [load])

  const approve = useCallback(async (crId, approverEmail, approverRole, comment) => {
    if (USE_MOCK) {
      await sleep(600)
      setChangeRequests(prev => prev.map(cr => {
        if (cr.cr_id !== crId) return cr
        const newApprovers = cr.approvers.map(a =>
          a.email === approverEmail ? { ...a, status: 'APPROVED' } : a
        )
        const remaining = newApprovers.filter(a => a.type !== 'NOTIFICATION' && a.status === 'PENDING')
        return { ...cr, approvers: newApprovers, status: remaining.length === 0 ? 'APPROVED' : 'PENDING_APPROVAL', total_approvals_received: (cr.total_approvals_received || 0) + 1 }
      }))
      return { status: 'APPROVED' }
    }
    const result = await apiFetch(`/actions/${crId}/approve`, { method: 'POST', body: JSON.stringify({ approver_email: approverEmail, approver_role: approverRole, comment }) })
    await load()  // refresh list so the row immediately reflects the server-side state
    return result
  }, [load])

  const reject = useCallback(async (crId, approverEmail, reason) => {
    if (USE_MOCK) {
      await sleep(400)
      setChangeRequests(prev => prev.map(cr => cr.cr_id === crId ? { ...cr, status: 'REJECTED' } : cr))
      return {}
    }
    const result = await apiFetch(`/actions/${crId}/reject`, { method: 'POST', body: JSON.stringify({ approver_email: approverEmail, reason }) })
    await load()
    return result
  }, [load])

  const execute = useCallback(async (crId) => {
    if (USE_MOCK) {
      await sleep(1500)
      setChangeRequests(prev => prev.map(cr => cr.cr_id === crId ? { ...cr, status: 'COMPLETED', execution_log: [
        `[${new Date().toISOString()}] Execution started`,
        `[${new Date().toISOString()}] Locating target resource...`,
        `[${new Date().toISOString()}] SIMULATION: Remediation action applied`,
        `[${new Date().toISOString()}] Conflict marked as RESOLVED`,
        `[${new Date().toISOString()}] Audit log entry written`,
      ] } : cr))
      return { status: 'COMPLETED', execution_log: [] }
    }
    const result = await apiFetch(`/actions/${crId}/execute`, { method: 'POST', body: JSON.stringify({ executed_by: 'operator@meridianinsurance.com' }) })
    await load()
    return result
  }, [load])

  const escalate = useCallback(async (crId, reason) => {
    if (USE_MOCK) {
      await sleep(300)
      setChangeRequests(prev => prev.map(cr => cr.cr_id === crId ? { ...cr, status: 'ESCALATED' } : cr))
      return {}
    }
    const result = await apiFetch(`/actions/${crId}/escalate`, { method: 'POST', body: JSON.stringify({ reason }) })
    await load()
    return result
  }, [load])

  return { changeRequests, loading, load, createAction, approve, reject, execute, escalate }
}

// In-memory mock for /conversations when USE_MOCK is true. Mirrors the DDB
// shape returned by the api_handler so the UI can render identically in both modes.
const MOCK_SESSIONS = [
  {
    session_id: 'mock-sess-1',
    title: 'Conflicts: SharePoint policy vs Zscaler URL blocking',
    created_at: new Date(Date.now() - 2 * 3600_000).toISOString(),
    last_message_at: new Date(Date.now() - 2 * 3600_000 + 90_000).toISOString(),
    message_count: 2,
    chat_type: 'analyst',
    messages: [
      { role: 'user', content: 'Are there conflicts between our SharePoint AUP and Zscaler URL rules?', ts: new Date(Date.now() - 2 * 3600_000).toISOString(), tool_calls: [] },
      { role: 'assistant', content: '(mock) Found 2 conflicts: browser restrictions and social-media on guest network.', ts: new Date(Date.now() - 2 * 3600_000 + 30_000).toISOString(), tool_calls: ['sharepoint_lookup', 'zscaler_lookup'] },
    ],
  },
  {
    session_id: 'mock-sess-2',
    title: 'AWS Config rules — current count',
    created_at: new Date(Date.now() - 24 * 3600_000).toISOString(),
    last_message_at: new Date(Date.now() - 24 * 3600_000 + 60_000).toISOString(),
    message_count: 2,
    chat_type: 'mcp',
    messages: [
      { role: 'user', content: 'How many AWS Config rules are configured?', ts: new Date(Date.now() - 24 * 3600_000).toISOString(), tool_calls: [] },
      { role: 'assistant', content: '(mock) 0 rules. No conformance pack attached.', ts: new Date(Date.now() - 24 * 3600_000 + 30_000).toISOString(), tool_calls: ['list_config_rules'] },
    ],
  },
]

// Architecture: the master AgentCore Runtime owns persistence. Each /chat
// call writes both the DDB conversation index row (on the first turn) and the
// memory event (every turn). The UI just:
//   - lists sessions from DDB
//   - loads message history from AgentCore Memory via /conversations/{id}/messages
//   - sends new messages via /chat with a client-generated session_id
export function useConversations(opts = {}) {
  // opts.type filters server-side (and client-side in mock mode) to 'analyst'
  // or 'mcp' so AnalystView and MCPChat each see only their own sessions.
  const filterType = opts.type || null
  const [sessions, setSessions] = useState([])
  const [activeMessages, setActiveMessages] = useState([])
  const [loading, setLoading] = useState(false)

  const list = useCallback(async () => {
    setLoading(true)
    try {
      if (USE_MOCK) {
        await sleep(150)
        const all = MOCK_SESSIONS.map(({ messages, ...summary }) => summary)
        setSessions(filterType ? all.filter(s => (s.chat_type || 'analyst') === filterType) : all)
      } else {
        const qs = filterType ? `?type=${encodeURIComponent(filterType)}` : ''
        const data = await apiFetch(`/conversations${qs}`)
        setSessions(data.sessions || [])
      }
    } finally { setLoading(false) }
  }, [filterType])

  // Load message history for a session from AgentCore Memory.
  // Returns { session_id, messages: [{role, content, ts}] } in chronological order.
  const loadMessages = useCallback(async (sessionId) => {
    setLoading(true)
    try {
      if (USE_MOCK) {
        await sleep(150)
        const sess = MOCK_SESSIONS.find(s => s.session_id === sessionId)
        const msgs = sess?.messages || []
        setActiveMessages(msgs)
        return { session_id: sessionId, messages: msgs }
      }
      const data = await apiFetch(`/conversations/${encodeURIComponent(sessionId)}/messages`)
      setActiveMessages(data.messages || [])
      return data
    } finally { setLoading(false) }
  }, [])

  const clearActive = useCallback(() => setActiveMessages([]), [])

  // Optimistically add a freshly-created session to the local list so the
  // sidebar updates immediately. The real row is created by the master agent
  // on its first /chat invocation; this just keeps the UI snappy.
  const addLocalSession = useCallback((session) => {
    setSessions(prev => [session, ...prev.filter(s => s.session_id !== session.session_id)])
  }, [])

  // Bump local sidebar metadata after a turn completes, so message_count and
  // last_message_at reflect the new state without re-fetching the full list.
  const bumpLocalSession = useCallback((sessionId, delta = 2) => {
    setSessions(prev => prev.map(s => s.session_id === sessionId
      ? { ...s, message_count: (s.message_count || 0) + delta, last_message_at: new Date().toISOString() }
      : s))
  }, [])

  // Hard-delete a session: removes the DDB index row. UI calls this for the
  // per-chat trash button, the "Resolve" button, and the auto-archive when a
  // linked CR completes. Optimistically removes from local state before the
  // network call so the sidebar feels instant; on error, the next list() pull
  // restores the row (the server is the source of truth).
  const deleteSession = useCallback(async (sessionId) => {
    if (!sessionId) return
    setSessions(prev => prev.filter(s => s.session_id !== sessionId))
    if (USE_MOCK) { await sleep(150); return }
    try {
      await apiFetch(`/conversations/${encodeURIComponent(sessionId)}`, { method: 'DELETE' })
    } catch (err) {
      console.warn('deleteSession failed:', err)
      throw err
    }
  }, [])

  // Bulk-delete N sessions in a single server round-trip. Unlike deleteSession,
  // this is NOT optimistic: the server returns a {deleted, failed} summary with
  // per-id partial-failure outcomes, so the caller should refresh via list()
  // after the promise resolves to reconcile the sidebar with the truth. In mock
  // mode, splice the ids out of MOCK_SESSIONS and return {deleted: ids, failed: []}.
  const bulkDeleteSessions = useCallback(async (ids) => {
    if (USE_MOCK) {
      await sleep(150)
      for (const id of ids || []) {
        const idx = MOCK_SESSIONS.findIndex(s => s.session_id === id)
        if (idx >= 0) MOCK_SESSIONS.splice(idx, 1)
      }
      return { deleted: [...(ids || [])], failed: [] }
    }
    return apiFetch('/conversations/bulk-delete', {
      method: 'POST',
      body: JSON.stringify({ session_ids: ids }),
    })
  }, [])

  // Server-side scoped delete — server walks every session row this user owns
  // (paginated), filters by scope, and deletes each. Lets the UI clear chats
  // the sidebar can't see because of the list Limit.
  //   scope === 'all' | 'harness' | 'older_than_days' (days required for the last)
  // Returns {deleted: [...], failed: [...], truncated: bool}. If truncated,
  // the caller should re-invoke until truncated === false to drain the rest.
  const bulkDeleteByScope = useCallback(async (scope, opts = {}) => {
    if (USE_MOCK) {
      await sleep(150)
      const nowMs = Date.now()
      const matches = (s) => {
        if (scope === 'all') return true
        if (scope === 'harness') {
          return ['harness-', 'features-', 'logic-race-'].some(p => (s.session_id || '').startsWith(p))
        }
        if (scope === 'older_than_days') {
          const t = Date.parse(s.created_at || '')
          if (Number.isNaN(t)) return false
          const days = Number(opts.days)
          if (!Number.isFinite(days) || days <= 0) return false
          return (nowMs - t) > days * 86400_000
        }
        return false
      }
      const deleted = []
      for (let i = MOCK_SESSIONS.length - 1; i >= 0; i--) {
        if (matches(MOCK_SESSIONS[i])) {
          deleted.push(MOCK_SESSIONS[i].session_id)
          MOCK_SESSIONS.splice(i, 1)
        }
      }
      return { deleted, failed: [], truncated: false }
    }
    const payload = { scope }
    if (scope === 'older_than_days') payload.days = Number(opts.days)
    return apiFetch('/conversations/bulk-delete', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  }, [])

  return {
    sessions, activeMessages, loading,
    list, loadMessages, clearActive,
    addLocalSession, bumpLocalSession, deleteSession, bulkDeleteSessions, bulkDeleteByScope
  }
}

// Hits an agent runtime via the Lambda Function URL (CHAT_URL) so we bypass
// API Gateway's 29s timeout. Body shape: { prompt, session_id, chat_type, target, data_group, data_project_id, data_project_name }.
// chat_type ('analyst' | 'mcp') is stamped onto new session rows so the two
// chats can be listed separately. `target` selects which agent runtime handles
// the message: absent/'master' → orchestrator fan-out (Analyst page); a
// specialist id ('sharepoint' | 'zscaler' | 'awsconfig' | 'jira' | 'servicenow')
// → that agent directly (MCP page). Response: { reply, session_id }.
export async function sendChat({ prompt, session_id, chat_type, target, data_group, data_project_id, data_project_name, data_group_id }) {
  if (USE_MOCK || !CHAT_URL) {
    await sleep(600 + Math.random() * 800)
    return { reply: `(mock reply) You asked: "${prompt}". Wire VITE_CHAT_URL to get a real answer.`, session_id }
  }
  const res = await fetch(`${CHAT_URL}chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({
      prompt,
      session_id,
      chat_type: chat_type || 'analyst',
      target,
      data_group,
      data_project_id,
      data_project_name,
      data_group_id,
    }),
  })
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json()
}

// IT-asset change-impact analysis via the servicenow_specialist runtime.
// POST /servicenow/impact-analysis {resource, target_environment, severity,
// draft_change} → {changed_resource, affected_cis, owner_team, cab_required,
// approver_chain, change?}. draft_change=true also drafts a real change_request.
export async function runImpactAnalysis({ resource, target_environment = 'PROD', severity = 'HIGH', draft_change = false }) {
  if (USE_MOCK) {
    await sleep(500 + Math.random() * 500)
    return mockImpactAnalysis({ resource, target_environment, severity, draft_change })
  }
  return apiFetch('/servicenow/impact-analysis', {
    method: 'POST',
    body: JSON.stringify({ resource, target_environment, severity, draft_change }),
  })
}

// CMDB / Asset drift scan via the master orchestrator (servicenow_drift_scan mode).
// POST /servicenow/drift-scan → {configured, drift_items:[{title, severity, finding,
// impact, remediation, source_technical, enforcement_evidence:[{raw:{drift_kind}}]}],
// summary:{total, by_kind, by_severity}, snapshot_counts, aws_inventory_count}.
export async function runDriftScan() {
  if (USE_MOCK) {
    await sleep(600 + Math.random() * 600)
    return mockDriftScan()
  }
  return apiFetch('/servicenow/drift-scan', { method: 'POST', body: JSON.stringify({}) })
}

// Single-round-trip dashboard aggregate. Polls every 60s.
// Falls back to the per-route hooks (findings + CRs + audit) when USE_MOCK.
export function useDashboard() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      if (USE_MOCK) {
        await sleep(150)
        setData(null)
        return
      }
      const d = await apiFetch('/dashboard')
      setData(d)
    } catch {
      // Dashboard tile falls back to its per-route hooks; silent recovery.
    } finally { setLoading(false) }
  }, [])

  useEffect(() => {
    let cancelled = false
    const tick = async () => { if (!cancelled) await load() }
    tick()
    const id = setInterval(tick, 60_000)
    return () => { cancelled = true; clearInterval(id) }
  }, [load])

  return { data, loading, reload: load }
}

// ── Reports ─────────────────────────────────────────────────────────────────
// Synchronous report generation: generate() returns a payload with a ready
// download_url (presigned S3 GET in live mode; an object-URL blob in mock mode).
// No job table / polling — the backend builds the file inline.
export function useReports() {
  const [catalog, setCatalog] = useState(null)
  const [loadingCatalog, setLoadingCatalog] = useState(false)

  const loadCatalog = useCallback(async () => {
    setLoadingCatalog(true)
    try {
      if (USE_MOCK) {
        await sleep(150)
        setCatalog({ catalog: MOCK_REPORT_CATALOG, categories: MOCK_REPORT_CATEGORIES })
      } else {
        const data = await apiFetch('/reports/catalog')
        setCatalog(data)
      }
    } finally { setLoadingCatalog(false) }
  }, [])

  const generate = useCallback(async (report_type, format, params) => {
    if (USE_MOCK) {
      await sleep(500)
      return mockGenerateReport(report_type, format, params)
    }
    return apiFetch('/reports/generate', {
      method: 'POST',
      body: JSON.stringify({ report_type, format, params: params || {} }),
    })
  }, [])

  return { catalog, loadingCatalog, loadCatalog, generate }
}

// ── Compliance (Governance "Generate report" buttons + per-framework export) ──
export function useCompliance() {
  const [generating, setGenerating] = useState(null)

  const generateReport = useCallback(async (report_type, frameworks) => {
    setGenerating(report_type)
    try {
      let data
      if (USE_MOCK) {
        await sleep(500)
        const map = { executive: 'executive_compliance', technical: 'technical_compliance', evidence_package: 'evidence_package' }
        data = mockGenerateReport(map[report_type] || report_type, undefined, { frameworks })
      } else {
        data = await apiFetch('/compliance/report', {
          method: 'POST',
          body: JSON.stringify({ report_type, frameworks }),
        })
      }
      if (data?.download_url || data?.report_url) {
        triggerDownload(data.download_url || data.report_url, data.filename)
      }
      return data
    } finally { setGenerating(null) }
  }, [])

  return { generating, generateReport }
}

// Trigger a scan run. Returns {scan_run_id, status, stub?}.
export async function triggerScan() {
  if (USE_MOCK) return { scan_run_id: 'mock-scan', status: 'COMPLETED', stub: true }
  return apiFetch('/scan', { method: 'POST', body: JSON.stringify({}) })
}

// Poll a scan run's status. UI hits this every 2s until status != 'RUNNING'.
export async function getScanRun(scanRunId) {
  if (USE_MOCK) return { scan_run_id: scanRunId, status: 'COMPLETED' }
  return apiFetch(`/scan-runs/${encodeURIComponent(scanRunId)}`)
}

// Live scan feed — the shared spine for "real-time" conflict detection.
// Polls /scan-runs on one timer and fires onNewScan(run) the first time it sees
// a newly-finished (COMPLETED/FAILED) run, so any page can re-pull findings the
// moment a background F1 scan (upload → ingest → scan) completes — no manual
// refresh, whether the scan was manual, auto-ingest, or the daily cron.
//
// StrictMode-safe: each mount primes on its first observation (records the run
// that already exists and does NOT fire for it) so the dev double-mount never
// double-fires; an in-flight guard keeps the single timer single-flight. Returns
// { activeRun } — the newest RUNNING run — for a live "scanning…" pill.
// No-op in mock mode (no real scan-runs exist) so static mock data stays stable.
export function useScanFeed({ onNewScan, intervalMs = 5000, enabled = true } = {}) {
  const onNewScanRef = useRef(onNewScan)
  onNewScanRef.current = onNewScan
  const [activeRun, setActiveRun] = useState(null)

  useEffect(() => {
    if (!enabled || USE_MOCK) return
    let cancelled = false
    let inFlight = false
    let primed = false      // suppress firing for the run that exists at mount
    let lastSeenKey = null  // scan_run_id|finished_at of the newest finished run

    const tick = async () => {
      if (inFlight || cancelled) return
      inFlight = true
      try {
        const { scan_runs = [] } = await listScanRuns(10)
        if (cancelled) return
        const up = (s) => (s || '').toUpperCase()
        // A scan_run_id that already has a terminal (COMPLETED/FAILED) row is
        // done — ignore its orphaned RUNNING pre-write row. (api_handler writes a
        // RUNNING row on POST /scan, the scanner then writes its own terminal row
        // under the SAME scan_run_id, so the pre-write never flips and would
        // otherwise keep a "scanning…" indicator on forever.) Also ignore stale
        // RUNNING rows >10min old (crashed scans).
        const terminalIds = new Set(
          scan_runs.filter(r => ['COMPLETED', 'FAILED'].includes(up(r.status)))
                   .map(r => r.scan_run_id)
        )
        const tenMinAgo = Date.now() - 10 * 60_000
        const active = scan_runs.find(r =>
          up(r.status) === 'RUNNING' &&
          !terminalIds.has(r.scan_run_id) &&
          new Date(r.started_at || 0).getTime() > tenMinAgo
        )
        setActiveRun(active || null)
        const newest = scan_runs
          .filter(r => ['COMPLETED', 'FAILED'].includes(up(r.status)))
          .sort((a, b) => (b.finished_at || b.started_at || '')
            .localeCompare(a.finished_at || a.started_at || ''))[0]
        if (!newest) return
        const key = `${newest.scan_run_id}|${newest.finished_at || newest.started_at || ''}`
        if (!primed) { primed = true; lastSeenKey = key; return }
        if (key !== lastSeenKey) {
          lastSeenKey = key
          onNewScanRef.current?.(newest)
        }
      } catch {
        // Keep prior state; the next tick retries.
      } finally {
        inFlight = false
      }
    }

    tick()
    const id = setInterval(tick, intervalMs)
    return () => { cancelled = true; clearInterval(id) }
  }, [enabled, intervalMs])

  return { activeRun }
}

// Lazy GET /findings/{id} for the FindingDetail page.
// In mock mode, looks the row up from MOCK_CONFLICTS so direct-URL navigation works.
export function useFindingDetail(id) {
  const [finding, setFinding] = useState(null)
  const [loading, setLoading] = useState(false)
  useEffect(() => {
    if (!id) return
    let cancelled = false
    setLoading(true)
    ;(async () => {
      try {
        if (USE_MOCK) {
          await sleep(150)
          const found = MOCK_CONFLICTS.find(f => f.conflict_id === id) || null
          if (!cancelled) setFinding(found)
          return
        }
        const d = await apiFetch(`/findings/${encodeURIComponent(id)}`)
        if (!cancelled) setFinding(d)
      } catch {
        if (!cancelled) setFinding(null)
      } finally { if (!cancelled) setLoading(false) }
    })()
    return () => { cancelled = true }
  }, [id])
  return { finding, loading }
}

// MCP server health for the dashboard tile + MCPChat status panel.
export function useMcpHealth() {
  const [data, setData] = useState({ summary: 'UNKNOWN', servers: [] })
  const load = useCallback(async () => {
    try {
      if (USE_MOCK) {
        setData({
          summary: 'UP',
          servers: [
            { name: 'SharePoint MCP', status: 'UP', latency_ms: 320 },
            { name: 'Zscaler MCP',    status: 'UP', latency_ms: 410 },
            { name: 'AWS Config MCP', status: 'UP', latency_ms: 95  },
            { name: 'ServiceNow MCP', status: 'DEGRADED', latency_ms: 1240 },
            { name: 'Atlassian MCP',  status: 'UP', latency_ms: 280 },
          ],
        })
        return
      }
      const d = await apiFetch('/mcp-health')
      setData(d)
    } catch { /* keep prior */ }
  }, [])
  useEffect(() => {
    let cancelled = false
    const tick = async () => { if (!cancelled) await load() }
    tick()
    const id = setInterval(tick, 30_000)
    return () => { cancelled = true; clearInterval(id) }
  }, [load])
  return data
}

// Live AgentCore runtime status for the MCP page, keyed by agent id. Backed by
// GET /agent-status (bedrock-agentcore list-agent-runtimes). Returns a map
// { [id]: status } where status is READY / CREATING / PLACEHOLDER / etc.
// Polls every 30s.
export function useAgentStatus() {
  const [statusById, setStatusById] = useState({})
  const load = useCallback(async () => {
    try {
      if (USE_MOCK) {
        setStatusById({
          sharepoint: 'READY', zscaler: 'READY', awsconfig: 'READY',
          structured: 'READY', sales: 'READY', hr: 'READY', paloalto: 'READY', jira: 'READY', servicenow: 'READY',
        })
        return
      }
      const d = await apiFetch('/agent-status')
      const map = {}
      for (const s of d.servers || []) map[s.id] = s.status
      setStatusById(map)
    } catch { /* keep prior */ }
  }, [])
  useEffect(() => {
    let cancelled = false
    const tick = async () => { if (!cancelled) await load() }
    tick()
    const id = setInterval(tick, 30_000)
    return () => { cancelled = true; clearInterval(id) }
  }, [load])
  return statusById
}

// Upload helpers — POST /uploads/presign returns a presigned S3 PUT URL into
// the raw bucket under users/<sub>/<ts>-<filename>. The browser then PUTs the
// file directly to S3. The F1 auto-detect chain (EventBridge → processing_pipeline
// → KB ingestion → scanner) picks it up automatically.
export async function presignUpload({ filename, contentType }) {
  if (USE_MOCK) {
    return { url: '#mock', method: 'PUT', key: `users/mock/${Date.now()}-${filename}`,
             bucket: 'mock', expires_in: 900, headers: {} }
  }
  return apiFetch('/uploads/presign', {
    method: 'POST',
    body: JSON.stringify({ filename, contentType: contentType || 'application/octet-stream' }),
  })
}

export async function uploadToPresignedUrl(url, headers, body) {
  if (url === '#mock') {
    // Pretend the PUT succeeded after a tiny delay so mock mode demos still flow.
    await sleep(400)
    return { ok: true, status: 200 }
  }
  const s3Headers = new Headers()
  Object.entries(headers || {}).forEach(([key, value]) => {
    if (value === undefined || value === null) return
    if (key.toLowerCase() === 'authorization') return
    s3Headers.set(key, String(value))
  })
  s3Headers.delete('authorization')
  s3Headers.delete('Authorization')
  const res = await fetch(url, {
    method: 'PUT',
    headers: s3Headers,
    body,
    credentials: 'omit',
    cache: 'no-store',
    referrerPolicy: 'no-referrer',
  })
  const text = await res.text().catch(() => '')
  return { ok: res.ok, status: res.status, detail: text }
}

export async function listScanRuns(limit = 20) {
  if (USE_MOCK) return { scan_runs: [] }
  return apiFetch('/scan-runs')
}

export async function listUploadedFiles(bucket = 'processed') {
  if (USE_MOCK) {
    const now = new Date().toISOString()
    const invoiceFiles = [
      ...Array.from({ length: 5 }, (_, idx) => ({
        key: `users/mock/processed/AR_Invoice_${String(idx + 1).padStart(3, '0')}.csv`,
        name: `AR_Invoice_${String(idx + 1).padStart(3, '0')}.csv`,
        size: 1224 + idx * 117,
        last_modified: now,
      })),
      ...Array.from({ length: 5 }, (_, idx) => ({
        key: `users/mock/processed/AP_Invoice_${String(idx + 1).padStart(3, '0')}.csv`,
        name: `AP_Invoice_${String(idx + 1).padStart(3, '0')}.csv`,
        size: 1350 + idx * 91,
        last_modified: now,
      })),
    ]
    return {
      bucket: 'mock-processed',
      prefix: 'users/mock/',
      files: [
        ...invoiceFiles,
        { key: 'users/mock/processed/vendor_contract.pdf', name: 'vendor_contract.pdf', size: 23890, last_modified: now },
        { key: 'users/mock/processed/control_export.json', name: 'control_export.json', size: 4420, last_modified: now },
      ],
      truncated: false,
    }
  }
  const qs = new URLSearchParams({ bucket }).toString()
  return apiFetch(`/uploads/list?${qs}`)
}

export async function getUploadStatus(key) {
  if (USE_MOCK) {
    await sleep(250)
    return {
      key,
      isCsv: key?.toLowerCase().endsWith('.csv'),
      status: key?.toLowerCase().endsWith('.csv') ? 'catalog_done' : 'processed',
      message: 'mock',
      raw: { exists: false },
      processed: { exists: true },
      structured: key?.toLowerCase().endsWith('.csv') ? { exists: true, key: 'structured/staged/mock/mock.csv' } : null,
      crawler: key?.toLowerCase().endsWith('.csv') ? { state: 'READY', lastCrawl: { Status: 'SUCCEEDED' } } : null,
    }
  }
  const qs = new URLSearchParams({ key }).toString()
  return CHAT_URL
    ? functionUrlFetch(`/uploads/status?${qs}`)
    : apiFetch(`/uploads/status?${qs}`)
}

export async function materializeDataGroupingProject({ projectName, projectId, groups = [], deleteGroups = [], move = true, syncKnowledgeBase = true }) {
  if (USE_MOCK) {
    await sleep(400)
    return {
      bucket: 'mock-processed',
      projectPrefix: `projects/${projectId}/`,
      metadataKey: `projects/${projectId}/metadata/project.json`,
      copied: groups.flatMap(group => (group.files || []).map(file => ({
        sourceKey: file.key,
        destinationKey: `projects/${projectId}/${group.name}/${file.name}`,
      }))),
      structuredCopies: [],
      deletedSources: [],
      crawlerStarted: false,
      crawlerMessage: 'mock',
      kbSync: { started: syncKnowledgeBase, message: syncKnowledgeBase ? 'mock_started' : 'skipped' },
    }
  }
  return functionUrlFetch('/data-grouping/materialize', {
    method: 'POST',
    body: JSON.stringify({ projectName, projectId, groups, deleteGroups, move, syncKnowledgeBase }),
  })
}

export async function getDataGroupingProject(projectId) {
  if (USE_MOCK) {
    await sleep(150)
    return {
      projectId,
      exists: false,
      groups: [],
      assignedSourceKeys: [],
    }
  }
  const qs = new URLSearchParams({ projectId }).toString()
  return apiFetch(`/data-grouping/project?${qs}`)
}

export async function listDataGroupingProjects() {
  if (USE_MOCK) {
    await sleep(150)
    return {
      groups: [
        {
          id: 'mock::Project_Helios_Ridge',
          projectId: 'vendor-audit-june-2026',
          projectName: 'Vendor Audit June 2026',
          groupName: 'Project_Helios_Ridge',
          label: 'Vendor Audit June 2026 / Project_Helios_Ridge',
          value: 'Project_Helios_Ridge',
          fileCount: 3,
          csvCount: 3,
          tableCount: 3,
        },
      ],
      truncated: false,
    }
  }
  return apiFetch('/data-grouping/projects')
}

// ── Data-ingest jobs (async S3-Vectors worker; Phase 2 backend) ──────────────
// DocuSearch (unstructured) + Structured Analytics (tabular) submit an async
// ingest job that chunks/embeds a published group folder into S3 Vectors. These
// route through the Lambda Function URL (CHAT_URL) like the other data-grouping
// ops so no API Gateway resource wiring is needed. The api_handler pre-writes a
// QUEUED data-jobs row and fire-and-forget invokes the worker, which flips the
// row RUNNING → SUCCEEDED/FAILED. jobType: 'docusearch' | 'structured_analytics'.
export async function triggerDataIngest({ jobType, projectId, projectName, groupName, datasetId, grain }) {
  if (USE_MOCK) {
    await sleep(400)
    return { job_id: `job-mock-${Date.now()}`, status: 'QUEUED', vector_index: `${projectId || 'p'}-${groupName || 'g'}`.toLowerCase() }
  }
  return functionUrlFetch('/data-pipeline/ingest', {
    method: 'POST',
    body: JSON.stringify({ jobType, projectId, projectName, groupName, datasetId, grain }),
  })
}

// List recent data-ingest jobs (newest first). Optional projectId scopes via GSI.
export async function listDataJobs(projectId) {
  if (USE_MOCK) {
    await sleep(150)
    const now = Date.now()
    return {
      data_jobs: [
        {
          job_id: 'job-mock-docusearch', created_at: new Date(now - 120_000).toISOString(),
          status: 'SUCCEEDED', job_type: 'docusearch', project_id: 'discovery',
          group_name: 'Vendor_Policies', vector_index: 'discovery-vendor-policies',
          result: { files: 12, documents: 12, chunks: 96, vectors: 96 },
        },
        {
          job_id: 'job-mock-structured', created_at: new Date(now - 45_000).toISOString(),
          status: 'RUNNING', job_type: 'structured_analytics', project_id: 'discovery',
          group_name: 'Sales_Q2', vector_index: 'discovery-sales-q2',
        },
      ],
    }
  }
  const qs = projectId ? `?${new URLSearchParams({ projectId }).toString()}` : ''
  return functionUrlFetch(`/data-jobs${qs}`)
}

export async function getDataJob(jobId) {
  if (USE_MOCK) {
    await sleep(150)
    return { job_id: jobId, status: 'SUCCEEDED', job_type: 'docusearch', result: { files: 3, vectors: 24 } }
  }
  return functionUrlFetch(`/data-jobs/${encodeURIComponent(jobId)}`)
}

export async function startDataGroupingCrawler() {
  if (USE_MOCK) {
    await sleep(300)
    return {
      crawlerName: 'mock-structured-crawler',
      crawlerStarted: true,
      crawlerMessage: 'mock_started',
      state: 'RUNNING',
    }
  }
  return apiFetch('/data-grouping/start-crawler', {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export async function analyzeDataGroupingDocuments({ groupName, files }) {
  if (USE_MOCK) {
    await sleep(300)
    return {
      groupName,
      generatedAt: new Date().toISOString(),
      documentCount: files.length,
      skipped: [],
      projects: files.map((file, index) => ({
        name: file.name,
        key: file.key,
        title: file.name.replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' '),
        keywords: ['delivery', 'dependency', 'risk'].slice(0, 2 + (index % 2)),
        goals: ['Mock goal detected from local project document.'],
        problems: ['Mock problem statement detected from local project document.'],
        risks: index % 3 === 0 ? ['Mock risk: dependency or timeline uncertainty.'] : [],
        dependencies: ['Mock dependency: engineering team coordination.'],
        successSignals: [],
        missingInformation: ['success metrics'],
        riskLevel: index % 3 === 0 ? 'medium' : 'low',
        recommendedAction: 'Clarify owner, timeline, dependencies, and success metrics.',
      })),
      overlaps: [],
      actionPlan: ['Request missing project details.', 'Sequence projects after dependency review.'],
      markdown: `# Portfolio Analysis: ${groupName}\n\nMock document analysis for ${files.length} files.\n`,
    }
  }
  return apiFetch('/data-grouping/analyze-documents', {
    method: 'POST',
    body: JSON.stringify({ groupName, files }),
  })
}

const MOCK_SECURITY_GROUP_BASELINE = [
  {
    resourceId: 'sg-lm-prod-peer-dev-001',
    resourceName: 'lm-prod-peer-dev',
    vpcId: 'vpc-lm-prod-001a2b3c4d',
    environment: 'PRODUCTION',
    ingress: [
      {
        direction: 'ingress',
        protocol: '-1',
        fromPort: -1,
        toPort: -1,
        sourceType: 'cidr',
        source: '10.50.0.0/16',
        description: 'dev VPC CIDR',
      },
    ],
    egress: [],
  },
]

const MOCK_SECURITY_GROUP_LATEST = [
  {
    ...MOCK_SECURITY_GROUP_BASELINE[0],
    ingress: [
      ...MOCK_SECURITY_GROUP_BASELINE[0].ingress,
      {
        direction: 'ingress',
        protocol: 'tcp',
        fromPort: 22,
        toPort: 22,
        sourceType: 'cidr',
        source: '0.0.0.0/0',
        description: 'console-added temporary SSH access',
      },
    ],
  },
]

function mockSecurityGroupFinding() {
  const deadline = new Date(Date.now() + 10 * 60_000).toISOString()
  return {
    checkId: `mock-sg-drift-${Date.now()}`,
    checkedAt: new Date().toISOString(),
    source: 'mock_ec2_describe_security_groups',
    baselineCapturedAt: new Date(Date.now() - 5 * 60_000).toISOString(),
    baselineResourceCount: MOCK_SECURITY_GROUP_BASELINE.length,
    latestResourceCount: MOCK_SECURITY_GROUP_LATEST.length,
    hitl: {
      status: 'PENDING',
      deadlineAt: deadline,
      timeoutMinutes: 10,
      note: 'Mock remediation is not executed.',
    },
    findings: [
      {
        id: 'sg-lm-prod-peer-dev-001-ingress-added-public-ssh',
        resourceId: 'sg-lm-prod-peer-dev-001',
        resourceName: 'lm-prod-peer-dev',
        driftType: 'Ingress rule added',
        before: 'No matching baseline rule',
        after: 'ingress tcp 22 from 0.0.0.0/0',
        severity: 'CRITICAL',
        recommendation: 'Open a HITL exception immediately; revoke this public ingress if no approval is received before the deadline.',
        pendingRevert: {
          action: 'revoke_security_group_ingress',
          resourceId: 'sg-lm-prod-peer-dev-001',
          status: 'PENDING_HITL',
        },
      },
    ],
    pendingReverts: [
      {
        action: 'revoke_security_group_ingress',
        resourceId: 'sg-lm-prod-peer-dev-001',
        status: 'PENDING_HITL',
      },
    ],
    latest: MOCK_SECURITY_GROUP_LATEST,
  }
}

export async function getCurrentSecurityGroups() {
  if (USE_MOCK) {
    await sleep(250)
    return {
      source: 'mock_ec2_describe_security_groups',
      observedAt: new Date().toISOString(),
      resources: MOCK_SECURITY_GROUP_LATEST,
      count: MOCK_SECURITY_GROUP_LATEST.length,
    }
  }
  return apiFetch('/config-drift/security-groups/current')
}

export async function getSecurityGroupBaseline() {
  if (USE_MOCK) {
    await sleep(150)
    return {
      captured: true,
      capturedAt: new Date(Date.now() - 5 * 60_000).toISOString(),
      capturedBy: 'mock-user',
      source: 'mock_ec2_describe_security_groups',
      resourceType: 'AWS::EC2::SecurityGroup',
      resourceCount: MOCK_SECURITY_GROUP_BASELINE.length,
      resources: MOCK_SECURITY_GROUP_BASELINE,
    }
  }
  return apiFetch('/config-drift/security-groups/baseline')
}

export async function captureSecurityGroupBaseline({ groupIds } = {}) {
  if (USE_MOCK) {
    await sleep(500)
    return {
      capturedAt: new Date().toISOString(),
      capturedBy: 'mock-user',
      source: 'mock_ec2_describe_security_groups',
      resourceType: 'AWS::EC2::SecurityGroup',
      resources: MOCK_SECURITY_GROUP_BASELINE,
    }
  }
  return apiFetch('/config-drift/security-groups/baseline', {
    method: 'POST',
    body: JSON.stringify({ groupIds: groupIds || [] }),
  })
}

export async function checkSecurityGroupDrift({ hitlTimeoutMinutes = 10 } = {}) {
  if (USE_MOCK) {
    await sleep(600)
    return mockSecurityGroupFinding()
  }
  return apiFetch('/config-drift/security-groups/check', {
    method: 'POST',
    body: JSON.stringify({ hitlTimeoutMinutes }),
  })
}

export async function revertSecurityGroupDrift({ checkId }) {
  if (USE_MOCK) {
    await sleep(500)
    return {
      checkId,
      status: 'COMPLETED',
      applied: [{ resourceId: 'sg-lm-prod-peer-dev-001', action: 'revoke_security_group_ingress' }],
      skipped: [],
      check: {
        ...mockSecurityGroupFinding(),
        checkId,
        revertStatus: 'COMPLETED',
        revertedAt: new Date().toISOString(),
        pendingReverts: [
          {
            action: 'revoke_security_group_ingress',
            resourceId: 'sg-lm-prod-peer-dev-001',
            status: 'COMPLETED',
          },
        ],
      },
    }
  }
  return apiFetch('/config-drift/security-groups/revert', {
    method: 'POST',
    body: JSON.stringify({ checkId }),
  })
}

// Create a real JIRA issue via the jira_specialist runtime. Routes through the
// Lambda Function URL (CHAT_URL) like sendChat, since the runtime call (MCP
// subprocess + create) can exceed API Gateway's 29s integration timeout.
// Returns { jira_ticket_key, url }. project_key defaults to DEVARBITER.
export async function createJiraTicket({ conflict_id, summary, description, project_key, severity }) {
  const pk = project_key || 'DEVARBITER'
  if (USE_MOCK || !CHAT_URL) {
    await sleep(700)
    const key = `${pk}-${Math.floor(Math.random() * 9000) + 1000}`
    return { status: 'mock', jira_ticket_key: key, url: `https://example.atlassian.net/browse/${key}` }
  }
  const res = await fetch(`${CHAT_URL}jira/tickets`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ conflict_id, summary, description, project_key: pk, severity }),
  })
  if (!res.ok) {
    // Surface the Lambda's {"error": ...} body so create failures (bad issue
    // type, permissions, etc.) are legible instead of a bare "502 Bad Gateway".
    let detail = ''
    try { detail = (await res.json())?.error || '' } catch { /* non-JSON body */ }
    throw new Error(detail ? `${res.status}: ${detail}` : `${res.status} ${res.statusText}`)
  }
  return res.json()
}

// Shared POST-to-Function-URL helper for the JIRA L1 + What-If routes. Goes via
// CHAT_URL (not API GW) because the MCP subprocess / runtime call can exceed the
// 29s API Gateway integration timeout. Surfaces the Lambda's {"error"} body.
async function _postChat(pathSuffix, payload, mockResult) {
  if (USE_MOCK || !CHAT_URL) { await sleep(700); return mockResult }
  const res = await fetch(`${CHAT_URL}${pathSuffix}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(payload),
  })
  if (!res.ok) {
    let detail = ''
    try { detail = (await res.json())?.error || '' } catch { /* non-JSON */ }
    throw new Error(detail ? `${res.status}: ${detail}` : `${res.status} ${res.statusText}`)
  }
  return res.json()
}

// JIRA L1 resolution: transition an issue (defaults to "Done") + optional comment.
// Returns { status, jira_ticket_key, transitioned_to, url }.
export async function transitionJira({ jira_key, transition, comment, cr_id }) {
  return _postChat('jira/transition',
    { jira_key, transition: transition || 'Done', comment, cr_id },
    { status: 'transitioned', jira_ticket_key: jira_key, transitioned_to: transition || 'Done' })
}

// Add a comment to a JIRA issue. Returns { status, jira_ticket_key, url }.
export async function commentJira({ jira_key, comment, cr_id }) {
  return _postChat('jira/comment',
    { jira_key, comment, cr_id },
    { status: 'commented', jira_ticket_key: jira_key })
}

// What-If dry-run: run the rule pack against hypothetical observations WITHOUT
// persisting. `observations` is { <source>: [obs...] }; omitted sources seed
// normally server-side. Returns { dry_run, findings, totals }.
export async function dryRunScan(observations) {
  return _postChat('scan/dry-run',
    { observations },
    { dry_run: true, findings: [], totals: { conflicts: 0, compliant: 0 } })
}

// Live nav badge counts. Polls every 60s; cancels on unmount.
// findingsOpen = conflicts with status === 'OPEN'; actionsPending = CRs with
// status === 'PENDING_APPROVAL'. Mock mode reads from MOCK_*.
// Also exposes the OPEN findings themselves (openFindings) so the TopBar
// notifications bell can render from the same poll — call this once per
// shell (App.jsx) and pass results down, don't instantiate per consumer.
export function useNavCounts() {
  const [findingsOpen, setFindingsOpen] = useState(0)
  const [actionsPending, setActionsPending] = useState(0)
  const [openFindings, setOpenFindings] = useState([])

  useEffect(() => {
    let cancelled = false
    const tick = async () => {
      try {
        if (USE_MOCK) {
          if (cancelled) return
          const open = MOCK_CONFLICTS.filter(f => f.status === 'OPEN')
          setFindingsOpen(open.length)
          setOpenFindings(open)
          setActionsPending(MOCK_CHANGE_REQUESTS.filter(c => c.status === 'PENDING_APPROVAL').length)
        } else {
          const [findings, actions] = await Promise.all([
            apiFetch('/findings'),
            apiFetch('/actions'),
          ])
          if (cancelled) return
          const open = (findings.findings || []).filter(f => f.status === 'OPEN')
          setFindingsOpen(open.length)
          setOpenFindings(open)
          setActionsPending((actions.change_requests || []).filter(c => c.status === 'PENDING_APPROVAL').length)
        }
      } catch {
        // Silent: badges fall back to whatever last succeeded; nav still renders.
      }
    }
    tick()
    const id = setInterval(tick, 60_000)
    return () => { cancelled = true; clearInterval(id) }
  }, [])

  return { findingsOpen, actionsPending, openFindings }
}

export function useAudit() {
  const [logs, setLogs] = useState([])
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      if (USE_MOCK) { await sleep(200); setLogs(MOCK_AUDIT) }
      else { const data = await apiFetch('/audit'); setLogs(data.logs || []) }
    } finally { setLoading(false) }
  }, [])

  return { logs, loading, load }
}

// Token Tracking (CISO-only Governance tab). Shape mirrors the live DDB table
// fronted by GET /token-usage and GET /token-usage/summary. In mock mode we
// filter MOCK_TOKEN_USAGE locally and derive summary client-side; in live mode
// the two endpoints fire in parallel — the summary is server-aggregated to
// avoid streaming 30 days of raw records just to populate KPI cards.
function _computeTokenSummary(records) {
  let inputT = 0, outputT = 0, cost = 0, blocked = 0
  const sessions = new Set()
  for (const r of records) {
    inputT  += r.input_tokens  || 0
    outputT += r.output_tokens || 0
    cost    += r.estimated_cost || 0
    if (r.guardrail_blocked) blocked++
    if (r.session_id) sessions.add(r.session_id)
  }
  const totalTokens = inputT + outputT
  const chats = sessions.size
  return {
    totalTokens, inputTokens: inputT, outputTokens: outputT,
    totalCost:  Number(cost.toFixed(6)),
    avgPerChat: chats > 0 ? Math.round(totalTokens / chats) : 0,
    chats, blocked,
  }
}

function _inferRangeId(filters) {
  if (!filters?.from) return '30d'
  const ms = Date.now() - new Date(filters.from).getTime()
  if (ms <= 25 * 3600_000)      return 'today'
  if (ms <= 8 * 24 * 3600_000)  return '7d'
  return '30d'
}

export function useTokenUsage() {
  const [records, setRecords] = useState([])
  const [summary, setSummary] = useState({
    totalTokens: 0, inputTokens: 0, outputTokens: 0,
    totalCost: 0, avgPerChat: 0, chats: 0, blocked: 0,
  })
  const [loading, setLoading] = useState(false)

  const load = useCallback(async (filters = {}) => {
    setLoading(true)
    try {
      if (USE_MOCK) {
        await sleep(150)
        let data = MOCK_TOKEN_USAGE
        if (filters.from)    data = data.filter(r => r.timestamp >= filters.from)
        if (filters.to)      data = data.filter(r => r.timestamp <= filters.to)
        if (filters.agent)   data = data.filter(r => r.agent === filters.agent)
        if (filters.persona) data = data.filter(r => r.persona === filters.persona)
        setRecords(data)
        setSummary(_computeTokenSummary(data))
      } else {
        const qs = new URLSearchParams()
        if (filters.from)    qs.set('from',    filters.from)
        if (filters.to)      qs.set('to',      filters.to)
        if (filters.agent)   qs.set('agent',   filters.agent)
        if (filters.persona) qs.set('persona', filters.persona)
        const sumQs = new URLSearchParams({ range: _inferRangeId(filters) })
        if (filters.agent)   sumQs.set('agent',   filters.agent)
        if (filters.persona) sumQs.set('persona', filters.persona)
        const [list, sum] = await Promise.all([
          apiFetch(`/token-usage?${qs.toString()}`),
          apiFetch(`/token-usage/summary?${sumQs.toString()}`),
        ])
        const rs = list.records || []
        setRecords(rs)
        setSummary(sum || _computeTokenSummary(rs))
      }
    } finally { setLoading(false) }
  }, [])

  return { records, summary, loading, load }
}
