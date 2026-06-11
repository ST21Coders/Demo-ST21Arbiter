import { useEffect, useState, useRef } from 'react'
import {
  Loader2, AlertTriangle, RefreshCw,
  BarChart3, Network, HeartPulse,
} from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useFindings, useScanFeed } from '../hooks/useApi'
import {
  buildConflictMatrix, buildDomainSourceMatrix,
  DOMAIN_LABELS, DOMAIN_KEYS, SOURCE_PAIRS, TEAMS, TEAM_LABELS,
} from '../mockData'
import { AGENT_MODELS, modelLabel } from '../config'

// Foundation model name, mirrored from params/dev.json (default Nova 2 Lite).
const MODEL_NAME = modelLabel(AGENT_MODELS.master)

// ─── Architecture topology definitions ────────────────────────────────────────

const DATA_SOURCES = [
  { id: 'sharepoint', label: 'SharePoint',  sub: 'Policy docs',    color: '#64748b' },
  { id: 'zscaler',    label: 'Zscaler ZIA', sub: 'URL / DLP',      color: '#64748b' },
  { id: 'paloalto',   label: 'Palo Alto',   sub: 'NGFW perimeter', color: '#64748b' },
  { id: 'aws',        label: 'AWS Config',  sub: 'Infrastructure', color: '#64748b' },
  { id: 'servicenow', label: 'ServiceNow',  sub: 'ITSM',           color: '#64748b' },
]

const INGESTION = [
  { id: 'ing-sp',  sourceId: 'sharepoint', label: 'SharePoint',  sub: 'Graph API poll · S3',  color: '#d97706', status: 'ONLINE', latencyMs: 320, lastSync: '2 min ago' },
  { id: 'ing-zs',  sourceId: 'zscaler',    label: 'Zscaler ZIA', sub: 'ZIA REST poll · S3',   color: '#d97706', status: 'ONLINE', latencyMs: 410, lastSync: '4 min ago' },
  { id: 'ing-pa',  sourceId: 'paloalto',   label: 'Palo Alto',   sub: 'PAN-OS XML API · S3',  color: '#d97706', status: 'ONLINE', latencyMs: 380, lastSync: '5 min ago' },
  { id: 'ing-aws', sourceId: 'aws',        label: 'AWS Config',  sub: 'EventBridge · S3',     color: '#d97706', status: 'ONLINE', latencyMs: 95,  lastSync: 'streaming' },
  { id: 'ing-sn',  sourceId: 'servicenow', label: 'ServiceNow',  sub: 'ITSM API poll · S3',   color: '#d97706', status: 'DEGRADED', latencyMs: 1240, lastSync: '12 min ago', error: 'API gateway latency 1240ms (SLA 300ms)' },
]

const PROCESSING = {
  id: 'processing',
  label: 'Processing pipeline',
  sub: 'Textract · NN classify · chunk · Titan Embed V2 → KB sync',
  color: '#d97706',
  status: 'ONLINE',
}

const STORAGE = [
  { id: 's3-raw',     label: 'S3 raw zone',     sub: 'Raw landing zone',     color: '#059669', status: 'ONLINE' },
  { id: 'kb',         label: 'Knowledge Bases', sub: 'RAG · policy vectors', color: '#059669', status: 'ONLINE' },
  { id: 'aurora',     label: 'Aurora PG',       sub: 'Policy records',       color: '#059669', status: 'ONLINE' },
  { id: 'dynamo-doc', label: 'DynamoDB',        sub: 'Doc metadata',         color: '#059669', status: 'ONLINE' },
]

const AGENTCORE = {
  gateway: {
    id: 'agentcore-gateway',
    label: 'AgentCore Gateway',
    sub: 'Tool registry',
    color: '#4f46e5',
    status: 'ONLINE',
    tools: [
      { id: 'tool-sp',     label: 'SharePoint tool' },
      { id: 'tool-zs',     label: 'Zscaler tool' },
      { id: 'tool-pa',     label: 'Palo Alto tool' },
      { id: 'tool-config', label: 'Config tool' },
      { id: 'tool-iam',    label: 'IAM tool' },
    ],
  },
  runtime: {
    id: 'agentcore-runtime',
    label: 'AgentCore Runtime',
    sub: 'Master Orchestrator',
    color: '#2563eb',
    status: 'ACTIVE',
  },
  memory: {
    id: 'agentcore-memory',
    label: 'AgentCore Memory',
    sub: 'Findings · session state',
    color: '#7c3aed',
    status: 'ONLINE',
  },
  bedrockAgents: {
    id: 'bedrock-agents',
    label: 'Bedrock Agents',
    color: '#6d28d9',
    status: 'ACTIVE',
    lines: [
      `${MODEL_NAME} × 5 — DOC · NET · ZSC · PAN · IAM specialists`,
      `${MODEL_NAME} × 2 — Conflict Reasoner · Remediation`,
    ],
    agents: [
      { id: 'doc-specialist',  name: 'DOC specialist',   model: MODEL_NAME,  role: 'SharePoint document analysis' },
      { id: 'net-specialist',  name: 'NET specialist',   model: MODEL_NAME,  role: 'Network / VPC analysis' },
      { id: 'zsc-specialist',  name: 'ZSC specialist',   model: MODEL_NAME,  role: 'Zscaler URL category analysis' },
      { id: 'pan-specialist',  name: 'PAN specialist',   model: MODEL_NAME,  role: 'Palo Alto NGFW / egress analysis' },
      { id: 'iam-specialist',  name: 'IAM specialist',   model: MODEL_NAME,  role: 'IAM / S3 policy analysis' },
      { id: 'conflict-reasoner', name: 'Conflict Reasoner', model: MODEL_NAME, role: 'Cross-domain conflict determination' },
      { id: 'remediation',      name: 'Remediation Planner', model: MODEL_NAME, role: 'Ordered remediation plans' },
    ],
  },
}

