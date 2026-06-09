import { useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import {
  Loader2, ChevronDown, ChevronRight, CheckCircle, XCircle,
  Play, AlertOctagon, Clock, Shield, Zap, Plus, ExternalLink, Ticket, X
} from 'lucide-react'
import { useChangeRequests, createJiraTicket } from '../hooks/useApi'
import { SeverityBadge, StatusBadge } from '../components/SeverityBadge'
import ActionRequestModal from '../components/ActionRequestModal'
import { formatDistanceToNow } from 'date-fns'
import { getEmail } from '../hooks/useAuth'
import { TEAM_LABELS } from '../mockData'

const STAT_COLORS = {
  pending:   { border: '#f59e0b', glow: 'rgba(245,158,11,0.08)', icon: 'rgba(245,158,11,0.10)', text: '#b45309' },
  approved:  { border: '#10b981', glow: 'rgba(16,185,129,0.08)', icon: 'rgba(16,185,129,0.10)', text: '#047857' },
  completed: { border: '#6366f1', glow: 'rgba(99,102,241,0.08)', icon: 'rgba(99,102,241,0.10)', text: '#4338ca' },
  escalated: { border: '#ef4444', glow: 'rgba(239,68,68,0.08)',  icon: 'rgba(239,68,68,0.10)',  text: '#b91c1c' },
}

function ApproverRow({ approver }) {
  const icon = {
    APPROVED: <CheckCircle size={13} className="text-emerald-600" />,
    REJECTED:  <XCircle size={13} className="text-red-600" />,
    PENDING:   <Clock size={13} className="text-amber-600" />,
    NOTIFIED:  <Shield size={13} className="text-indigo-600" />,
  }[approver.status] || <Clock size={13} className="text-slate-400" />

  return (
    <div className="flex items-center gap-2 py-1.5 border-b border-slate-100 last:border-0">
      {icon}
      <div className="flex-1 min-w-0">
        <p className="text-xs text-slate-700">{approver.email}</p>
        {approver.description && <p className="text-xs text-slate-500">{approver.description}</p>}
      </div>
      <span className={`text-xs font-medium ${
        approver.status === 'APPROVED' ? 'text-emerald-700' :
        approver.status === 'REJECTED' ? 'text-red-700' :
        approver.status === 'NOTIFIED' ? 'text-indigo-700' :
        'text-amber-700'
      }`}>{approver.status}</span>
      {approver.type === 'NOTIFICATION' && (
        <span className="text-xs text-slate-500 px-1.5 py-0.5 rounded bg-slate-50 border border-slate-200">
          notify-only
        </span>
      )}
    </div>
  )
}

function CRCard({ cr, onApprove, onReject, onExecute, onEscalate }) {
  const [expanded, setExpanded] = useState(false)
  const [acting, setActing] = useState(false)
  const [comment, setComment] = useState('')
  const [jiraKey, setJiraKey] = useState(cr.jira_ticket_key || null)
  const [jiraUrl, setJiraUrl] = useState(cr.jira_ticket_url || null)
  const [jiraBusy, setJiraBusy] = useState(false)
  const [jiraFormOpen, setJiraFormOpen] = useState(false)
  // Editable JIRA fields. Title seeds from the conflict/finding; description
  // from the business justification — both editable before creating.
  const [jiraTitle, setJiraTitle] = useState(
    `${cr.severity || 'HIGH'}: ${cr.description || cr.request || cr.target_resource || cr.conflict_id || cr.cr_id}`
  )
  const [jiraDesc, setJiraDesc] = useState(cr.justification || cr.description || '')
  const [jiraErr, setJiraErr] = useState(null)

  async function createJira() {
    setJiraBusy(true)
    setJiraErr(null)
    try {
      const res = await createJiraTicket({
        conflict_id: cr.conflict_id || cr.linked_conflict_id,
        summary: jiraTitle,
        description: jiraDesc,
        project_key: 'DEVARBITER',
        severity: cr.severity,
      })
      const key = res?.jira_ticket_key || res?.mock_ticket_key
      if (!key) throw new Error('No ticket key returned')
      setJiraKey(key)
      setJiraUrl(res?.url || null)
      setJiraFormOpen(false)
    } catch (err) {
      setJiraErr(err.message || String(err))
    } finally { setJiraBusy(false) }
  }

  const pendingApprovers = cr.approvers?.filter(a => a.type !== 'NOTIFICATION' && a.status === 'PENDING') || []
  // DEV CRs come back as AUTO_APPROVED (no human approvers required) — they
  // should be executable without going through the approval queue.
  const canExecute = cr.status === 'APPROVED' || cr.status === 'AUTO_APPROVED'
  const canApprove = cr.status === 'PENDING_APPROVAL' && pendingApprovers.length > 0
  // Pull the signed-in user's email from the IdToken so any CISO-group user
  // (regardless of their address) is correctly identified as an approver.
  const myEmail = getEmail()

  async function act(fn, ...args) {
    setActing(true)
    try { await fn(...args) } finally { setActing(false) }
  }

  const envColors = {
    PROD:    { bg: '#fef2f2', text: '#b91c1c',  border: '#fecaca' },
    DEV:     { bg: '#ecfdf5', text: '#047857',  border: '#a7f3d0' },
    STAGING: { bg: '#fffbeb', text: '#b45309',  border: '#fde68a' },
  }
  const env = envColors[cr.target_environment] || envColors.STAGING

  return (
    <div className="rounded-xl overflow-hidden bg-white border border-slate-200"
         style={{ boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }}>
      <div className="flex items-start gap-3 px-4 py-3.5 cursor-pointer hover:bg-slate-50 transition-colors"
           onClick={() => setExpanded(p => !p)}>
        {expanded
          ? <ChevronDown size={15} className="text-slate-500 mt-0.5" />
          : <ChevronRight size={15} className="text-slate-500 mt-0.5" />
        }
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <SeverityBadge severity={cr.severity} />
            <span className="text-sm font-medium text-slate-900">{cr.action_type?.replace(/_/g, ' ')}</span>
            <span className="text-xs text-slate-400 font-mono">{cr.cr_id}</span>
          </div>
          <p className="text-xs text-slate-600 mt-1 truncate">{cr.target_resource || cr.request}</p>
          <p className="text-xs text-slate-500 mt-0.5">
            by {cr.requested_by} · {formatDistanceToNow(new Date(cr.created_at), { addSuffix: true })}
          </p>
          {cr.routed_team && (
            <p className="text-[11px] text-slate-500 mt-0.5">
              routed to <span className="text-slate-700 font-medium">{TEAM_LABELS[cr.routed_team] || cr.routed_team}</span>
            </p>
          )}
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          <span className="text-xs px-2 py-0.5 rounded font-mono font-medium"
                style={{ background: env.bg, color: env.text, border: `1px solid ${env.border}` }}>
            {cr.target_environment}
          </span>
          <StatusBadge status={cr.status} />
        </div>
      </div>

      {expanded && (
        <div className="px-4 pb-4 space-y-4 border-t border-slate-100">

          {/* Team ownership & routing (denormalized from the linked finding) */}
          {(cr.owner_team || cr.consumer_team || cr.platform_team || cr.tags?.length) && (
            <div className="mt-4">
              <p className="text-[10px] text-slate-400 font-semibold uppercase tracking-wider mb-2">Ownership & Routing</p>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                {[['Owner', cr.owner_team], ['Consumer', cr.consumer_team], ['Platform', cr.platform_team]].map(([lbl, team]) => team ? (
                  <div key={lbl}>
                    <p className="text-[10px] text-slate-400 uppercase tracking-wider mb-1">{lbl} Team</p>
                    <span className="text-xs px-2 py-0.5 rounded-md bg-slate-100 text-slate-700 border border-slate-200">
                      {TEAM_LABELS[team] || team}
                    </span>
                  </div>
                ) : null)}
              </div>
              {cr.tags?.length > 0 && (
                <div className="flex gap-1.5 flex-wrap mt-2">
                  {cr.tags.map(t => (
                    <span key={t} className="text-xs px-2 py-0.5 rounded-full bg-violet-50 text-violet-700 border border-violet-200">{t}</span>
                  ))}
                </div>
              )}
              {cr.jira_project_key && (
                <p className="text-[11px] text-slate-500 mt-2">JIRA project: <span className="font-mono text-slate-700">{cr.jira_project_key}</span>{cr.jira_component ? ` · ${cr.jira_component}` : ''}</p>
              )}
            </div>
          )}

          {/* Approval progress */}
          <div className="mt-4">
            <p className="text-[10px] text-slate-400 font-semibold uppercase tracking-wider mb-2">
              Approval Chain
              {cr.total_approvers_needed > 0 && (
                <span className="ml-2 normal-case text-slate-400 font-normal">
                  {cr.total_approvals_received}/{cr.total_approvers_needed} approvals received
                </span>
              )}
            </p>
            {cr.approvers?.length > 0 ? (
              <div className="rounded-lg px-3 py-1 bg-slate-50 border border-slate-200">
                {cr.approvers.map((a, i) => <ApproverRow key={i} approver={a} />)}
              </div>
            ) : (
              <p className="text-xs text-emerald-700 flex items-center gap-1.5">
                <CheckCircle size={12} /> Auto-approved (DEV environment)
              </p>
            )}
          </div>

          {cr.justification && (
            <div>
              <p className="text-[10px] text-slate-400 font-semibold uppercase tracking-wider mb-1.5">Business Justification</p>
              <p className="text-sm text-slate-700 rounded-lg p-3 bg-slate-50 border border-slate-200">
                {cr.justification}
              </p>
            </div>
          )}

          {cr.execution_log?.length > 0 && (
            <div>
              <p className="text-[10px] text-slate-400 font-semibold uppercase tracking-wider mb-1.5">Execution Log</p>
              <div className="rounded-lg p-3 font-mono text-xs text-emerald-800 space-y-0.5 max-h-40 overflow-y-auto bg-emerald-50 border border-emerald-200">
                {cr.execution_log.map((line, i) => <p key={i}>{line}</p>)}
              </div>
            </div>
          )}

          <div className="flex flex-wrap gap-2 pt-1">
            {canApprove && (
              <>
                <div className="flex gap-2 flex-1 min-w-0">
                  <input
                    value={comment}
                    onChange={e => setComment(e.target.value)}
                    placeholder="Approval comment (optional)"
                    className="input flex-1 text-xs"
                  />
                </div>
                <button
                  onClick={() => act(onApprove, cr.cr_id, myEmail, 'ciso', comment)}
                  disabled={acting}
                  className="btn-primary flex items-center gap-1.5 text-xs"
                >
                  {acting ? <Loader2 size={12} className="animate-spin" /> : <CheckCircle size={12} />}
                  Approve
                </button>
                <button
                  onClick={() => act(onReject, cr.cr_id, myEmail, comment || 'Rejected')}
                  disabled={acting}
                  className="btn-danger flex items-center gap-1.5 text-xs"
                >
                  <XCircle size={12} /> Reject
                </button>
              </>
            )}

            {canExecute && (
              <button
                onClick={() => act(onExecute, cr.cr_id)}
                disabled={acting}
                className="flex items-center gap-1.5 text-xs px-3 py-2 rounded-lg font-semibold transition-all text-white bg-emerald-600 hover:bg-emerald-700"
              >
                {acting ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />}
                Execute Remediation
              </button>
            )}

            {['PENDING_APPROVAL', 'APPROVED'].includes(cr.status) && (
              <button
                onClick={() => act(onEscalate, cr.cr_id, 'Manual escalation')}
                disabled={acting}
                className="btn-ghost flex items-center gap-1.5 text-xs text-indigo-600 hover:text-indigo-700"
              >
                <AlertOctagon size={12} /> Escalate
              </button>
            )}
            {jiraKey ? (
              jiraUrl ? (
                <a
                  href={jiraUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="flex items-center gap-1.5 text-xs text-indigo-700 bg-indigo-50 border border-indigo-200 px-2 py-1 rounded-md hover:bg-indigo-100 transition-colors"
                  title="Open the JIRA ticket in a new tab"
                >
                  JIRA: <span className="font-mono">{jiraKey}</span>
                  <ExternalLink size={11} />
                </a>
              ) : (
                <span className="flex items-center gap-1.5 text-xs text-indigo-700 bg-indigo-50 border border-indigo-200 px-2 py-1 rounded-md">
                  JIRA: <span className="font-mono">{jiraKey}</span>
                </span>
              )
            ) : (
              <button
                onClick={() => { setJiraFormOpen(o => !o); setJiraErr(null) }}
                disabled={jiraBusy}
                className="btn-ghost flex items-center gap-1.5 text-xs text-indigo-600 hover:text-indigo-700"
                title="Create a JIRA Task in the DEVARBITER project via the JIRA specialist agent."
              >
                <Ticket size={12} /> Open JIRA ticket
              </button>
            )}
          </div>

          {/* Editable JIRA create form — Task in project DEVARBITER. */}
          {jiraFormOpen && !jiraKey && (
            <div className="mt-3 rounded-lg border border-indigo-200 bg-indigo-50/40 p-3 space-y-2.5">
              <div className="flex items-center justify-between">
                <p className="text-[10px] font-semibold uppercase tracking-wider text-indigo-700">
                  New JIRA Task · project <span className="font-mono">DEVARBITER</span>
                </p>
                <button onClick={() => setJiraFormOpen(false)} className="text-slate-400 hover:text-slate-600" title="Cancel">
                  <X size={13} />
                </button>
              </div>
              <div>
                <label className="block text-[10px] text-slate-500 font-medium mb-1">Title</label>
                <input
                  value={jiraTitle}
                  onChange={e => setJiraTitle(e.target.value)}
                  className="input w-full text-xs"
                  placeholder="Ticket summary"
                />
              </div>
              <div>
                <label className="block text-[10px] text-slate-500 font-medium mb-1">Description</label>
                <textarea
                  value={jiraDesc}
                  onChange={e => setJiraDesc(e.target.value)}
                  rows={4}
                  className="input w-full text-xs"
                  placeholder="Business justification / details"
                />
              </div>
              {jiraErr && <p className="text-[11px] text-red-600">JIRA error: {jiraErr}</p>}
              <div className="flex items-center gap-2">
                <button
                  onClick={createJira}
                  disabled={jiraBusy || !jiraTitle.trim()}
                  className="btn-primary flex items-center gap-1.5 text-xs px-3 py-1.5"
                >
                  {jiraBusy ? <Loader2 size={12} className="animate-spin" /> : <Zap size={12} />}
                  {jiraBusy ? 'Creating…' : 'Create ticket'}
                </button>
                <button onClick={() => setJiraFormOpen(false)} disabled={jiraBusy} className="btn-ghost text-xs">
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function ActionCenter() {
  const { changeRequests, loading, load, createAction, approve, reject, execute, escalate } = useChangeRequests()
  const [filterStatus, setFilterStatus] = useState('')
  const [showModal, setShowModal] = useState(false)
  const [prefill, setPrefill] = useState(null)
  const location = useLocation()
  const navigate = useNavigate()

  useEffect(() => { load() }, [load])

  // When the page is reached via the Create Ticket button in a chat surface,
  // React Router passes prefill data on location.state. Open the modal with
  // it, then clear the router state so a manual refresh doesn't re-open the
  // modal with stale data.
  useEffect(() => {
    const incoming = location.state?.prefill
    if (!incoming) return
    setPrefill(incoming)
    setShowModal(true)
    navigate(location.pathname, { replace: true, state: null })
  }, [location.state, location.pathname, navigate])

  const filtered = changeRequests.filter(cr => !filterStatus || cr.status === filterStatus)

  const stats = {
    pending:  changeRequests.filter(cr => cr.status === 'PENDING_APPROVAL').length,
    approved: changeRequests.filter(cr => cr.status === 'APPROVED').length,
    completed: changeRequests.filter(cr => cr.status === 'COMPLETED').length,
    escalated: changeRequests.filter(cr => cr.status === 'ESCALATED').length,
  }

  const statItems = [
    { key: 'pending',   label: 'Pending Approval',  icon: Clock },
    { key: 'approved',  label: 'Approved',           icon: CheckCircle },
    { key: 'completed', label: 'Completed',          icon: Play },
    { key: 'escalated', label: 'Escalated',          icon: AlertOctagon },
  ]

  return (
    <div className="p-6 space-y-5 page-container">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-slate-900 tracking-tight">Action Center</h1>
          <p className="text-xs text-slate-500 mt-0.5">Enterprise change request workflow — environment-tiered approvals</p>
        </div>
        <button onClick={() => setShowModal(true)} className="btn-primary flex items-center gap-1.5 text-xs">
          <Plus size={13} /> New Action Request
        </button>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        {statItems.map(({ key, label, icon: Icon }) => {
          const s = STAT_COLORS[key]
          return (
            <div key={key} className="rounded-xl p-4 flex items-center gap-4 bg-white"
                 style={{
                   border: `1px solid ${s.border}25`,
                   borderLeft: `3px solid ${s.border}`,
                   boxShadow: `0 1px 3px rgba(15,23,42,0.06), 0 1px 2px rgba(15,23,42,0.04)`,
                 }}>
              <div className="w-9 h-9 rounded-lg flex items-center justify-center flex-shrink-0"
                   style={{ background: s.icon }}>
                <Icon size={16} style={{ color: s.text }} />
              </div>
              <div>
                <p className="text-2xl font-bold text-slate-900 tabular-nums">{stats[key]}</p>
                <p className="text-xs text-slate-500 mt-0.5">{label}</p>
              </div>
            </div>
          )
        })}
      </div>

      {/* Approval matrix */}
      <div className="rounded-xl p-4 bg-white border border-slate-200"
           style={{ boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }}>
        <div className="flex items-center gap-2 mb-3">
          <Shield size={12} className="text-slate-500" />
          <p className="text-xs font-semibold text-slate-600 uppercase tracking-wider">Approval Matrix</p>
        </div>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-2 text-xs">
          {[
            { env: 'DEV',          desc: 'Auto-approved',                   color: { bg: '#ecfdf5', border: '#a7f3d0', text: '#047857' } },
            { env: 'STAGING',      desc: 'Team Lead',                       color: { bg: '#eef2ff', border: '#c7d2fe', text: '#4338ca' } },
            { env: 'PRE-PROD',     desc: 'Manager + Owning Team Lead',      color: { bg: '#fffbeb', border: '#fde68a', text: '#b45309' } },
            { env: 'PROD CRITICAL',desc: 'CISO + VPE + Legal notified',     color: { bg: '#fef2f2', border: '#fecaca', text: '#b91c1c' } },
          ].map(({ env, desc, color }) => (
            <div key={env} className="rounded-lg p-2.5"
                 style={{ background: color.bg, border: `1px solid ${color.border}` }}>
              <p className="font-semibold mb-1" style={{ color: color.text }}>{env}</p>
              <p className="text-slate-600">{desc}</p>
            </div>
          ))}
        </div>
        <p className="text-xs text-slate-400 mt-3">
          Cross-team rule: if policy is owned by a different team, that team's lead is always added regardless of environment.
        </p>
      </div>

      {/* Filter */}
      <div className="flex gap-3">
        <select value={filterStatus} onChange={e => setFilterStatus(e.target.value)} className="input w-48 text-xs">
          <option value="">All Statuses</option>
          {['PENDING_APPROVAL','APPROVED','REJECTED','EXECUTING','COMPLETED','ESCALATED','AUTO_APPROVED'].map(s => (
            <option key={s} value={s}>{s.replace(/_/g, ' ')}</option>
          ))}
        </select>
        {filterStatus && (
          <button onClick={() => setFilterStatus('')} className="btn-ghost text-xs px-3">Clear</button>
        )}
      </div>

      {/* CR List */}
      {loading ? (
        <div className="flex items-center justify-center py-16">
          <Loader2 size={24} className="animate-spin text-slate-400" />
        </div>
      ) : filtered.length === 0 ? (
        <div className="rounded-xl p-10 text-center bg-white border border-slate-200">
          <Zap size={28} className="text-slate-300 mx-auto mb-3" />
          <p className="text-slate-500 text-sm">No change requests yet.</p>
          <p className="text-slate-400 text-xs mt-1">Create one from a finding or click "New Action Request" above.</p>
        </div>
      ) : (
        <div className="space-y-3">
          {filtered.map(cr => (
            <CRCard
              key={cr.cr_id}
              cr={cr}
              onApprove={approve}
              onReject={reject}
              onExecute={execute}
              onEscalate={escalate}
            />
          ))}
        </div>
      )}

      {showModal && (
        <ActionRequestModal
          conflict={null}
          prefill={prefill}
          onClose={result => {
            setShowModal(false)
            setPrefill(null)
            if (result) load()
          }}
          onCreate={createAction}
        />
      )}
    </div>
  )
}
