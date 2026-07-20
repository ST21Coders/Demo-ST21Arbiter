import { useState, useRef, useEffect } from 'react'
import { Rabbit, Send, Loader2, MessageSquare, Plus, Wifi, WifiOff } from 'lucide-react'
import { CHAT_URL } from '../config'
import { sendChat, useConversations, useAgentStatus } from '../hooks/useApi'
import { AGENT_CATALOG, findAgent } from '../agentCatalog'

/* ─── Status mapping (mirrors MCPChat.deriveStatus) ──────────────────
   Unknown (status not yet loaded) stays chat-enabled so a transient
   /agent-status hiccup never blocks a real agent. */
function deriveStatus(raw) {
  if (!raw) return { bucket: 'PENDING', label: 'CHECKING…', chat: true }
  if (raw === 'READY') return { bucket: 'ONLINE', label: 'READY', chat: true }
  if (raw === 'PLACEHOLDER') return { bucket: 'OFFLINE', label: 'NOT DEPLOYED', chat: false }
  if (raw.endsWith('FAILED') || raw === 'DELETING')
    return { bucket: 'OFFLINE', label: raw, chat: false }
  return { bucket: 'DEGRADED', label: raw, chat: true } // CREATING / UPDATING / …
}

const DOT_CLASS = { ONLINE: 'bg-emerald-500', DEGRADED: 'bg-amber-500', PENDING: 'bg-slate-400', OFFLINE: 'bg-red-500' }
const TEXT_CLASS = { ONLINE: 'text-emerald-600', DEGRADED: 'text-amber-600', PENDING: 'text-slate-500', OFFLINE: 'text-red-600' }

const DRAFT_KEY = 'arbiter.smartRabbit.sessionDraft.v1'

function readDraft() {
  if (typeof window === 'undefined') return null
  try {
    const draft = JSON.parse(sessionStorage.getItem(DRAFT_KEY) || 'null')
    if (!draft || !Array.isArray(draft.messages)) return null
    return draft
  } catch {
    return null
  }
}

function writeDraft(draft) {
  if (typeof window === 'undefined') return
  try {
    sessionStorage.setItem(DRAFT_KEY, JSON.stringify(draft))
  } catch {
    // Best-effort only; chat still works if the browser denies storage.
  }
}

function Message({ msg }) {
  if (msg.role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[75%] bg-indigo-50 border border-indigo-200 rounded-xl px-4 py-2.5 text-sm text-slate-800">
          {msg.content}
        </div>
      </div>
    )
  }
  return (
    <div className="flex gap-3">
      <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-slate-100 to-slate-200 border border-slate-200 flex items-center justify-center flex-shrink-0 mt-0.5">
        <Rabbit size={13} className="text-indigo-600" />
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-sm text-slate-800 leading-relaxed">
          {String(msg.content || '').split('\n').map((line, i) => {
            if (line.startsWith('```') || line.includes('```')) return null
            if (line.startsWith('**') && line.endsWith('**')) {
              return <p key={i} className="font-semibold text-slate-900 mb-1">{line.replace(/\*\*/g, '')}</p>
            }
            if (/^(-\s|\d+\.\s)/.test(line)) {
              return <p key={i} className="text-slate-700 text-xs my-0.5 ml-2">{line}</p>
            }
            if (line.startsWith('|') && line.endsWith('|')) {
              return <p key={i} className="font-mono text-xs text-slate-600 my-0.5">{line}</p>
            }
            if (line.trim() === '') return <div key={i} className="h-1" />
            return <p key={i} className="text-slate-700 text-xs my-0.5">{line.replace(/\*\*/g, '')}</p>
          })}
        </div>
        <p className="text-[10px] text-slate-400 mt-1.5">{msg.time}</p>
      </div>
    </div>
  )
}