const OUTPUTS = [
  { id: 'dynamo-conflicts', label: 'DynamoDB',       sub: 'Conflicts store',    color: '#059669', status: 'ONLINE' },
  { id: 'action-center',    label: 'Action Center',  sub: 'Approval flow',      color: '#059669', status: 'ONLINE' },
  { id: 'react-ui',         label: 'API · React UI', sub: 'ARBITER dashboard',  color: '#059669', status: 'ONLINE' },
  { id: 'audit',            label: 'Audit Trail',    sub: 'SIEM · Splunk',      color: '#059669', status: 'ONLINE' },
]

// ─── Canvas layout constants ──────────────────────────────────────────────────

const CW = 1240
const CANVAS_H = 920

// Tier widths and centering
function tierPositions(count, nodeW, gap, canvasW = CW) {
  const total = count * nodeW + (count - 1) * gap
  const start = (canvasW - total) / 2
  return Array.from({ length: count }, (_, i) => start + i * (nodeW + gap))
}

const DS_Y = 30,  DS_W = 180, DS_H = 56
const IN_Y = 140, IN_W = 180, IN_H = 70
const PR_Y = 270, PR_W = 940, PR_H = 76
const ST_Y = 400, ST_W = 200, ST_H = 64
const AC_Y = 510, AC_W = 1080, AC_H = 290
const OU_Y = 840, OU_W = 200, OU_H = 60

const DS_XS = tierPositions(DATA_SOURCES.length, DS_W, 80)
const IN_XS = tierPositions(INGESTION.length,    IN_W, 80)
const ST_XS = tierPositions(STORAGE.length,      ST_W, 40)
const OU_XS = tierPositions(OUTPUTS.length,      OU_W, 40)
const PR_X = (CW - PR_W) / 2
const AC_X = (CW - AC_W) / 2

// AgentCore inner layout (absolute SVG coords)
const AC_PAD = 28
const GW_X = AC_X + AC_PAD,  GW_Y = AC_Y + 40,  GW_W = 240, GW_H = 230
const RT_X = AC_X + 320,     RT_Y = AC_Y + 40,  RT_W = 350, RT_H = 80
const MM_X = AC_X + 700,     MM_Y = AC_Y + 40,  MM_W = 350, MM_H = 80
const BA_X = AC_X + 320,     BA_Y = AC_Y + 150, BA_W = 730, BA_H = 120

function cx(x, w) { return x + w / 2 }
function by(y, h) { return y + h }

// ─── Edge construction ────────────────────────────────────────────────────────

function buildEdges() {
  const edges = []
  const dsById = Object.fromEntries(DATA_SOURCES.map((d, i) => [d.id, i]))
  const inById = Object.fromEntries(INGESTION.map((d, i) => [d.id, i]))

  // 1. Data Source → Ingestion connector (each source pairs with its connector)
  INGESTION.forEach((ing, i) => {
    const di = dsById[ing.sourceId]
    if (di === undefined) return
    const x1 = cx(DS_XS[di], DS_W)
    const y1 = by(DS_Y, DS_H)
    const x2 = cx(IN_XS[i], IN_W)
    const y2 = IN_Y
    const mid = (y1 + y2) / 2
    edges.push({
      id: `ds-${ing.sourceId}-ing-${ing.id}`,
      d: `M ${x1} ${y1} C ${x1} ${mid} ${x2} ${mid} ${x2} ${y2}`,
      active: ing.status === 'ONLINE' || ing.status === 'DEGRADED',
      color: ing.status === 'DEGRADED' ? '#f59e0b' : '#d97706',
      tier: 'ds-in',
    })
  })

  // 2. Ingestion → Processing pipeline (all 4 converge)
  INGESTION.forEach((ing, i) => {
    const x1 = cx(IN_XS[i], IN_W)
    const y1 = by(IN_Y, IN_H)
    const x2 = cx(PR_X, PR_W)
    const y2 = PR_Y
    const mid = (y1 + y2) / 2
    edges.push({
      id: `ing-${ing.id}-pr`,
      d: `M ${x1} ${y1} C ${x1} ${mid} ${x2} ${mid} ${x2} ${y2}`,
      active: true,
      color: '#d97706',
      tier: 'in-pr',
    })
  })

  // 3. Processing → Storage (split out to all 4 storage targets)
  STORAGE.forEach((st, i) => {
    const x1 = cx(PR_X, PR_W)
    const y1 = by(PR_Y, PR_H)
    const x2 = cx(ST_XS[i], ST_W)
    const y2 = ST_Y
    const mid = (y1 + y2) / 2
    edges.push({
      id: `pr-st-${st.id}`,
      d: `M ${x1} ${y1} C ${x1} ${mid} ${x2} ${mid} ${x2} ${y2}`,
      active: true,
      color: '#059669',
      tier: 'pr-st',
    })
  })

  // 4. Storage (KB primarily) → AgentCore compound (top)
  STORAGE.forEach((st, i) => {
    const x1 = cx(ST_XS[i], ST_W)
    const y1 = by(ST_Y, ST_H)
    const x2 = cx(AC_X, AC_W)
    const y2 = AC_Y
    const mid = (y1 + y2) / 2
    const isKB = st.id === 'kb'
    edges.push({
      id: `st-${st.id}-ac`,
      d: `M ${x1} ${y1} C ${x1} ${mid} ${x2} ${mid} ${x2} ${y2}`,
      active: isKB,
      color: '#059669',
      tier: 'st-ac',
    })
  })

  // 5. Inside AgentCore: Gateway → Runtime
  edges.push({
    id: 'gw-rt',
    d: `M ${GW_X + GW_W} ${GW_Y + 40} L ${RT_X} ${RT_Y + RT_H / 2}`,
    active: true,
    color: '#4f46e5',
    tier: 'agentcore-internal',
  })

  // 6. Runtime → Bedrock Agents (vertical)
  edges.push({
    id: 'rt-ba',
    d: `M ${cx(RT_X, RT_W)} ${by(RT_Y, RT_H)} L ${cx(RT_X, RT_W)} ${BA_Y}`,
    active: true,
    color: '#2563eb',
    tier: 'agentcore-internal',
  })

  // 7. Runtime ↔ Memory (horizontal write-back)
  edges.push({
    id: 'rt-mm',
    d: `M ${RT_X + RT_W} ${RT_Y + RT_H / 2} L ${MM_X} ${MM_Y + RT_H / 2}`,
    active: true,
    color: '#7c3aed',
    tier: 'agentcore-internal',
  })

  // 8. AgentCore → Output sinks (split to 4)
  OUTPUTS.forEach((out, i) => {
    const x1 = cx(AC_X, AC_W)
    const y1 = by(AC_Y, AC_H)
    const x2 = cx(OU_XS[i], OU_W)
    const y2 = OU_Y
    const mid = (y1 + y2) / 2
    edges.push({
      id: `ac-out-${out.id}`,
      d: `M ${x1} ${y1} C ${x1} ${mid} ${x2} ${mid} ${x2} ${y2}`,
      active: true,
      color: '#6d28d9',
      tier: 'ac-out',
    })
  })

  return edges
}

