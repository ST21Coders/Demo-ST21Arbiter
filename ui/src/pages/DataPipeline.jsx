import { useState } from 'react'
import { CheckCircle, Clock, RefreshCw, Database, FileText, Server, Loader2, Activity } from 'lucide-react'

const SOURCES = [
  {
    id: 'sharepoint',
    name: 'SharePoint',
    icon: FileText,
    description: 'Policy documents, standards, procedures',
    s3Prefix: 's3://mig-arbiter-raw/sharepoint/',
    docCount: 12,
    lastSync: '2026-05-18T08:30:00Z',
    status: 'SYNCED',
    formats: ['DOCX', 'PDF'],
    color: { bg: '#eef2ff', icon: '#4f46e5', border: '#c7d2fe' },
  },
  {
    id: 'zscaler',
    name: 'Zscaler ZIA',
    icon: Server,
    description: 'URL categorization rules, policy enforcement',
    s3Prefix: 's3://mig-arbiter-raw/zscaler/',
    docCount: 3,
    lastSync: '2026-05-18T07:45:00Z',
    status: 'SYNCED',
    formats: ['JSON'],
    color: { bg: '#f0f9ff', icon: '#0284c7', border: '#bae6fd' },
  },
  {
    id: 'awsconfig',
    name: 'AWS Config',
    icon: Database,
    description: 'Security groups, S3 bucket configs, IAM snapshots',
    s3Prefix: 's3://mig-arbiter-raw/aws-config/',
    docCount: 8,
    lastSync: '2026-05-18T09:00:00Z',
    status: 'SYNCED',
    formats: ['JSON'],
    color: { bg: '#fff7ed', icon: '#ea580c', border: '#fed7aa' },
  },
]

const PIPELINE_STEPS = [
  { id: 1, label: 'S3 Ingest',          desc: 'Documents land in mig-arbiter-raw via connector or manual upload',             status: 'DONE' },
  { id: 2, label: 'Lambda Processing',   desc: 'DOCX→text chunking, JSON→natural language description, metadata extraction',   status: 'DONE' },
  { id: 3, label: 'Bedrock KB Sync',     desc: 'Chunks embedded with Titan Embed Text v2, indexed in OpenSearch Serverless',   status: 'DONE' },
  { id: 4, label: 'Agent Scan',          desc: 'Master Orchestrator dispatches 4 specialist agents for cross-domain detection', status: 'READY' },
  { id: 5, label: 'Conflict Storage',    desc: 'Conflicts written to mig-arbiter-conflicts DynamoDB table',                    status: 'READY' },
]

const STATUS_STYLE = {
  DONE:    { bg: '#ecfdf5',  border: '#a7f3d0', text: '#047857',  label: 'Done' },
  READY:   { bg: '#eef2ff', border: '#c7d2fe', text: '#4338ca',  label: 'Ready' },
  SYNCING: { bg: '#fffbeb', border: '#fde68a', text: '#b45309',  label: 'Syncing…' },
  ERROR:   { bg: '#fef2f2',  border: '#fecaca', text: '#b91c1c',  label: 'Error' },
}

function SourceCard({ source, onSync, syncing }) {
  const Icon = source.icon
  return (
    <div className="rounded-xl p-4 bg-white"
         style={{ border: `1px solid ${source.color.border}`, boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }}>
      <div className="flex items-start justify-between mb-4">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-lg flex items-center justify-center flex-shrink-0"
               style={{ background: source.color.bg }}>
            <Icon size={16} style={{ color: source.color.icon }} />
          </div>
          <div>
            <p className="font-semibold text-slate-900 text-sm">{source.name}</p>
            <p className="text-xs text-slate-500">{source.description}</p>
          </div>
        </div>
        <button
          onClick={() => onSync(source.id)}
          disabled={syncing === source.id}
          className="btn-ghost text-xs flex items-center gap-1 px-2 py-1"
        >
          {syncing === source.id
            ? <Loader2 size={12} className="animate-spin" />
            : <RefreshCw size={12} />
          }
          Sync
        </button>
      </div>

      <div className="grid grid-cols-2 gap-3 text-xs">
        <div>
          <p className="text-slate-400 mb-0.5">S3 Prefix</p>
          <p className="font-mono text-slate-600 truncate text-[11px]" title={source.s3Prefix}>{source.s3Prefix}</p>
        </div>
        <div>
          <p className="text-slate-400 mb-0.5">Documents</p>
          <p className="text-slate-700">{source.docCount} files ({source.formats.join(', ')})</p>
        </div>
        <div>
          <p className="text-slate-400 mb-0.5">Last Sync</p>
          <p className="text-slate-700">{new Date(source.lastSync).toLocaleTimeString()}</p>
        </div>
        <div>
          <p className="text-slate-400 mb-0.5">Status</p>
          <span className="flex items-center gap-1 text-emerald-700">
            <CheckCircle size={11} /> {source.status}
          </span>
        </div>
      </div>
    </div>
  )
}

