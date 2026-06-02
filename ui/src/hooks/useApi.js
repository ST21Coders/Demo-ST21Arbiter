import { useState, useEffect, useCallback } from 'react'
import { API_URL, CHAT_URL, USE_MOCK } from '../config'
import { MOCK_CONFLICTS, MOCK_CHANGE_REQUESTS, MOCK_AUDIT } from '../mockData'
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
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json()
}

// Simulated scan delay for mock mode
function sleep(ms) { return new Promise(r => setTimeout(r, ms)) }

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
      const cr = {
        cr_id: `CR-${Date.now()}`,
        status: payload.target_environment === 'DEV' ? 'AUTO_APPROVED' : 'PENDING_APPROVAL',
        ...payload,
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
    return apiFetch(`/actions/${crId}/approve`, { method: 'POST', body: JSON.stringify({ approver_email: approverEmail, approver_role: approverRole, comment }) })
  }, [])

  const reject = useCallback(async (crId, approverEmail, reason) => {
    if (USE_MOCK) {
      await sleep(400)
      setChangeRequests(prev => prev.map(cr => cr.cr_id === crId ? { ...cr, status: 'REJECTED' } : cr))
      return {}
    }
    return apiFetch(`/actions/${crId}/reject`, { method: 'POST', body: JSON.stringify({ approver_email: approverEmail, reason }) })
  }, [])

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
    return apiFetch(`/actions/${crId}/execute`, { method: 'POST', body: JSON.stringify({ executed_by: 'operator@meridianinsurance.com' }) })
  }, [])

  const escalate = useCallback(async (crId, reason) => {
    if (USE_MOCK) {
      await sleep(300)
      setChangeRequests(prev => prev.map(cr => cr.cr_id === crId ? { ...cr, status: 'ESCALATED' } : cr))
      return {}
    }
    return apiFetch(`/actions/${crId}/escalate`, { method: 'POST', body: JSON.stringify({ reason }) })
  }, [])

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

  // Hard-delete a session: removes the DDB index row and best-effort drains
  // the memory events. UI calls this for the per-chat trash button, the
  // "Resolve" button, and the auto-archive when a linked CR completes.
  // Optimistically removes from local state before the network call so the
  // sidebar feels instant; on error, the next list() pull will restore.

  return {
    sessions, activeMessages, loading,
    list, loadMessages, clearActive,
    addLocalSession, bumpLocalSession
  }
}

// Hits the master orchestrator via the Lambda Function URL (CHAT_URL) so we
// bypass API Gateway's 29s timeout. Body shape: { prompt, session_id, chat_type }.
// chat_type ('analyst' | 'mcp') is stamped onto new session rows so the two
// chats can be listed separately. Response: { reply, session_id }.
export async function sendChat({ prompt, session_id, chat_type }) {
  if (USE_MOCK || !CHAT_URL) {
    await sleep(600 + Math.random() * 800)
    return { reply: `(mock reply) You asked: "${prompt}". Wire VITE_CHAT_URL to get a real answer.`, session_id }
  }
  const res = await fetch(`${CHAT_URL}chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ prompt, session_id, chat_type: chat_type || 'analyst' }),
  })
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json()
}

// Live nav badge counts. Polls every 60s; cancels on unmount.
// findingsOpen = conflicts with status === 'OPEN'; actionsPending = CRs with
// status === 'PENDING_APPROVAL'. Mock mode reads from MOCK_*.
export function useNavCounts() {
  const [findingsOpen, setFindingsOpen] = useState(0)
  const [actionsPending, setActionsPending] = useState(0)

  useEffect(() => {
    let cancelled = false
    const tick = async () => {
      try {
        if (USE_MOCK) {
          if (cancelled) return
          setFindingsOpen(MOCK_CONFLICTS.filter(f => f.status === 'OPEN').length)
          setActionsPending(MOCK_CHANGE_REQUESTS.filter(c => c.status === 'PENDING_APPROVAL').length)
        } else {
          const [findings, actions] = await Promise.all([
            apiFetch('/findings'),
            apiFetch('/actions'),
          ])
          if (cancelled) return
          setFindingsOpen((findings.findings || []).filter(f => f.status === 'OPEN').length)
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

  return { findingsOpen, actionsPending }
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