const ALL_EDGES = buildEdges()

// ─── SVG node helper ──────────────────────────────────────────────────────────

function statusDotColor(s) {
  if (s === 'ONLINE' || s === 'ACTIVE') return '#10b981'
  if (s === 'DEGRADED') return '#f59e0b'
  if (s === 'IDLE') return '#cbd5e1'
  return '#ef4444'
}

function Node({ x, y, w, h, label, sub, color, status, onClick, selected }) {
  const borderColor = selected ? '#6366f1' : status === 'DEGRADED' ? '#f59e0b' : color + '55'
  const fillColor = selected ? '#eef2ff' : '#ffffff'

  return (
    <g onClick={onClick} style={{ cursor: onClick ? 'pointer' : 'default' }}>
      {selected && (
        <rect x={x - 3} y={y - 3} width={w + 6} height={h + 6} rx={12}
          fill="none" stroke="#6366f1" strokeWidth={1.5} opacity={0.4} />
      )}
      <rect x={x} y={y} width={w} height={h} rx={10} fill={fillColor}
        stroke={borderColor} strokeWidth={selected ? 1.5 : 1} />
      <rect x={x} y={y + 8} width={2.5} height={h - 16} rx={1.5} fill={color} />
      {status && (
        <circle cx={x + w - 14} cy={y + 14} r={4} fill={statusDotColor(status)}>
          {(status === 'ONLINE' || status === 'ACTIVE' || status === 'DEGRADED') && (
            <animate attributeName="opacity" values="1;0.4;1" dur={status === 'DEGRADED' ? '1.4s' : '2.5s'} repeatCount="indefinite" />
          )}
        </circle>
      )}
      <text x={x + 14} y={y + h / 2 - (sub ? 4 : -3)} fill="#0f172a" fontSize={11.5} fontWeight="600"
        fontFamily="ui-sans-serif,system-ui,sans-serif">
        {label.length > 22 ? label.slice(0, 21) + '…' : label}
      </text>
      {sub && (
        <text x={x + 14} y={y + h / 2 + 11} fill="#64748b" fontSize={9.5}
          fontFamily="ui-mono,monospace">
          {sub.length > 38 ? sub.slice(0, 36) + '…' : sub}
        </text>
      )}
    </g>
  )
}

// ─── Topology canvas ──────────────────────────────────────────────────────────