export default function DataPipeline() {
  const [syncing, setSyncing] = useState(null)

  async function handleSync(id) {
    setSyncing(id)
    await new Promise(r => setTimeout(r, 1500))
    setSyncing(null)
  }

  const cardStyle = { background: '#ffffff', border: '1px solid #e2e8f0', boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }

  return (
    <div className="p-6 space-y-6 max-w-5xl">
      <div>
        <h1 className="text-lg font-bold text-slate-900 tracking-tight">Data Pipeline</h1>
        <p className="text-xs text-slate-500 mt-0.5">S3-backed ingestion pipeline feeding Bedrock Knowledge Base</p>
      </div>

      {/* Pipeline flow */}
      <div className="rounded-xl p-4" style={cardStyle}>
        <div className="flex items-center gap-2 mb-4">
          <Activity size={12} className="text-slate-500" />
          <p className="text-[10px] text-slate-500 font-semibold uppercase tracking-wider">Ingestion Pipeline</p>
        </div>
        <div className="space-y-0">
          {PIPELINE_STEPS.map((step, i) => {
            const s = STATUS_STYLE[step.status]
            return (
              <div key={step.id} className="flex items-start gap-4">
                <div className="flex flex-col items-center flex-shrink-0">
                  <div className="w-7 h-7 rounded-full border flex items-center justify-center text-xs font-bold"
                       style={{ background: s.bg, border: `1px solid ${s.border}`, color: s.text }}>
                    {step.status === 'DONE' ? <CheckCircle size={13} /> : <Clock size={13} />}
                  </div>
                  {i < PIPELINE_STEPS.length - 1 && (
                    <div className="w-px flex-1 my-1 bg-slate-200" style={{ minHeight: '24px' }} />
                  )}
                </div>
                <div className={`pb-4 ${i === PIPELINE_STEPS.length - 1 ? '' : ''}`}>
                  <div className="flex items-center gap-2 pt-1">
                    <p className="text-sm font-medium text-slate-900">{step.label}</p>
                    <span className="text-xs px-2 py-0.5 rounded font-medium"
                          style={{ background: s.bg, color: s.text, border: `1px solid ${s.border}` }}>
                      {s.label}
                    </span>
                  </div>
                  <p className="text-xs text-slate-500 mt-0.5">{step.desc}</p>
                </div>
              </div>
            )
          })}
        </div>
      </div>

      {/* Source connectors */}
      <div>
        <p className="text-[10px] text-slate-500 font-semibold uppercase tracking-wider mb-3">Data Sources (S3-backed)</p>
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          {SOURCES.map(src => (
            <SourceCard key={src.id} source={src} onSync={handleSync} syncing={syncing} />
          ))}
        </div>
      </div>

      {/* KB info */}
      <div className="rounded-xl p-4" style={cardStyle}>
        <p className="text-[10px] text-slate-500 font-semibold uppercase tracking-wider mb-3">Bedrock Knowledge Base</p>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 text-xs">
          {[
            { label: 'KB ID',            value: import.meta.env.VITE_KB_ID || 'mig-arbiter-policy-kb', mono: true },
            { label: 'Embedding Model',  value: 'Titan Embed Text v2',                                  mono: false },
            { label: 'Chunk Size',       value: '1,000 tokens (20% overlap)',                           mono: false },
            { label: 'Vector Store',     value: 'OpenSearch Serverless',                                mono: false },
          ].map(item => (
            <div key={item.label}
                 className="rounded-lg p-3 bg-slate-50 border border-slate-200">
              <p className="text-slate-400 mb-1">{item.label}</p>
              <p className={`text-slate-700 ${item.mono ? 'font-mono text-[11px]' : ''}`}>{item.value}</p>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
