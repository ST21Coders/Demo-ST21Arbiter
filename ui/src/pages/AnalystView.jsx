import { useEffect, useState, useRef } from 'react'
import {
  Send, Loader2, Bot, User, Lightbulb, Wifi, WifiOff,
  ShieldAlert, AlertTriangle, CheckCircle, FileText, Ticket, Bell, Archive,
  ExternalLink, ChevronDown, ChevronRight, Zap, MessageSquare, Plus, Trash2,
} from 'lucide-react'
import { useFindings, useChangeRequests, useConversations } from '../hooks/useApi'
import { SeverityBadge } from '../components/SeverityBadge'
import ActionRequestModal from '../components/ActionRequestModal'
import CreateTicketButton from '../components/CreateTicketButton'
import { detectProblem } from '../detectProblem'
import { CHAT_URL } from '../config'
import { sendChat } from '../hooks/useApi'

// Action types the agent itself can propose that already represent ticketing.
// When the last assistant turn carries one of these, we suppress the
// auto-surfaced Create Ticket button to avoid offering it twice.
const AGENT_TICKETING_ACTION_TYPES = new Set([
  'CREATE_CR', 'SERVICENOW_INC', 'SERVICENOW_RITM', 'SERVICENOW_CHG',
])

// ── Suggested questions ───────────────────────────────────────────────────────

const SUGGESTED = [
  'Is it safe to remove the Dropbox block in Zscaler? What approvals do I need?',
  'I want to fix the VPC peering violation. Walk me through the full impact and approval chain.',
  'We need to disable S3 replication to EU. What are the regulatory implications?',
  'How do I resolve the access review policy contradiction between v2.1 and v1.8?',
  'Summarise all open critical findings and what actions I need to take right now.',
]

// ── Action type config ────────────────────────────────────────────────────────

const ACTION_META = {
  CREATE_CR:        { icon: ShieldAlert, label: 'Create Change Request', color: 'text-indigo-700', bg: 'bg-indigo-50 border-indigo-200', btn: 'btn-primary' },
  SERVICENOW_INC:   { icon: AlertTriangle, label: 'Open Incident (INC)',  color: 'text-red-700',    bg: 'bg-red-50 border-red-200',     btn: 'btn-danger'  },
  SERVICENOW_RITM:  { icon: Ticket,        label: 'ServiceNow Request',   color: 'text-amber-700', bg: 'bg-amber-50 border-amber-200', btn: 'bg-amber-600 hover:bg-amber-500 text-white px-3 py-1.5 rounded-lg text-xs font-medium transition-colors' },
  SERVICENOW_CHG:   { icon: FileText,      label: 'ServiceNow Change',    color: 'text-indigo-700', bg: 'bg-indigo-50 border-indigo-200', btn: 'bg-indigo-600 hover:bg-indigo-500 text-white px-3 py-1.5 rounded-lg text-xs font-medium transition-colors' },
  NOTIFY_LEGAL:     { icon: Bell,          label: 'Notify Legal',         color: 'text-orange-700', bg: 'bg-orange-50 border-orange-200', btn: 'bg-orange-600 hover:bg-orange-500 text-white px-3 py-1.5 rounded-lg text-xs font-medium transition-colors' },
  ARCHIVE_DOC:      { icon: Archive,       label: 'Archive Document',     color: 'text-slate-600',   bg: 'bg-slate-50 border-slate-200',       btn: 'btn-ghost'   },
}

const PRIORITY_LABEL = { 1: 'Immediate', 2: 'Same Day', 3: 'This Week' }
const PRIORITY_COLOR  = { 1: 'text-red-700', 2: 'text-amber-700', 3: 'text-indigo-700' }

// ── ServiceNow mock modal ─────────────────────────────────────────────────────