function TopologyCanvas() {
  const [selected, setSelected] = useState(null)
  const containerRef = useRef(null)

  function toggle(id) { setSelected(prev => prev === id ? null : id) }

  return (
    <div className="space-y-3">
      <div className="card p-0 overflow-x-auto">
        <div className="p-4 border-b border-slate-200 flex items-center justify-between flex-wrap gap-2">
          <p className="text-xs font-semibold text-slate-600 uppercase tracking-wider">ARBITER System Architecture</p>
          <div className="flex items-center gap-4 text-[10px] text-slate-500 flex-wrap">
            <span className="flex items-center gap-1.5"><span className="w-2 h-0.5 bg-amber-600 rounded" /> Ingestion flow</span>
            <span className="flex items-center gap-1.5"><span className="w-2 h-0.5 bg-emerald-600 rounded" /> Storage flow</span>
            <span className="flex items-center gap-1.5"><span className="w-2 h-0.5 bg-indigo-600 rounded" /> AgentCore</span>
            <span className="flex items-center gap-1.5"><span className="w-2 h-0.5 bg-violet-600 rounded" /> Output</span>
          </div>
        </div>
        <div style={{ width: 'fit-content', minWidth: CW + 32, padding: '16px', margin: '0 auto' }} ref={containerRef}>
          <svg width={CW} height={CANVAS_H} style={{ display: 'block' }}>

            {/* Tier labels */}
            <text x={20} y={DS_Y - 8} fill="#94a3b8" fontSize={9} fontWeight="700" letterSpacing="0.12em">DATA SOURCES</text>
            <text x={20} y={IN_Y - 8} fill="#94a3b8" fontSize={9} fontWeight="700" letterSpacing="0.12em">INGESTION · LAMBDA CONNECTORS</text>
            <text x={20} y={PR_Y - 8} fill="#94a3b8" fontSize={9} fontWeight="700" letterSpacing="0.12em">PROCESSING & NORMALIZATION</text>
            <text x={20} y={ST_Y - 8} fill="#94a3b8" fontSize={9} fontWeight="700" letterSpacing="0.12em">STORAGE</text>
            <text x={20} y={AC_Y - 8} fill="#94a3b8" fontSize={9} fontWeight="700" letterSpacing="0.12em">
              AMAZON BEDROCK AGENTCORE
            </text>
            <text x={20} y={OU_Y - 8} fill="#94a3b8" fontSize={9} fontWeight="700" letterSpacing="0.12em">OUTPUT SINKS</text>

            {/* AgentCore outer dashed compound container */}
            <rect x={AC_X} y={AC_Y} width={AC_W} height={AC_H} rx={14}
              fill="#f8fafc" stroke="#cbd5e1" strokeWidth={1.5} strokeDasharray="6 4" />

            {/* Edges */}
            {ALL_EDGES.map(edge => (
              <path key={edge.id}
                d={edge.d}
                fill="none"
                stroke={edge.active ? edge.color : '#cbd5e1'}
                strokeWidth={edge.active ? 1.5 : 1}
                strokeLinecap="round"
                className={edge.active ? 'flow-edge-active' : 'flow-edge-idle'}
                opacity={edge.active ? 0.65 : 0.35}
              />
            ))}

            {/* Bedrock Agents inner dashed sub-region */}
            <rect x={BA_X} y={BA_Y} width={BA_W} height={BA_H} rx={10}
              fill="#ffffff" stroke={selected === 'bedrock-agents' ? '#6366f1' : '#a78bfa'}
              strokeWidth={selected === 'bedrock-agents' ? 1.5 : 1}
              strokeDasharray="5 4"
              onClick={() => toggle('bedrock-agents')}
              style={{ cursor: 'pointer' }} />
            <rect x={BA_X} y={BA_Y + 8} width={2.5} height={BA_H - 16} rx={1.5} fill={AGENTCORE.bedrockAgents.color} />
            <text x={BA_X + 14} y={BA_Y + 28} fill="#0f172a" fontSize={12.5} fontWeight="700">{AGENTCORE.bedrockAgents.label}</text>
            {AGENTCORE.bedrockAgents.lines.map((line, i) => (
              <text key={i} x={BA_X + 14} y={BA_Y + 58 + i * 22} fill="#475569" fontSize={11}>{line}</text>
            ))}
            <circle cx={BA_X + BA_W - 14} cy={BA_Y + 14} r={4} fill={statusDotColor('ACTIVE')}>
              <animate attributeName="opacity" values="1;0.4;1" dur="2.5s" repeatCount="indefinite" />
            </circle>

            {/* Data Sources */}
            {DATA_SOURCES.map((src, i) => (
              <Node key={src.id} x={DS_XS[i]} y={DS_Y} w={DS_W} h={DS_H}
                label={src.label} sub={src.sub} color={src.color} status="ONLINE"
                selected={selected === src.id} onClick={() => toggle(src.id)} />
            ))}

            {/* Ingestion Connectors */}
            {INGESTION.map((ing, i) => (
              <Node key={ing.id} x={IN_XS[i]} y={IN_Y} w={IN_W} h={IN_H}
                label={ing.label} sub={ing.sub} color={ing.color} status={ing.status}
                selected={selected === ing.id} onClick={() => toggle(ing.id)} />
            ))}

            {/* Processing Pipeline (wide) */}
            <Node x={PR_X} y={PR_Y} w={PR_W} h={PR_H}
              label={PROCESSING.label} sub={PROCESSING.sub} color={PROCESSING.color} status={PROCESSING.status}
              selected={selected === PROCESSING.id} onClick={() => toggle(PROCESSING.id)} />

            {/* Storage */}
            {STORAGE.map((st, i) => (
              <Node key={st.id} x={ST_XS[i]} y={ST_Y} w={ST_W} h={ST_H}
                label={st.label} sub={st.sub} color={st.color} status={st.status}
                selected={selected === st.id} onClick={() => toggle(st.id)} />
            ))}

            {/* AgentCore inner: Gateway with sub-tools */}
            <Node x={GW_X} y={GW_Y} w={GW_W} h={GW_H}
              label={AGENTCORE.gateway.label} sub={AGENTCORE.gateway.sub} color={AGENTCORE.gateway.color}
              status={AGENTCORE.gateway.status}
              selected={selected === AGENTCORE.gateway.id} onClick={() => toggle(AGENTCORE.gateway.id)} />
            {AGENTCORE.gateway.tools.map((t, i) => {
              const tx = GW_X + 16
              const ty = GW_Y + 60 + i * 38
              return (
                <g key={t.id}>
                  <rect x={tx} y={ty} width={GW_W - 32} height={28} rx={6}
                    fill="#f8fafc" stroke="#cbd5e1" strokeWidth={1} />
                  <text x={tx + 10} y={ty + 18} fill="#475569" fontSize={10.5} fontFamily="ui-mono,monospace">{t.label}</text>
                </g>
              )
            })}

            {/* AgentCore inner: Runtime */}
            <Node x={RT_X} y={RT_Y} w={RT_W} h={RT_H}
              label={AGENTCORE.runtime.label} sub={AGENTCORE.runtime.sub} color={AGENTCORE.runtime.color}
              status={AGENTCORE.runtime.status}
              selected={selected === AGENTCORE.runtime.id} onClick={() => toggle(AGENTCORE.runtime.id)} />

            {/* AgentCore inner: Memory */}
            <Node x={MM_X} y={MM_Y} w={MM_W} h={MM_H}
              label={AGENTCORE.memory.label} sub={AGENTCORE.memory.sub} color={AGENTCORE.memory.color}
              status={AGENTCORE.memory.status}
              selected={selected === AGENTCORE.memory.id} onClick={() => toggle(AGENTCORE.memory.id)} />

            {/* Output Sinks */}
            {OUTPUTS.map((out, i) => (
              <Node key={out.id} x={OU_XS[i]} y={OU_Y} w={OU_W} h={OU_H}
                label={out.label} sub={out.sub} color={out.color} status={out.status}
                selected={selected === out.id} onClick={() => toggle(out.id)} />
            ))}
          </svg>
        </div>
      </div>

      {/* Detail panel for selected node */}
      {selected && <NodeDetail id={selected} onClose={() => setSelected(null)} />}
    </div>
  )
}

