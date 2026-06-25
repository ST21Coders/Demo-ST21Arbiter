import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Cpu, FolderTree, GitBranch, Terminal, Trash2, ArrowRight } from 'lucide-react'
import { resetPreferences } from '../../hooks/usePreferences'

const cardStyle = { background: 'var(--surface)', border: '1px solid rgb(var(--c-slate-200))', boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }

const OPERATOR_LINKS = [
  { to: '/llm-control', icon: Cpu,       label: 'LLM Control',  desc: 'Guardrails, agent registry, model selection' },
  { to: '/pipeline',    icon: GitBranch, label: 'Data Pipeline', desc: 'Document ingestion & knowledge-base sync' },
  { to: '/data-grouping', icon: FolderTree, label: 'Data Grouping', desc: 'Project data objects, metadata, and spreadsheet summaries' },
  { to: '/mcp-chat',    icon: Terminal,  label: 'MCP Admin',     desc: 'Runtime health & MCP server diagnostics' },
]

export default function AdvancedSection() {
  const [confirming, setConfirming] = useState(false)

  function clearLocalData() {
    // Resets preferences only. Deliberately does NOT touch 'arbiter.tokens' —
    // signing out is the Session section's job.
    resetPreferences()
    setConfirming(false)
  }

  return (
    <div className="space-y-5">
      {/* Operator quick links */}
      <div className="rounded-xl p-4" style={cardStyle}>
        <p className="text-[10px] text-slate-500 font-semibold uppercase tracking-wider mb-3">Platform operations</p>
        <div className="space-y-2">
          {OPERATOR_LINKS.map(({ to, icon: Icon, label, desc }) => (
            <Link key={to} to={to}
                  className="flex items-center gap-3 px-3 py-2.5 rounded-lg border border-slate-200 hover:bg-slate-50 transition-colors group">
              <Icon size={15} className="text-slate-500 flex-shrink-0" />
              <div className="flex-1 min-w-0">
                <p className="text-sm text-slate-900 font-medium">{label}</p>
                <p className="text-xs text-slate-500">{desc}</p>
              </div>
              <ArrowRight size={13} className="text-slate-300 group-hover:text-slate-500 transition-colors flex-shrink-0" />
            </Link>
          ))}
        </div>
      </div>

      {/* Clear local data */}
      <div className="rounded-xl p-4" style={cardStyle}>
        <div className="flex items-center justify-between gap-4">
          <div>
            <p className="text-sm text-slate-900 font-medium">Clear local app data</p>
            <p className="text-xs text-slate-500 mt-0.5">Resets your preferences to defaults. Does not sign you out.</p>
          </div>
          {confirming ? (
            <div className="flex items-center gap-2 flex-shrink-0">
              <button onClick={() => setConfirming(false)}
                      className="px-3 py-1.5 rounded-lg text-sm font-medium text-slate-600 bg-slate-100 hover:bg-slate-200 transition-colors">
                Cancel
              </button>
              <button onClick={clearLocalData}
                      className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium text-white bg-red-600 hover:bg-red-700 transition-colors">
                <Trash2 size={13} /> Confirm reset
              </button>
            </div>
          ) : (
            <button onClick={() => setConfirming(true)}
                    className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium text-red-700 bg-red-50 border border-red-200 hover:bg-red-100 transition-colors flex-shrink-0">
              <Trash2 size={13} /> Reset
            </button>
          )}
        </div>
      </div>

    </div>
  )
}