export default function SmartRabbit() {
  const restoredRef = useRef(readDraft())
  const restored = restoredRef.current
  const restoredHit = restored?.selectedAgentId ? findAgent(restored.selectedAgentId) : null
  const initialGroup = restoredHit?.group || AGENT_CATALOG[0]
  const initialAgent = restoredHit?.agent || initialGroup.agents[0]

  const [selectedGroupId, setSelectedGroupId] = useState(initialGroup.id)
  const [selectedAgentId, setSelectedAgentId] = useState(initialAgent.id)
  const [messages, setMessages] = useState(() => restored?.messages || [])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [activeSessionId, setActiveSessionId] = useState(restored?.activeSessionId || null)
  const bottomRef = useRef(null)
  const statusById = useAgentStatus()
  const { addLocalSession, bumpLocalSession } = useConversations({ type: 'rabbit' })

  const group = AGENT_CATALOG.find(g => g.id === selectedGroupId) || AGENT_CATALOG[0]
  const agent = group.agents.find(a => a.id === selectedAgentId) || group.agents[0]
  const status = deriveStatus(statusById[agent.id])

  const introMessage = (g, a) => ({
    role: 'assistant',
    system: true,
    content: `You're chatting with **${a.name}** from the **${g.name}** catalog group.\n\n${a.description}`,
    time: new Date().toLocaleTimeString(),
  })

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  useEffect(() => {
    writeDraft({ selectedAgentId: agent.id, activeSessionId, messages })
  }, [agent.id, activeSessionId, messages])

  // First mount with no restored transcript → intro for the initial agent.
  useEffect(() => {
    setMessages(prev => prev.length ? prev : [introMessage(group, agent)])
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  /* Switching group/agent keeps the conversation (like the Analyst page's
     group selector): the transcript stays, a system note marks the handoff,
     and the NEXT send() routes to the newly selected agent. */
  function switchAgent(nextGroupId, nextAgentId) {
    const nextGroup = AGENT_CATALOG.find(g => g.id === nextGroupId) || AGENT_CATALOG[0]
    const nextAgent = nextGroup.agents.find(a => a.id === nextAgentId) || nextGroup.agents[0]
    setSelectedGroupId(nextGroup.id)
    setSelectedAgentId(nextAgent.id)
    if (nextAgent.id === agent.id) return
    setMessages(prev => [...prev, {
      role: 'assistant',
      system: true,
      content: `Switched to **${nextAgent.name}** (${nextGroup.name}). Follow-up questions now go to this agent.`,
      time: new Date().toLocaleTimeString(),
    }])
  }

  function newChat() {
    setActiveSessionId(null)
    setInput('')
    setMessages([introMessage(group, agent)])
  }

  async function send() {
    const q = input.trim()
    if (!q || loading || !status.chat) return
    setInput('')
    setMessages(prev => [...prev, { role: 'user', content: q, time: new Date().toLocaleTimeString() }])
    setLoading(true)

    let sid = activeSessionId
    if (!sid) {
      sid = `sess-${crypto.randomUUID().replace(/-/g, '').slice(0, 12)}`
      setActiveSessionId(sid)
      addLocalSession({
        session_id: sid,
        title: q.slice(0, 80),
        chat_type: 'rabbit',
        created_at: new Date().toISOString(),
        last_message_at: new Date().toISOString(),
        message_count: 0,
      })
    }

    try {
      const { reply } = await sendChat({
        prompt: q,
        session_id: sid,
        chat_type: 'rabbit',
        target: agent.id,
      })
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: reply,
        time: new Date().toLocaleTimeString(),
      }])
      bumpLocalSession(sid, 2)
    } catch (e) {
      setMessages(prev => [...prev, {
        role: 'assistant',
        system: true,
        content: `⚠️ Chat failed: ${e.message || e}`,
        time: new Date().toLocaleTimeString(),
      }])
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Header: title + catalog group/agent selectors + status */}
      <div className="px-5 py-3 border-b border-slate-200 bg-white flex items-center gap-4 flex-wrap">
        <div className="flex items-center gap-2 min-w-0">
          <div className="w-8 h-8 rounded-lg bg-indigo-600 flex items-center justify-center flex-shrink-0">
            <Rabbit size={16} className="text-white" />
          </div>
          <div className="min-w-0">
            <h1 className="text-sm font-bold text-slate-900 leading-tight">Smart Rabbit</h1>
            <p className="text-[10px] text-slate-500">Agent catalog — pick a group and an agent, switch any time mid-conversation</p>
          </div>
        </div>

        <div className="flex items-center gap-2 ml-auto flex-wrap">
          <label className="flex items-center gap-1.5 text-xs text-slate-600">
            Group
            <select
              value={group.id}
              onChange={(e) => switchAgent(e.target.value, '')}
              className="rounded-md border border-slate-200 bg-white px-2 py-1 text-xs outline-none focus:border-indigo-400"
            >
              {AGENT_CATALOG.map(g => (
                <option key={g.id} value={g.id}>{g.name}</option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-1.5 text-xs text-slate-600">
            Agent
            <select
              value={agent.id}
              onChange={(e) => switchAgent(group.id, e.target.value)}
              className="rounded-md border border-slate-200 bg-white px-2 py-1 text-xs outline-none focus:border-indigo-400"
            >
              {group.agents.map(a => (
                <option key={a.id} value={a.id}>{a.name}</option>
              ))}
            </select>
          </label>
          <span className="flex items-center gap-1.5 text-[10px]">
            <span className={`w-1.5 h-1.5 rounded-full ${DOT_CLASS[status.bucket] || 'bg-slate-400'}`} />
            <span className={TEXT_CLASS[status.bucket] || 'text-slate-500'}>{status.label}</span>
          </span>
          <span className="flex items-center gap-1 text-[10px] text-slate-500">
            {CHAT_URL ? <Wifi size={11} className="text-emerald-500" /> : <WifiOff size={11} className="text-amber-500" />}
            {CHAT_URL ? 'Live' : 'Mock'}
          </span>
          <button
            onClick={newChat}
            className="flex items-center gap-1 text-xs text-slate-600 border border-slate-200 rounded-md px-2 py-1 hover:bg-slate-50"
          >
            <Plus size={12} /> New chat
          </button>
        </div>
      </div>

      {/* Transcript */}
      <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4 bg-slate-50/50">
        {messages.map((m, i) => <Message key={i} msg={m} />)}
        {loading && (
          <div className="flex items-center gap-2 text-xs text-slate-500">
            <Loader2 size={13} className="animate-spin" />
            {agent.name} is thinking…
          </div>
        )}
        {!status.chat && (
          <div className="flex items-center gap-2 text-xs text-amber-600">
            <MessageSquare size={13} />
            {agent.name} is not deployed yet — chat is disabled for this agent.
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Composer */}
      <div className="px-5 py-3 border-t border-slate-200 bg-white">
        <div className="flex items-center gap-2">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }}
            placeholder={status.chat ? `Ask ${agent.name}…` : `${agent.name} is unavailable`}
            disabled={!status.chat}
            className="input flex-1 text-sm"
          />
          <button
            onClick={send}
            disabled={loading || !input.trim() || !status.chat}
            className="btn-primary flex items-center gap-1.5 text-sm disabled:opacity-50"
          >
            {loading ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />}
            Send
          </button>
        </div>
      </div>
    </div>
  )
}