function NodeDetail({ id, onClose }) {
  const ing = INGESTION.find(x => x.id === id)
  const st  = STORAGE.find(x => x.id === id)
  const ds  = DATA_SOURCES.find(x => x.id === id)
  const out = OUTPUTS.find(x => x.id === id)

  let title, sub, body
  if (id === 'processing') {
    title = PROCESSING.label
    sub = 'AWS Lambda · Textract · Bedrock Titan Embed Text V2'
    body = (
      <p className="text-xs text-slate-600">
        DOCX/PDF → Textract OCR → semantic chunking → Titan Embed V2 vectors → indexed into Knowledge Base. Lambda concurrency 50, processing avg 1.8s/document.
      </p>
    )
  } else if (id === 'agentcore-gateway') {
    title = AGENTCORE.gateway.label
    sub = AGENTCORE.gateway.sub
    body = (
      <div className="space-y-2">
        <p className="text-xs text-slate-600">Registers tools that Bedrock agents can invoke. Each tool wraps a backing data source with input validation, rate limiting, and audit logging.</p>
        <div className="flex flex-wrap gap-1.5">
          {AGENTCORE.gateway.tools.map(t => (
            <span key={t.id} className="text-[10px] font-mono bg-slate-50 border border-slate-200 text-slate-700 px-2 py-0.5 rounded">{t.label}</span>
          ))}
        </div>
      </div>
    )
  } else if (id === 'agentcore-runtime') {
    title = AGENTCORE.runtime.label
    sub = AGENTCORE.runtime.sub
    body = <p className="text-xs text-slate-600">Master Orchestrator — coordinates the 6 Bedrock Agents, dispatches tool calls through the Gateway, and writes findings to Memory. {MODEL_NAME} with extended thinking enabled.</p>
  } else if (id === 'agentcore-memory') {
    title = AGENTCORE.memory.label
    sub = AGENTCORE.memory.sub
    body = <p className="text-xs text-slate-600">Persists session state, intermediate findings, and conversation context across orchestrator iterations. Backed by AgentCore Memory short- and long-term stores.</p>
  } else if (id === 'bedrock-agents') {
    title = AGENTCORE.bedrockAgents.label
    sub = '6 specialist agents · invoked by Runtime'
    body = (
      <div className="space-y-1.5">
        {AGENTCORE.bedrockAgents.agents.map(a => (
          <div key={a.id} className="flex items-start justify-between gap-3 py-1 border-b border-slate-100 last:border-0">
            <div>
              <p className="text-xs font-medium text-slate-800">{a.name}</p>
              <p className="text-[11px] text-slate-500">{a.role}</p>
            </div>
            <span className="text-[10px] font-mono text-indigo-700 flex-shrink-0">{a.model}</span>
          </div>
        ))}
      </div>
    )
  } else if (ing) {
    title = `${ing.label} · ingestion connector`
    sub = ing.sub
    body = (
      <div className="space-y-2">
        <div className="grid grid-cols-3 gap-2">
          <div className="bg-slate-50 border border-slate-200 rounded-lg px-3 py-2">
            <p className="text-[10px] text-slate-500">Status</p>
            <p className={`text-xs font-semibold ${ing.status === 'DEGRADED' ? 'text-amber-700' : 'text-emerald-700'}`}>{ing.status}</p>
          </div>
          <div className="bg-slate-50 border border-slate-200 rounded-lg px-3 py-2">
            <p className="text-[10px] text-slate-500">Latency</p>
            <p className="text-xs font-semibold text-slate-800">{ing.latencyMs}ms</p>
          </div>
          <div className="bg-slate-50 border border-slate-200 rounded-lg px-3 py-2">
            <p className="text-[10px] text-slate-500">Last sync</p>
            <p className="text-xs font-semibold text-slate-800">{ing.lastSync}</p>
          </div>
        </div>
        {ing.error && (
          <div className="bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 flex items-start gap-2">
            <AlertTriangle size={12} className="text-amber-600 mt-0.5 flex-shrink-0" />
            <p className="text-xs text-amber-800">{ing.error}</p>
          </div>
        )}
      </div>
    )
  } else if (ds) {
    title = ds.label
    sub = ds.sub
    body = <p className="text-xs text-slate-600">External source system. Ingested into ARBITER via a Lambda connector that polls or streams changes into the raw S3 zone.</p>
  } else if (st) {
    title = st.label
    sub = st.sub
    body = <p className="text-xs text-slate-600">Storage layer. Populated by the processing pipeline and consumed by AgentCore at scan time.</p>
  } else if (out) {
    title = out.label
    sub = out.sub
    body = <p className="text-xs text-slate-600">Downstream sink — written by AgentCore at the end of each scan / remediation flow.</p>
  } else {
    return null
  }

  return (
    <div className="card slide-in">
      <div className="flex items-start justify-between mb-2">
        <div>
          <p className="text-sm font-semibold text-slate-900">{title}</p>
          <p className="text-xs text-slate-500">{sub}</p>
        </div>
        <button onClick={onClose} className="btn-ghost text-xs py-1 px-2">✕</button>
      </div>
      {body}
    </div>
  )
}