function SnowModal({ action, onClose }) {
  const [submitted, setSubmitted] = useState(false)
  const [ticketId] = useState(() => {
    const prefix = action.type === 'SERVICENOW_INC' ? 'INC' : action.type === 'SERVICENOW_RITM' ? 'RITM' : 'CHG'
    return `${prefix}${String(Math.floor(1000000 + Math.random() * 8999999))}`
  })

  async function submit() {
    await new Promise(r => setTimeout(r, 1200))
    setSubmitted(true)
  }

  return (
    <div className="fixed inset-0 bg-slate-900/40 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-white border border-slate-200 rounded-2xl w-full max-w-lg slide-in shadow-xl">
        <div className="flex items-center justify-between p-5 border-b border-slate-200">
          <h2 className="font-bold text-slate-900 text-base">{action.label}</h2>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-900 text-sm">✕</button>
        </div>
        {submitted ? (
          <div className="p-6 text-center space-y-3">
            <CheckCircle size={40} className="text-emerald-600 mx-auto" />
            <p className="text-slate-900 font-semibold">Ticket Created</p>
            <p className="text-slate-600 text-sm">ServiceNow ticket <span className="text-emerald-700 font-mono font-bold">{ticketId}</span> has been submitted.</p>
            <p className="text-slate-400 text-xs">Assignment group: {action.snow_assignment || 'Security Operations'}</p>
            <button onClick={onClose} className="btn-primary mt-2">Close</button>
          </div>
        ) : (
          <div className="p-5 space-y-4">
            <div>
              <label className="block text-xs text-slate-600 mb-1.5">Short Description</label>
              <input readOnly value={action.snow_short_desc || action.label} className="input w-full text-sm bg-slate-50" />
            </div>
            <div>
              <label className="block text-xs text-slate-600 mb-1.5">Description</label>
              <textarea readOnly rows={4} value={action.snow_description || action.description} className="input w-full text-sm resize-none bg-slate-50" />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs text-slate-600 mb-1.5">Urgency</label>
                <input readOnly value={`${action.snow_urgency || '2'} - ${action.snow_urgency === '1' ? 'Critical' : action.snow_urgency === '3' ? 'Medium' : 'High'}`} className="input w-full text-sm bg-slate-50" />
              </div>
              <div>
                <label className="block text-xs text-slate-600 mb-1.5">Assignment Group</label>
                <input readOnly value={action.snow_assignment || 'Security Operations'} className="input w-full text-sm bg-slate-50" />
              </div>
            </div>
            <div className="bg-amber-50 border border-amber-200 rounded-lg p-3 text-xs text-amber-800">
              POC mode — this will simulate ServiceNow submission. In production, this calls the ServiceNow REST API.
            </div>
            <div className="flex gap-3 pt-1">
              <button onClick={onClose} className="btn-ghost flex-1">Cancel</button>
              <button onClick={submit} className="btn-primary flex-1">Submit to ServiceNow</button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

// ── Action cards ──────────────────────────────────────────────────────────────

function ActionCards({ actions, onCreateCR }) {
  const [snowAction, setSnowAction] = useState(null)
  const [notified, setNotified] = useState({})

  if (!actions?.length) return null

  function handleNotify(idx) {
    setNotified(p => ({ ...p, [idx]: true }))
  }

  return (
    <div className="mt-4 space-y-2">
      <div className="flex items-center gap-2 mb-2">
        <Zap size={13} className="text-amber-600" />
        <p className="text-xs text-amber-700 font-semibold uppercase tracking-wider">Recommended Actions — Pending Your Approval</p>
      </div>
      {actions.map((action, i) => {
        const meta  = ACTION_META[action.type] || ACTION_META.CREATE_CR
        const Icon  = meta.icon
        const prio  = action.priority || 2
        return (
          <div key={i} className={`rounded-xl border p-3.5 ${meta.bg}`}>
            <div className="flex items-start gap-3">
              <Icon size={16} className={`${meta.color} flex-shrink-0 mt-0.5`} />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 flex-wrap mb-1">
                  <p className="text-sm font-semibold text-slate-900">{action.label}</p>
                  <span className={`text-xs font-medium ${PRIORITY_COLOR[prio]}`}>
                    P{prio} — {PRIORITY_LABEL[prio]}
                  </span>
                </div>
                <p className="text-xs text-slate-700 leading-relaxed">{action.description}</p>

                {action.requires_approval && action.approval_chain?.length > 0 && (
                  <div className="flex items-center gap-1 flex-wrap mt-1.5">
                    <span className="text-xs text-slate-500">Approvers:</span>
                    {action.approval_chain.map((a, j) => (
                      <span key={j} className="text-xs bg-white border border-slate-200 text-slate-700 px-2 py-0.5 rounded-full">{a}</span>
                    ))}
                  </div>
                )}

                {action.blocking_policies?.length > 0 && (
                  <p className="text-xs text-red-700 mt-1">
                    Blocking: {action.blocking_policies.join(', ')}
                  </p>
                )}
              </div>
              <div className="flex-shrink-0 ml-2">
                {action.type === 'CREATE_CR' && (
                  <button
                    onClick={() => onCreateCR(action)}
                    className={`${meta.btn} text-xs px-3 py-1.5`}
                  >
                    Create CR
                  </button>
                )}
                {['SERVICENOW_INC', 'SERVICENOW_RITM', 'SERVICENOW_CHG'].includes(action.type) && (
                  <button
                    onClick={() => setSnowAction(action)}
                    className={`${meta.btn} text-xs px-3 py-1.5`}
                  >
                    Submit
                  </button>
                )}
                {action.type === 'NOTIFY_LEGAL' && (
                  notified[i]
                    ? <span className="text-xs text-emerald-700 flex items-center gap-1"><CheckCircle size={11} /> Sent</span>
                    : <button onClick={() => handleNotify(i)} className={`${meta.btn} text-xs px-3 py-1.5`}>Notify</button>
                )}
                {action.type === 'ARCHIVE_DOC' && (
                  <button onClick={() => setSnowAction({ ...action, type: 'SERVICENOW_RITM', snow_short_desc: `Archive: ${action.target_resource || action.label}`, snow_assignment: 'IAM / Policy Team' })}
                    className={`${meta.btn} text-xs px-3 py-1.5`}>
                    Archive
                  </button>
                )}
              </div>
            </div>
          </div>
        )
      })}

      {snowAction && (
        <SnowModal action={snowAction} onClose={() => setSnowAction(null)} />
      )}
    </div>
  )
}

// ── Message bubble ────────────────────────────────────────────────────────────

function Message({ msg, onCreateCR }) {
  const isUser = msg.role === 'user'
  return (
    <div className={`flex gap-3 ${isUser ? 'flex-row-reverse' : ''}`}>
      <div className={`w-7 h-7 rounded-full flex items-center justify-center flex-shrink-0 mt-0.5 ${isUser ? 'bg-indigo-600 text-white' : 'bg-slate-100 text-slate-700 border border-slate-200'}`}>
        {isUser ? <User size={13} /> : <Bot size={13} />}
      </div>
      <div className={`max-w-[80%] ${isUser ? '' : 'flex-1'}`}>
        <div className={`rounded-2xl px-4 py-3 text-sm leading-relaxed whitespace-pre-wrap ${
          isUser ? 'bg-indigo-600 text-white rounded-br-sm' : 'bg-white border border-slate-200 text-slate-800 rounded-bl-sm'
        }`}>
          {msg.content}
        </div>
        {!isUser && msg.actions?.length > 0 && (
          <ActionCards actions={msg.actions} onCreateCR={onCreateCR} />
        )}
      </div>
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

function initialGreeting() {
  return [{
    role: 'assistant',
    system: true,
    content: CHAT_URL
      ? "Hello! I'm ARBITER's AI Governance Agent, powered by Claude Sonnet with live tool-calling.\n\nAsk me about any policy change, system modification, or compliance question. I'll scan the knowledge base, check for existing conflicts, identify policy owners, assess system impact, check for simpler alternatives, and recommend concrete actions (Change Requests, ServiceNow tickets) — all keeping you in the loop before anything is executed."
      : "Hello! I'm running in mock mode (no VITE_CHAT_URL set).",
    actions: [],
  }]
}

export default function AnalystView() {
  const { findings, load: loadFindings } = useFindings()
  const { changeRequests, load: loadCRs, createAction } = useChangeRequests()
  const bottomRef = useRef(null)

  const [messages, setMessages] = useState(initialGreeting)
  const [input, setInput] = useState('')
  const [thinking, setThinking] = useState(false)
  const [pendingCR, setPendingCR] = useState(null)
  // Lazily-generated session id. Cleared by newChat() so the next send mints
  // a fresh id; replaced by openSession() when the user picks an old chat.
  const sessionIdRef = useRef(null)
  const [activeSessionId, setActiveSessionId] = useState(null)

  // Analyst-only session list (server-side filtered by chat_type='analyst').
  const {
    sessions, list: listSessions, loadMessages,
    addLocalSession, bumpLocalSession, deleteSession,
  } = useConversations({ type: 'analyst' })

  useEffect(() => { loadFindings(); loadCRs() }, [loadFindings, loadCRs])
  useEffect(() => { listSessions() }, [listSessions])
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages, thinking])

  function newChat() {
    sessionIdRef.current = null
    setActiveSessionId(null)
    setMessages(initialGreeting())
  }

  async function openSession(sessionId) {
    if (sessionId === activeSessionId) return
    sessionIdRef.current = sessionId
    setActiveSessionId(sessionId)
    setMessages([{ role: 'assistant', system: true, content: 'Loading conversation…', actions: [] }])
    try {
      const data = await loadMessages(sessionId)
      // Map memory shape {role, content, ts, tool_calls} → local {role, content, actions}
      const mapped = (data.messages || []).map(m => ({
        role: m.role,
        content: m.content,
        actions: [],
      }))
      setMessages(mapped.length ? mapped : initialGreeting())
    } catch (err) {
      setMessages([{ role: 'assistant', system: true, content: `⚠ Could not load session: ${err.message}`, actions: [] }])
    }
  }

  async function send(text) {
    const q = (text || input).trim()
    if (!q || thinking) return
    setInput('')

    const userMsg = { role: 'user', content: q, actions: [] }
    setMessages(prev => [...prev, userMsg])
    setThinking(true)

    try {
      let responseText = ''
      let actions = []

      // First turn of a fresh chat → mint a session id and optimistically
      // push it onto the sidebar so the user sees it immediately. Done in
      // both live and mock mode so a chat can be created — and then deleted —
      // on localhost (mock) without a live CHAT_URL.
      if (!sessionIdRef.current) {
        sessionIdRef.current = `sess-${crypto.randomUUID().replace(/-/g, '').slice(0, 12)}`
        setActiveSessionId(sessionIdRef.current)
        addLocalSession({
          session_id: sessionIdRef.current,
          title: q.slice(0, 80),
          chat_type: 'analyst',
          created_at: new Date().toISOString(),
          last_message_at: new Date().toISOString(),
          message_count: 0,
        })
      }

      if (CHAT_URL) {
        // Use the shared sendChat() helper — attaches Cognito JWT and stamps
        // chat_type:'analyst' so this row is filterable in /conversations.
        const data = await sendChat({
          prompt: q,
          session_id: sessionIdRef.current,
          chat_type: 'analyst',
        })
        responseText = data.reply || ''
        actions = data.actions || []
      } else {
        await new Promise(r => setTimeout(r, 800))
        responseText = getMockResponse(q, findings, changeRequests)
      }
      bumpLocalSession(sessionIdRef.current, 2)

      setMessages(prev => [...prev, { role: 'assistant', content: responseText, actions }])
    } catch (err) {
      setMessages(prev => [...prev, { role: 'assistant', system: true, content: `⚠ Agent error: ${err.message}`, actions: [] }])
    } finally {
      setThinking(false)
    }
  }

  function handleKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
  }

  function handleCreateCR(action) {
    setPendingCR({
      action_type:        action.action_type || 'SECURITY_FIX',
      target_resource:    action.target_resource || '',
      target_environment: action.target_environment || 'PROD',
      severity:           action.severity || 'HIGH',
      justification:      action.justification || action.description || '',
      requesting_team:    action.requesting_team || '',
    })
  }

  // Trash icon on a sidebar row. Confirmation prompt → deleteSession. If the
  // user nukes the chat they currently have open, reset to the new-chat state.
  async function handleDeleteSession(sessionId, ev) {
    ev?.stopPropagation?.()
    if (!sessionId) return
    if (!window.confirm('Delete this chat? This cannot be undone.')) return
    try {
      await deleteSession(sessionId)
    } catch (err) {
      console.warn('Delete session failed:', err)
    }
    if (sessionId === activeSessionId) {
      sessionIdRef.current = null
      setActiveSessionId(null)
      setMessages(initialGreeting())
    }
  }

  return (
    <div className="flex h-full overflow-hidden" style={{ height: 'calc(100vh - 0px)' }}>

      {/* History sidebar (Analyst sessions). Hidden under lg to keep the
          page dense on narrow viewports — Try Asking + chat already use width. */}
      <aside className="hidden lg:flex w-56 flex-shrink-0 flex-col border-r border-slate-200 bg-white">
        <div className="flex items-center justify-between px-4 py-3 border-b border-slate-200">
          <div className="flex items-center gap-1.5">
            <MessageSquare size={11} className="text-slate-500" />
            <p className="text-[10px] font-bold text-slate-600 uppercase tracking-wider">Conversations</p>
          </div>
          <button
            onClick={newChat}
            title="Start a new chat"
            className="flex items-center gap-0.5 text-[10px] text-indigo-600 hover:text-indigo-800"
          >
            <Plus size={10} /> New
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-2 space-y-0.5">
          {sessions.length === 0 ? (
            <p className="text-[10px] text-slate-400 italic px-2 py-1">No history yet</p>
          ) : sessions.map(s => (
            <div
              key={s.session_id}
              className={`group relative w-full rounded text-xs hover:bg-slate-100 transition-colors ${
                activeSessionId === s.session_id ? 'bg-indigo-50 border border-indigo-200' : ''
              }`}
            >
              <button
                onClick={() => openSession(s.session_id)}
                className="w-full text-left pl-2 pr-7 py-1.5"
              >
                <p className="font-medium text-slate-800 truncate">{s.title || s.session_id}</p>
                <p className="text-[10px] text-slate-500 truncate">
                  {s.message_count || 0} msgs · {s.last_message_at ? new Date(s.last_message_at).toLocaleDateString() : ''}
                </p>
              </button>
              <button
                onClick={(e) => handleDeleteSession(s.session_id, e)}
                title="Delete chat"
                aria-label="Delete chat"
                className="absolute top-1/2 right-1 -translate-y-1/2 p-1 rounded text-slate-400 hover:text-red-600 hover:bg-red-50 opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity"
              >
                <Trash2 size={11} />
              </button>
            </div>
          ))}
        </div>
      </aside>

      {/* Chat panel */}
      <div className="flex-1 flex flex-col min-w-0 border-r border-slate-200">
        {/* Header */}
        <div className="flex items-center gap-2 px-5 py-3 border-b border-slate-200 bg-white flex-shrink-0">
          <Bot size={16} className="text-indigo-600" />
          <span className="font-semibold text-slate-900 text-sm">ARBITER Governance Agent</span>
          <span className="text-xs text-slate-500 ml-auto">claude-sonnet-4-6 · tool-calling · human-in-the-loop</span>
          {activeSessionId && (
            <button
              onClick={handleResolve}
              title="Mark conversation resolved and archive it"
              className="flex items-center gap-1 text-xs text-emerald-700 bg-emerald-50 hover:bg-emerald-100 border border-emerald-200 px-2 py-0.5 rounded-full transition-colors"
            >
              <CheckCircle size={11} /> Resolve
            </button>
          )}
          {CHAT_URL ? (
            <span className="flex items-center gap-1 text-xs text-emerald-700 bg-emerald-50 border border-emerald-200 px-2 py-0.5 rounded-full">
              <Wifi size={10} /> Live
            </span>
          ) : (
            <span className="flex items-center gap-1 text-xs text-amber-700 bg-amber-50 border border-amber-200 px-2 py-0.5 rounded-full">
              <WifiOff size={10} /> Mock
            </span>
          )}
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-5 bg-slate-50">
          {messages.map((m, i) => {
            const isLastAssistant =
              m.role === 'assistant' && i === messages.length - 1 && !thinking
            const agentSuggestedTicket = (m.actions || []).some(a => AGENT_TICKETING_ACTION_TYPES.has(a.type))
            const detected = isLastAssistant && !m.ticketCreated && !agentSuggestedTicket
              ? detectProblem({ messages: messages.slice(0, i + 1), sessionId: sessionIdRef.current })
              : null
            return (
              <div key={i}>
                <Message msg={m} onCreateCR={handleCreateCR} />
                {detected?.hasProblem && (
                  <div className="ml-10 mt-1">
                    <CreateTicketButton detected={detected} />
                  </div>
                )}
              </div>
            )
          })}
          {thinking && (
            <div className="flex gap-3">
              <div className="w-7 h-7 rounded-full bg-slate-100 border border-slate-200 flex items-center justify-center flex-shrink-0">
                <Bot size={13} className="text-slate-700" />
              </div>
              <div className="bg-white border border-slate-200 rounded-2xl rounded-bl-sm px-4 py-3 flex flex-col gap-1.5 max-w-sm">
                <div className="flex items-center gap-2 text-xs text-slate-600">
                  <Loader2 size={12} className="animate-spin" /> Scanning policies and knowledge base…
                </div>
                <div className="flex items-center gap-1.5">
                  <span className="w-1.5 h-1.5 bg-indigo-500 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
                  <span className="w-1.5 h-1.5 bg-indigo-500 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
                  <span className="w-1.5 h-1.5 bg-indigo-500 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
                </div>
              </div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        {/* Input */}
        <div className="px-5 py-3 border-t border-slate-200 flex gap-2 flex-shrink-0 bg-white">
          <textarea
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKey}
            rows={2}
            placeholder="Ask about a policy change, system impact, approval needs, or regulatory risk…"
            className="input flex-1 resize-none text-sm"
          />
          <button
            onClick={() => send()}
            disabled={!input.trim() || thinking}
            className="btn-primary self-end px-3"
          >
            {thinking ? <Loader2 size={16} className="animate-spin" /> : <Send size={16} />}
          </button>
        </div>
      </div>

      {/* Side panel */}
      <div className="w-72 flex-shrink-0 flex flex-col overflow-y-auto bg-white">
        {/* Suggested questions */}
        <div className="p-4 border-b border-slate-200">
          <div className="flex items-center gap-1.5 mb-3">
            <Lightbulb size={13} className="text-amber-600" />
            <p className="text-xs font-semibold text-slate-600 uppercase tracking-wider">Try asking…</p>
          </div>
          <div className="space-y-1.5">
            {SUGGESTED.map(q => (
              <button
                key={q}
                onClick={() => send(q)}
                disabled={thinking}
                className="w-full text-left text-xs text-slate-600 hover:text-slate-900 bg-slate-50 hover:bg-slate-100 border border-slate-200 px-3 py-2 rounded-lg transition-colors leading-relaxed"
              >
                {q}
              </button>
            ))}
          </div>
        </div>

        {/* Agent tools */}
        <div className="p-4 border-b border-slate-200">
          <p className="text-xs font-semibold text-slate-600 uppercase tracking-wider mb-2">Agent Tools</p>
          {/* The first three match the @tool decorators in
              agents/master_orchestrator/agent.py:329-338. The "PLANNED" entries
              telegraph the roadmap to F4 (analyst chat over live findings) per
              Documents/Feature_Coverage_Plan.md §3 steps 6–9. */}
          <div className="space-y-1 text-xs text-slate-500">
            {[
              { label: 'sharepoint_lookup — KB retrieval over MIG-POL docs', planned: false },
              { label: 'awsconfig_lookup — AWS Config compliance lookup',     planned: false },
              { label: 'zscaler_lookup — Zscaler policy retrieval',           planned: false },
              { label: 'query_conflicts — live findings from conflicts-v2',   planned: true },
              { label: 'query_scan_runs — recent scan history + totals',     planned: true },
            ].map(t => (
              <div key={t.label} className="flex items-start gap-1.5">
                <span className={t.planned ? 'text-slate-300 mt-0.5' : 'text-indigo-500 mt-0.5'}>›</span>
                <span className={t.planned ? 'text-slate-400 italic' : ''}>
                  {t.label}
                  {t.planned && <span className="ml-1 text-[10px] text-slate-400 bg-slate-100 border border-slate-200 px-1 rounded">PLANNED</span>}
                </span>
              </div>
            ))}
          </div>
        </div>

        {/* Open critical findings */}
        <div className="p-4">
          <p className="text-xs font-semibold text-slate-600 uppercase tracking-wider mb-2">Open Critical Findings</p>
          {findings.filter(f => f.severity === 'CRITICAL' && f.status === 'OPEN').length === 0
            ? <p className="text-xs text-slate-400">None</p>
            : findings.filter(f => f.severity === 'CRITICAL' && f.status === 'OPEN').map(f => (
                <div key={f.conflict_id} className="mb-2 pb-2 border-b border-slate-100 last:border-0">
                  <div className="flex items-center gap-1.5 mb-0.5">
                    <SeverityBadge severity={f.severity} />
                    <span className="text-xs font-mono text-slate-500">{f.conflict_id}</span>
                  </div>
                  <p className="text-xs text-slate-700 leading-snug">{f.title}</p>
                </div>
              ))
          }
        </div>
      </div>

      {/* CR Modal */}
      {pendingCR && (
        <ActionRequestModal
          conflict={null}
          onClose={result => {
            setPendingCR(null)
            if (result) loadCRs()
          }}
          onCreate={createAction}
          prefill={pendingCR}
        />
      )}
    </div>
  )
}

// ── Mock fallback (when CHAT_URL not set) ─────────────────────────────────────

function getMockResponse(q, findings, changeRequests) {
  const ql = q.toLowerCase()
  if (ql.includes('dropbox') || ql.includes('uc01'))
    return `**ARBITER Analysis: Zscaler / Dropbox (ARBITER-UC01)**\n\n**Safety:** Safe to remove the block — Dropbox Business is explicitly approved in MIG-POL-001-CS01 §2.1.\n\n**Governing Policy:** MIG-POL-001-CS01 v3.4 approves Dropbox Business for all MIG employees. Zscaler rule ZIA-URLCAT-CLOUD-BLK-042 is in direct conflict.\n\n**Remediation:** Remove dropbox.com from ZIA-URLCAT-CLOUD-BLK-042 or re-categorise to "Cloud Storage — Allowed".\n\n**Approvals needed (PROD/HIGH):** CISO + VPE (MIG standard approval matrix).\n\nStart local_server.py and set VITE_CHAT_URL for full agent analysis with action buttons.`
  if (ql.includes('vpc') || ql.includes('peering') || ql.includes('uc08') || ql.includes('sg-mig-prod'))
    return `**ARBITER Analysis: Dev-to-Prod VPC Peering (ARBITER-UC08)**\n\n**Safety: CRITICAL — fix immediately.** Active 78-day PCI DSS 4.0 r1.3.2 segmentation failure.\n\n**Governing Policy:** MIG-POL-004-SEG01 §3 prohibits any direct packet path between production and non-production VPCs.\n\n**Approvals (PROD/CRITICAL):** CISO + VPE + Legal notification required (MIG standard).\n\n**System Impact:** Production databases accessible from dev VPC. QSA finding at next PCI assessment.\n\nStart local_server.py for full agentic analysis with Create CR and ServiceNow INC buttons.`
  return `I found ${findings.length} conflicts and ${changeRequests.length} change requests. For full agent analysis with tool-calling, start local_server.py and set VITE_CHAT_URL=http://localhost:8000 in ui/.env.local.`
}
