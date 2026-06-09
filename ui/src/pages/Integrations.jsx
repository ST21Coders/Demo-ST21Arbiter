import {
  Plug, CheckCircle2, Clock, Shield, Search,
} from 'lucide-react'
import { useState } from 'react'
import { useAgentStatus } from '../hooks/useApi'

/* ─── Connector catalog ──────────────────────────────────────────────
   Read-only marketplace façade. "Live" connectors map to a real ARBITER
   AgentCore runtime — their status is pulled from GET /agent-status via
   useAgentStatus() (keyed by agentId). The rest are roadmap connectors
   shown as "Coming soon"; this page does not configure anything. */
const CATALOG = [
  {
    category: 'Policy & Security Sources',
    blurb: 'Sources ARBITER scans for cross-domain policy conflicts.',
    items: [
      { id: 'sharepoint', name: 'SharePoint',      vendor: 'Microsoft',           desc: 'Enterprise policy documents (Graph API → Knowledge Base).',  agentId: 'sharepoint', live: true },
      { id: 'zscaler',    name: 'Zscaler ZIA',      vendor: 'Zscaler',             desc: 'URL allowlists, DLP, and category enforcement policy.',      agentId: 'zscaler',    live: true },
      { id: 'paloalto',   name: 'Palo Alto NGFW',   vendor: 'Palo Alto Networks',  desc: 'Perimeter firewall rules, App-ID, and egress controls.',     agentId: 'paloalto',   live: true },
      { id: 'awsconfig',  name: 'AWS Config',       vendor: 'Amazon Web Services', desc: 'Infrastructure compliance rules and live resource state.',   agentId: 'awsconfig',  live: true },
    ],
  },
  {
    category: 'ITSM & Ticketing',
    blurb: 'Where ARBITER raises and resolves change/incident work.',
    items: [
      { id: 'jira',       name: 'Jira',             vendor: 'Atlassian',           desc: 'Issue creation and L1 ticket resolution via the Atlassian MCP server.', agentId: 'jira', live: true },
      { id: 'servicenow', name: 'ServiceNow',       vendor: 'ServiceNow',          desc: 'INC / CHG / RITM ticketing. Specialist agent on the roadmap.',          comingSoon: true },
    ],
  },
  {
    category: 'Observability & SIEM',
    blurb: 'Forward findings and audit events to monitoring platforms.',
    items: [
      { id: 'splunk',     name: 'Splunk',           vendor: 'Splunk',              desc: 'Log analytics and SIEM correlation of audit events.', comingSoon: true },
      { id: 'datadog',    name: 'Datadog',          vendor: 'Datadog',             desc: 'Metrics, traces, and monitoring of the scan pipeline.', comingSoon: true },
    ],
  },
  {
    category: 'Databases & Structured Data',
    blurb: 'Structured policy stores ingested via Glue / Athena.',
    items: [
      { id: 'oracle',     name: 'Oracle Database',  vendor: 'Oracle',              desc: 'Structured policy records ingested via AWS Glue + Athena.', comingSoon: true },
    ],
  },
]

/* Live runtime status → a UI bucket for the card badge. */
function bucketFor(item, statusById) {
  if (item.comingSoon) return { label: 'Coming soon', cls: 'bg-slate-100 text-slate-500 border-slate-200', dot: 'bg-slate-300', icon: Clock }
  const raw = item.live ? statusById[item.agentId] : undefined
  if (!raw)              return { label: 'Checking…',  cls: 'bg-slate-50 text-slate-500 border-slate-200',  dot: 'bg-slate-400', icon: Clock }
  if (raw === 'READY')   return { label: 'Connected',  cls: 'bg-emerald-50 text-emerald-700 border-emerald-200', dot: 'bg-emerald-500', icon: CheckCircle2 }
  if (raw === 'PLACEHOLDER') return { label: 'Not deployed', cls: 'bg-red-50 text-red-600 border-red-200', dot: 'bg-red-500', icon: Clock }
  return { label: raw, cls: 'bg-amber-50 text-amber-700 border-amber-200', dot: 'bg-amber-500', icon: Clock } // CREATING / UPDATING / …
}

function ConnectorCard({ item, statusById }) {
  const b = bucketFor(item, statusById)
  const Icon = b.icon
  return (
    <div className="border border-slate-200 rounded-xl bg-white p-4 flex flex-col gap-3 hover:border-slate-300 transition-colors">
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <span className="w-9 h-9 rounded-lg bg-slate-100 flex items-center justify-center flex-shrink-0">
            <Plug size={16} className="text-slate-500" />
          </span>
          <div className="min-w-0">
            <p className="text-sm font-semibold text-slate-800 truncate">{item.name}</p>
            <p className="text-[11px] text-slate-500 truncate">{item.vendor}</p>
          </div>
        </div>
        <span className={`inline-flex items-center gap-1 text-[10px] font-medium px-2 py-0.5 rounded-full border whitespace-nowrap ${b.cls}`}>
          <span className={`w-1.5 h-1.5 rounded-full ${b.dot}`} /> {b.label}
        </span>
      </div>
      <p className="text-xs text-slate-600 leading-relaxed flex-1">{item.desc}</p>
      <button
        disabled
        title="Connector configuration is managed by the platform team"
        className="text-[11px] font-medium text-slate-400 border border-slate-200 rounded-md px-3 py-1.5 cursor-not-allowed self-start"
      >
        <Icon size={11} className="inline mr-1 -mt-0.5" />
        {item.comingSoon ? 'Notify me' : 'Manage'}
      </button>
    </div>
  )
}

export default function Integrations() {
  const statusById = useAgentStatus()
  const [q, setQ] = useState('')

  const term = q.trim().toLowerCase()
  const groups = CATALOG
    .map(g => ({
      ...g,
      items: g.items.filter(i =>
        !term || i.name.toLowerCase().includes(term) || i.vendor.toLowerCase().includes(term) || i.desc.toLowerCase().includes(term)
      ),
    }))
    .filter(g => g.items.length > 0)

  const allItems = CATALOG.flatMap(g => g.items)
  const connected = allItems.filter(i => i.live && statusById[i.agentId] === 'READY').length
  const roadmap = allItems.filter(i => i.comingSoon).length

  return (
    <div className="p-6 space-y-6 max-w-6xl mx-auto">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-xl font-bold text-slate-800 flex items-center gap-2">
            <Plug size={20} className="text-indigo-600" /> Integrations Marketplace
          </h1>
          <p className="text-sm text-slate-500 mt-1">
            Connectors that feed ARBITER's policy-conflict engine. {connected} connected · {roadmap} on the roadmap.
          </p>
        </div>
        <div className="relative">
          <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400" />
          <input
            value={q}
            onChange={e => setQ(e.target.value)}
            placeholder="Search connectors…"
            className="input pl-8 w-56 text-sm"
          />
        </div>
      </div>

      {groups.map(g => (
        <section key={g.category} className="space-y-3">
          <div>
            <h2 className="text-sm font-bold text-slate-700 uppercase tracking-wide">{g.category}</h2>
            <p className="text-xs text-slate-500">{g.blurb}</p>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {g.items.map(item => (
              <ConnectorCard key={item.id} item={item} statusById={statusById} />
            ))}
          </div>
        </section>
      ))}

      {groups.length === 0 && (
        <p className="text-sm text-slate-400 italic py-8 text-center">No connectors match “{q}”.</p>
      )}

      <div className="flex items-center gap-1.5 text-[11px] text-slate-400 border-t border-slate-100 pt-4">
        <Shield size={11} /> Read-only catalog. Live source status is polled from the AgentCore control plane.
      </div>
    </div>
  )
}