// ─── Severity helpers ─────────────────────────────────────────────────────────

const SEVERITIES = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']
const DOMAINS    = ['SharePoint', 'Zscaler', 'AWSConfig', 'PaloAlto']

function cellClass(n) {
  if (n === 0) return 'hm-0'
  if (n === 1) return 'hm-1'
  if (n === 2) return 'hm-2'
  if (n === 3) return 'hm-3'
  return 'hm-4'
}

function statusColor(s) {
  if (s === 'ONLINE' || s === 'ACTIVE') return 'text-emerald-700'
  if (s === 'DEGRADED') return 'text-amber-700'
  if (s === 'IDLE') return 'text-slate-500'
  return 'text-red-700'
}

function StatusDot({ status }) {
  const cls =
    status === 'ONLINE' || status === 'ACTIVE' ? 'status-dot-online' :
    status === 'DEGRADED' ? 'status-dot-degraded' :
    status === 'IDLE' ? 'status-dot-idle' : 'status-dot-offline'
  return <span className={cls} />
}

// ─── Conflict Matrix tab ──────────────────────────────────────────────────────
// The doc-mandated grid: Compliance Domain (rows) × Source Pair (cols).
// Click a cell to drill into the matching Findings filter. The legacy
// Source × Severity grid remains accessible via the bottom tab toggle.

function DomainSourceMatrixView({ findings, loading, onCellClick }) {
  const matrix = buildDomainSourceMatrix(findings)
  if (loading) return (
    <div className="flex items-center justify-center py-24">
      <Loader2 size={24} className="animate-spin text-slate-400" />
    </div>
  )
  return (
    <div className="card overflow-x-auto">
      <p className="text-xs font-semibold text-slate-600 uppercase tracking-wider mb-4">
        Compliance Domain × Source System
      </p>
      <table className="w-auto min-w-max text-sm border-separate border-spacing-1.5">
        <thead>
          <tr>
            <th className="text-left text-xs text-slate-500 font-medium pb-2 w-44">Domain</th>
            {SOURCE_PAIRS.map(s => (
              <th key={s} className="text-[11px] text-slate-700 font-semibold pb-2 text-center w-24 leading-tight">{s}</th>
            ))}
            <th className="text-xs text-slate-500 font-medium pb-2 text-center w-14">Total</th>
          </tr>
        </thead>
        <tbody>
          {DOMAIN_KEYS.map(dk => {
            const rowTotal = SOURCE_PAIRS.reduce((s, sp) => s + (matrix[dk]?.[sp] ?? 0), 0)
            return (
              <tr key={dk}>
                <td className="text-xs text-slate-700 font-semibold pr-4 py-1 whitespace-nowrap">{DOMAIN_LABELS[dk]}</td>
                {SOURCE_PAIRS.map(sp => {
                  const count = matrix[dk]?.[sp] ?? 0
                  return (
                    <td key={sp} className="text-center py-1">
                      <button
                        onClick={() => count > 0 && onCellClick?.(dk, sp)}
                        disabled={count === 0}
                        className={`w-16 h-11 rounded-lg font-bold text-base transition-transform hover:scale-105 disabled:cursor-default disabled:hover:scale-100 ${cellClass(count)}`}
                      >
                        {count > 0 ? count : '—'}
                      </button>
                    </td>
                  )
                })}
                <td className="text-center">
                  <span className="text-xs font-bold text-slate-700">{rowTotal}</span>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function SourceSeverityMatrixView({ findings }) {
  const matrix = buildConflictMatrix(findings)
  return (
    <div className="card overflow-x-auto">
      <p className="text-xs font-semibold text-slate-600 uppercase tracking-wider mb-4">
        Source × Severity (legacy view)
      </p>
      <table className="w-auto min-w-max text-sm border-separate border-spacing-1.5">
        <thead>
          <tr>
            <th className="text-left text-xs text-slate-500 font-medium pb-2 w-36">Source</th>
            {SEVERITIES.map(s => (
              <th key={s} className={`text-xs font-bold pb-2 text-center w-20 ${
                s === 'CRITICAL' ? 'text-red-700' : s === 'HIGH' ? 'text-orange-700' :
                s === 'MEDIUM' ? 'text-amber-700' : 'text-emerald-700'
              }`}>{s}</th>
            ))}
            <th className="text-xs text-slate-500 font-medium pb-2 text-center w-14">Total</th>
          </tr>
        </thead>
        <tbody>
          {DOMAINS.map(domain => {
            const rowTotal = SEVERITIES.reduce((sum, s) => sum + (matrix[domain]?.[s] ?? 0), 0)
            return (
              <tr key={domain}>
                <td className="text-xs text-slate-700 font-semibold pr-4 py-1 whitespace-nowrap">{domain}</td>
                {SEVERITIES.map(sev => {
                  const count = matrix[domain]?.[sev] ?? 0
                  return (
                    <td key={sev} className="text-center py-1">
                      <div className={`w-16 h-11 rounded-lg font-bold text-base flex items-center justify-center ${cellClass(count)}`}>
                        {count > 0 ? count : '—'}
                      </div>
                    </td>
                  )
                })}
                <td className="text-center">
                  <span className="text-xs font-bold text-slate-700">{rowTotal}</span>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function ConflictMatrix({ findings, loading }) {
  const navigate = useNavigate()
  const [matrixTab, setMatrixTab] = useState('domain-source')
  const [teamScope, setTeamScope] = useState('')
  // Per-team segregation: scope the matrix to a single team across all three
  // ownership axes (owner / consumer / platform).
  const scoped = teamScope
    ? findings.filter(f => [f.owner_team, f.consumer_team, f.platform_team].includes(teamScope))
    : findings
  const contradictions = scoped.filter(f => f.conflict_type === 'CONTRADICTION').length
  const gaps          = scoped.filter(f => f.conflict_type === 'GAP').length
  const drifts        = scoped.filter(f => f.conflict_type === 'DRIFT').length

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        {[
          { label: 'Total Conflicts', value: scoped.length,                                                         color: 'text-slate-900' },
          { label: 'Contradiction',   value: contradictions,                                                        color: 'text-rose-700' },
          { label: 'Gap',             value: gaps,                                                                  color: 'text-amber-700' },
          { label: 'Drift',           value: drifts,                                                                color: 'text-orange-700' },
        ].map(k => (
          <div key={k.label} className="card text-center py-3">
            <p className={`text-2xl font-bold ${k.color}`}>{k.value}</p>
            <p className="text-xs text-slate-500 mt-1">{k.label}</p>
          </div>
        ))}
      </div>

      <div className="flex gap-2 text-xs items-center">
        <button
          onClick={() => setMatrixTab('domain-source')}
          className={matrixTab === 'domain-source' ? 'btn-primary' : 'btn-ghost'}
        >Domain × Source</button>
        <button
          onClick={() => setMatrixTab('source-severity')}
          className={matrixTab === 'source-severity' ? 'btn-primary' : 'btn-ghost'}
        >Source × Severity</button>
        <select value={teamScope} onChange={e => setTeamScope(e.target.value)} className="input w-44 text-xs ml-auto">
          <option value="">All Teams</option>
          {TEAMS.map(t => <option key={t} value={t}>{TEAM_LABELS[t]}</option>)}
        </select>
      </div>

      {matrixTab === 'domain-source'
        ? <DomainSourceMatrixView
            findings={scoped}
            loading={loading}
            onCellClick={(dk, sp) => navigate(`/findings?domain=${dk}&source=${encodeURIComponent(sp)}${teamScope ? `&team=${teamScope}` : ''}`)}
          />
        : <SourceSeverityMatrixView findings={scoped} />
      }

      <div className="flex items-center gap-4 pt-1">
        <p className="text-xs text-slate-500 font-medium">Density:</p>
        {[
          { label: '0',  cls: 'hm-0' },
          { label: '1',  cls: 'hm-1' },
          { label: '2',  cls: 'hm-2' },
          { label: '3',  cls: 'hm-3' },
          { label: '4+', cls: 'hm-4' },
        ].map(l => (
          <div key={l.label} className="flex items-center gap-1.5">
            <div className={`w-7 h-7 rounded flex items-center justify-center text-xs font-bold ${l.cls}`}>{l.label}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

// ─── Service Health tab ───────────────────────────────────────────────────────

function ServiceHealth() {
  const services = [
    ...INGESTION.map(i => ({ id: i.id, name: `${i.label} ingestion`, type: 'Connector', status: i.status, version: i.sub, detail: `${i.latencyMs}ms · ${i.lastSync}` })),
    { id: 'processing', name: PROCESSING.label, type: 'Pipeline', status: PROCESSING.status, version: PROCESSING.sub, detail: 'Lambda · concurrency 50' },
    ...STORAGE.map(s => ({ id: s.id, name: s.label, type: 'Storage', status: s.status, version: s.sub, detail: '—' })),
    { id: 'gw',  name: AGENTCORE.gateway.label,  type: 'AgentCore', status: AGENTCORE.gateway.status,  version: AGENTCORE.gateway.sub,  detail: `${AGENTCORE.gateway.tools.length} tools registered` },
    { id: 'rt',  name: AGENTCORE.runtime.label,  type: 'AgentCore', status: AGENTCORE.runtime.status,  version: AGENTCORE.runtime.sub,  detail: MODEL_NAME },
    { id: 'mm',  name: AGENTCORE.memory.label,   type: 'AgentCore', status: AGENTCORE.memory.status,   version: AGENTCORE.memory.sub,   detail: 'short + long term stores' },
    ...AGENTCORE.bedrockAgents.agents.map(a => ({ id: a.id, name: a.name, type: 'Bedrock Agent', status: 'ACTIVE', version: a.model, detail: a.role })),
    ...OUTPUTS.map(o => ({ id: o.id, name: o.label, type: 'Output sink', status: o.status, version: o.sub, detail: '—' })),
  ]

  const operational = services.filter(s => ['ONLINE', 'ACTIVE', 'IDLE'].includes(s.status)).length
  const degraded    = services.filter(s => s.status === 'DEGRADED').length
  const offline     = services.filter(s => s.status === 'OFFLINE').length

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-3 gap-3">
        {[
          { label: 'Operational', count: operational, total: services.length, color: 'text-emerald-700' },
          { label: 'Degraded',    count: degraded,    total: null,             color: 'text-amber-700' },
          { label: 'Offline',     count: offline,     total: null,             color: 'text-red-700' },
        ].map(k => (
          <div key={k.label} className="card text-center py-3">
            <p className={`text-2xl font-bold ${k.color}`}>{k.count}{k.total ? `/${k.total}` : ''}</p>
            <p className="text-xs text-slate-500 mt-1">{k.label}</p>
          </div>
        ))}
      </div>

      <div className="card">
        <p className="text-xs font-semibold text-slate-600 uppercase tracking-wider mb-3">Service Registry</p>
        <div className="space-y-0.5">
          {services.map(svc => (
            <div key={svc.id} className="flex items-center gap-3 py-2.5 border-b border-slate-100 last:border-0">
              <StatusDot status={svc.status} />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-sm text-slate-800 font-medium truncate">{svc.name}</span>
                  <span className="text-[10px] text-slate-500 bg-slate-50 border border-slate-200 px-1.5 py-0.5 rounded">{svc.type}</span>
                </div>
                <p className="text-[10px] font-mono text-slate-400 truncate mt-0.5">{svc.version}</p>
              </div>
              <div className="text-right hidden md:block">
                <p className={`text-xs font-semibold ${statusColor(svc.status)}`}>{svc.status}</p>
                {svc.detail && svc.detail !== '—' && (
                  <p className="text-[10px] text-slate-400 mt-0.5 truncate max-w-[260px]">{svc.detail}</p>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

// ─── Main page ────────────────────────────────────────────────────────────────

const TABS = [
  { id: 'canvas',   label: 'Live Topology',     icon: Network },
  { id: 'matrix',   label: 'Conflict Matrix',   icon: BarChart3 },
  { id: 'health',   label: 'Service Health',    icon: HeartPulse },
]

export default function HeatMap() {
  const [tab, setTab] = useState('canvas')
  const { findings, loading, load } = useFindings()

  useEffect(() => { load() }, [load])

  // Live scan feed: re-render the conflict matrix the moment a background scan
  // completes. Silent auto-refresh — the matrix is a glanceable view, not an
  // editable list.
  const { activeRun } = useScanFeed({ onNewScan: () => load() })

  const onlineConnectors    = INGESTION.filter(i => i.status === 'ONLINE').length
  const degradedConnectors  = INGESTION.filter(i => i.status === 'DEGRADED').length
  const activeAgents        = AGENTCORE.bedrockAgents.agents.length

  return (
    <div className="p-5 space-y-4">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-lg font-bold text-slate-900 tracking-tight">System Map</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            ARBITER architecture · ingestion → AgentCore → outputs · Meridian Insurance Group
          </p>
        </div>
        <div className="flex items-center gap-2">
          {activeRun && (
            <span className="flex items-center gap-1.5 text-[11px] text-indigo-700 bg-indigo-50 border border-indigo-200 px-2 py-1 rounded-md">
              <Loader2 size={11} className="animate-spin" /> Scanning…
            </span>
          )}
          <button onClick={() => load()} className="btn-ghost flex items-center gap-1.5 text-xs">
            <RefreshCw size={13} /> Refresh
          </button>
        </div>
      </div>

      <div className="grid grid-cols-4 gap-3">
        {[
          { label: 'Connectors Online',    value: `${onlineConnectors}/${INGESTION.length}`, color: 'text-emerald-700' },
          { label: 'Degraded Connectors',  value: degradedConnectors,                          color: 'text-amber-700' },
          { label: 'Bedrock Agents',       value: activeAgents,                                color: 'text-indigo-700' },
          { label: 'Storage Layers',       value: STORAGE.length,                              color: 'text-slate-900' },
        ].map(k => (
          <div key={k.label} className="card text-center py-3">
            <p className={`text-2xl font-bold ${k.color}`}>{k.value}</p>
            <p className="text-xs text-slate-500 mt-1">{k.label}</p>
          </div>
        ))}
      </div>

      <div className="flex border-b border-slate-200">
        {TABS.map(t => {
          const Icon = t.icon
          return (
            <button key={t.id} onClick={() => setTab(t.id)}
              className={`flex items-center gap-1.5 ${tab === t.id ? 'tab-active' : 'tab-inactive'}`}>
              <Icon size={13} />
              {t.label}
            </button>
          )
        })}
      </div>

      {tab === 'canvas' && <TopologyCanvas />}
      {tab === 'matrix' && <ConflictMatrix findings={findings} loading={loading} />}
      {tab === 'health' && <ServiceHealth />}
    </div>
  )
}
