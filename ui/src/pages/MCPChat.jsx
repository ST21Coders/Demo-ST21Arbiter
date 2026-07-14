import { useState, useRef, useEffect } from 'react'
import {
  Terminal, Send, Loader2, ChevronRight, Server, Zap,
  CheckCircle, AlertTriangle, Activity, Clock, Copy,
  Shield, Wifi, WifiOff, MessageSquare, Plus, RotateCcw, Database,
  Download,
} from 'lucide-react'
import { CHAT_URL } from '../config'
import { listDataGroupingProjects, useConversations, sendChat, useAgentStatus } from '../hooks/useApi'
import { detectProblem } from '../detectProblem'
import CreateTicketButton from '../components/CreateTicketButton'
import ClearChatsButton from '../components/ClearChatsButton'
import { downloadChatPdf } from '../utils/pdfReport'
import { dataGroupContentType, isDataGroupChatTarget, isStructuredEvidenceQuestion } from '../dataGroupRouting'

/* ─── MCP server registry ────────────────────────────────────────────
   Each entry maps to a real ARBITER AgentCore runtime. `id` is the routing
   target sent to POST /chat (api_handler resolves it → runtime ARN). Live
   status comes from useAgentStatus() (GET /agent-status). */

const MCP_SERVERS = [
  {
    id: 'master',
    name: 'Arbiter Orchestrator',
    host: 'agentcore · master_orchestrator',
    description: 'Routes mixed Data Groups to the appropriate structured and document specialists.',
    tools: [
      { name: 'specialist routing', desc: 'Coordinates evidence across the selected Data Group' },
    ],
  },
  {
    id: 'sharepoint',
    name: 'SharePoint Specialist',
    host: 'agentcore · sharepoint_specialist',
    description: 'Retrieves enterprise policy documents from the SharePoint-backed knowledge base.',
    tools: [
      { name: 'retrieve_policies', desc: 'Semantic search across the policy knowledge base' },
    ],
  },
  {
    id: 'zscaler',
    name: 'Zscaler ZIA Specialist',
    host: 'agentcore · zscaler_specialist',
    description: 'Answers questions about Zscaler Internet Access URL allowlists and category policy.',
    tools: [
      { name: 'retrieve_zscaler_policy', desc: 'KB lookup of ZIA policy exports' },
      { name: 'lookup_url_category', desc: 'Live URL category classification' },
    ],
  },
  {
    id: 'awsconfig',
    name: 'AWS Resource & Posture Specialist',
    host: 'agentcore · awsconfig_specialist',
    description: 'Read-only inventory, network/exposure, and change-impact analysis across the AWS account (S3, ELB, ECR, Lambda, EC2, Cognito, VPC) plus Config compliance. Credentials are never returned.',
    tools: [
      { name: 'list_resources', desc: 'Account-wide resource inventory (AWS Config query)' },
      { name: 'get_resource_relationships', desc: 'Dependency graph for blast-radius / impact' },
      { name: 'describe_network', desc: 'VPCs, public/private subnets, open security groups' },
      { name: 'describe_ec2_instances', desc: 'EC2 placement + public-exposure posture' },
      { name: 'describe_load_balancers', desc: 'ELBv2 scheme, listeners, target groups' },
      { name: 'describe_lambdas', desc: 'Lambda config + env-var keys (values redacted)' },
      { name: 'describe_s3_buckets', desc: 'Bucket public-access, encryption, versioning' },
      { name: 'describe_ecr_repositories', desc: 'ECR repos, scan-on-push, tag mutability' },
      { name: 'describe_glue', desc: 'Glue crawlers (state, schedule, last run) + catalog DBs' },
      { name: 'describe_dynamodb_tables', desc: 'DynamoDB table config — keys/indexes/encryption/PITR (no item data)' },
      { name: 'describe_cognito', desc: 'User pools + clients (secrets redacted), removal impact' },
      { name: 'list_config_rules', desc: 'AWS Config rules + compliance state' },
      { name: 'retrieve_awsconfig_docs', desc: 'KB lookup of control guidance' },
    ],
  },
  {
    id: 'structured',
    name: 'Structured Data Specialist',
    host: 'agentcore · structured_specialist',
    description: 'Answers project-centric questions across grouped project files and Glue-catalogued datasets.',
    tools: [
      { name: 'list_projects', desc: 'List Data Grouping projects and their structured table hints' },
      { name: 'run_athena_query', desc: 'Read-only SELECT queries against the structured Glue catalog' },
    ],
  },
  {
    id: 'paloalto',
    name: 'Palo Alto NGFW Specialist',
    host: 'agentcore · paloalto_specialist',
    description: 'Answers questions about Palo Alto perimeter firewall security rules, App-ID enforcement, and egress controls.',
    tools: [
      { name: 'retrieve_paloalto_policy', desc: 'KB lookup of PAN-OS rulebase exports' },
      { name: 'lookup_firewall_rule', desc: 'Live PAN-OS rule / App-ID lookup' },
    ],
  },
  {
    id: 'jira',
    name: 'JIRA Specialist',
    host: 'agentcore · jira_specialist',
    description: 'Reads and creates Jira issues, projects, and sprints via the Atlassian MCP server.',
    tools: [
      { name: 'jira (MCP)', desc: 'Issues, projects, sprints via mcp-atlassian' },
    ],
  },
  {
    id: 'servicenow',
    name: 'ServiceNow Specialist',
    host: 'agentcore · servicenow_specialist',
    description: 'Full ITSM/ITAM over the ServiceNow REST API: CMDB, Incident, Problem, Change, Asset Management, and CMDB/asset drift detection.',
    tools: [
      { name: 'query_ci / get_ci_details', desc: 'Resolve an AWS resource/ARN to a CMDB CI' },
      { name: 'get_affected_cis', desc: 'Blast-radius traversal over cmdb_rel_ci' },
      { name: 'query_incident / query_problem', desc: 'Look up incidents & problems' },
      { name: 'update_incident / comment_incident', desc: 'Change state, assign, add work notes' },
      { name: 'comment_problem', desc: 'Add work notes/comments to a problem' },
      { name: 'query_asset', desc: 'Look up hardware/software assets (alm_asset)' },
      { name: 'query_change', desc: 'Look up a change_request by number' },
      { name: 'detect_drift', desc: 'CMDB/asset hygiene gaps (see Drift Scan page)' },
    ],
  },
]

/* Map the backend runtime status → a UI bucket used for the status dot, a
   display label, and whether chat is allowed. Unknown (status not yet loaded)
   stays chat-enabled so a transient /agent-status hiccup never blocks a real
   agent. */
function deriveStatus(raw, staticPlaceholder) {
  if (staticPlaceholder) return { bucket: 'OFFLINE', label: 'PLACEHOLDER', chat: false }
  if (!raw) return { bucket: 'PENDING', label: 'CHECKING…', chat: true }
  if (raw === 'READY') return { bucket: 'ONLINE', label: 'READY', chat: true }
  if (raw === 'PLACEHOLDER') return { bucket: 'OFFLINE', label: 'NOT DEPLOYED', chat: false }
  if (raw.endsWith('FAILED') || raw === 'DELETING')
    return { bucket: 'OFFLINE', label: raw, chat: false }
  return { bucket: 'DEGRADED', label: raw, chat: true } // CREATING / UPDATING / …
}

/* ─── Components ─────────────────────────────────────────────────────── */

// Status bucket → tailwind colour classes for the dot + label.
const DOT_CLASS = { ONLINE: 'bg-emerald-500', DEGRADED: 'bg-amber-500', PENDING: 'bg-slate-400', OFFLINE: 'bg-red-500' }
const TEXT_CLASS = { ONLINE: 'text-emerald-600', DEGRADED: 'text-amber-600', PENDING: 'text-slate-500', OFFLINE: 'text-red-600' }

function ServerListItem({ server, selected, onSelect }) {
  return (
    <button
      onClick={() => onSelect(server)}
      className={`w-full text-left px-3 py-3 rounded-lg border transition-all ${selected?.id === server.id
          ? 'bg-indigo-50 border-indigo-300 text-indigo-700'
          : 'bg-white border-slate-200 hover:border-slate-300 hover:bg-slate-50 text-slate-700'
        }`}
    >
      <div className="flex items-center gap-2 mb-1">
        <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${DOT_CLASS[server.bucket] || 'bg-slate-400'}`} />
        <span className="text-xs font-semibold truncate">{server.name}</span>
      </div>
      <div className="flex items-center justify-between text-[10px] text-slate-500 pl-3.5">
        <span className="font-mono truncate">{server.host}</span>
        <span className={TEXT_CLASS[server.bucket] || 'text-slate-500'}>{server.label}</span>
      </div>
    </button>
  )
}

function ToolBadge({ name }) {
  return (
    <span className="inline-flex items-center gap-1 text-[10px] font-mono bg-indigo-50 border border-indigo-200 text-indigo-700 px-1.5 py-0.5 rounded">
      <Zap size={8} /> {name}
    </span>
  )
}

function reportTitleFromMessage(msg) {
  const lines = String(msg.content || '')
    .split('\n')
    .map(line => line.replace(/^#+\s*/, '').replace(/\*\*/g, '').trim())
    .filter(Boolean)
  const genericHeadings = new Set([
    'logical first query',
    'matching files',
    'matching structured tables',
    'available tables',
    'available files',
    'summary',
    'scope',
    'how to read this',
    'suggested follow-up prompts',
    'other useful first questions',
  ])
  const reportLine = lines.find(line => (
    /report|lookup|review|summary/i.test(line)
    && !genericHeadings.has(line.toLowerCase())
    && line.length <= 120
  ))
  if (reportLine) return reportLine

  const groupLine = lines.find(line => /^Group:\s*/i.test(line))
  const projectLine = lines.find(line => /^Project:\s*/i.test(line))
  const group = groupLine?.replace(/^Group:\s*/i, '').trim()
  const project = projectLine?.replace(/^Project:\s*/i, '').replace(/\s*\([^)]*\)\s*$/, '').trim()
  if (group && project) return `${project} ${group} Report`
  if (group) return `${group} Report`

  const firstUseful = lines.find(line => !genericHeadings.has(line.toLowerCase()) && line.length <= 120)
  return firstUseful || 'ARBITER Report'
}

function Message({ msg }) {
  const isUser = msg.role === 'user'

  if (isUser) {
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
        <Terminal size={13} className="text-indigo-600" />
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-sm text-slate-800 leading-relaxed prose-mcp">
          {msg.content.split('\n').map((line, i) => {
            if (line.startsWith('```')) return null
            if (line.startsWith('**') && line.endsWith('**')) {
              return <p key={i} className="font-semibold text-slate-900 mb-1">{line.replace(/\*\*/g, '')}</p>
            }
            if (line.startsWith('- ') || line.startsWith('1. ') || line.startsWith('2. ') || line.startsWith('3. ') || line.startsWith('4. ') || line.startsWith('5. ') || line.startsWith('6. ') || line.startsWith('7. ') || line.startsWith('8. ') || line.startsWith('9. ') || line.startsWith('10. ') || line.startsWith('11. ') || line.startsWith('12. ')) {
              return <p key={i} className="text-slate-700 text-xs my-0.5 ml-2">{line}</p>
            }
            if (line.startsWith('|') && line.endsWith('|')) {
              return <p key={i} className="font-mono text-xs text-slate-600 my-0.5">{line}</p>
            }
            if (line.includes('```')) {
              return null
            }
            if (line.trim() === '') return <div key={i} className="h-1" />
            return <p key={i} className="text-slate-700 text-xs my-0.5">{line}</p>
          })}
        </div>
        {msg.toolCalls?.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1">
            {msg.toolCalls.map(t => <ToolBadge key={t} name={t} />)}
          </div>
        )}
        <p className="text-[10px] text-slate-400 mt-1.5">{msg.time}</p>
      </div>
    </div>
  )
}

const SUGGESTED = {
  sharepoint: ['What does MIG-POL-001 say about cloud collaboration tools?', 'Find the remote access policy', 'Search policies for MFA requirements'],
  zscaler: ['Is dropbox.com allowed?', 'What URL categories are blocked?', 'Check the TeamViewer category'],
  paloalto: ['Is outbound tor traffic allowed at the perimeter?', 'Show the egress security rules', 'What does PAN-SEC-EGRESS-ANYANY-ALLOW-001 permit?'],
  awsconfig: ['List non-compliant resources', 'Which Config rules are failing?', 'Show S3 encryption compliance'],
  structured: [
    'List the available files and tables in this group and briefly explain what each one appears to contain.',
    {
      label: 'AI Summary group_key.json',
      prompt: 'Summarize this group\'s group_key.json for the user. Include purpose, domain, file mix, table/file structure, relationships, starter prompts, query wording guidance, and useful next questions.',
    },
    {
      label: 'Logical first query',
      prompt: 'Recommend a logical first query for this group. Use the group purpose, detailed description, available tables, row counts if available, and important columns. Return one best query first, then a few useful alternatives.',
    },
  ],
  jira: ['List my open issues', 'Show issues in the MIG project', 'What is the status of MIG-123?'],
  servicenow: [
    'What is the impact of changing alb-mig-prod-claims-api-001?',
    'Show open incidents on the Claims API CI',
    'Add a work note to INC0010001: investigating with the cloud team',
    'Look up asset P1000099',
    'What CMDB/asset hygiene drift do you see?',
  ],
}

const MCP_CHAT_DRAFT_KEY = 'arbiter.mcpChat.sessionDraft.v1'
const DATA_GROUPING_GROUPS_KEY = 'arbiter.dataGrouping.v2.savedGroups'
const DEFAULT_DATA_PROJECT_ID = 'discovery'
const DEFAULT_DATA_PROJECT_NAME = 'Discovery'

function readMcpChatDraft() {
  if (typeof window === 'undefined') return null
  try {
    const draft = JSON.parse(sessionStorage.getItem(MCP_CHAT_DRAFT_KEY) || 'null')
    if (!draft || !Array.isArray(draft.messages)) return null
    return draft
  } catch {
    return null
  }
}

function writeMcpChatDraft(draft) {
  if (typeof window === 'undefined') return
  try {
    sessionStorage.setItem(MCP_CHAT_DRAFT_KEY, JSON.stringify(draft))
  } catch {
    // Best-effort only; chat still works if the browser denies storage.
  }
}

function clearMcpChatDraft() {
  if (typeof window === 'undefined') return
  try {
    sessionStorage.removeItem(MCP_CHAT_DRAFT_KEY)
  } catch {
    // Best-effort only; chat reset still works if the browser denies storage.
  }
}

function readLocalDataGroupingGroups() {
  if (typeof window === 'undefined') return []
  try {
    const saved = JSON.parse(localStorage.getItem(DATA_GROUPING_GROUPS_KEY) || '[]')
    if (!Array.isArray(saved)) return []
    return saved
      .filter(group => group?.name)
      .map(group => {
        const files = (group.files || []).map(file => ({
          name: file?.name || file?.key || 'Unnamed file',
          type: /\.csv$/i.test(file?.name || '') ? 'csv' : 'file',
          glueTableHint: file?.glueTableHint,
        }))
        const fileCount = Array.isArray(group.files) ? group.files.length : Array.isArray(group.fileKeys) ? group.fileKeys.length : 0
        const csvCount = files.filter(file => file.type === 'csv').length
        const projectId = group.projectId || DEFAULT_DATA_PROJECT_ID
        const projectName = group.projectName || DEFAULT_DATA_PROJECT_NAME
        return {
          id: `local::${projectId}::${group.id || group.name}`,
          projectId,
          projectName,
          groupName: group.name,
          label: `${projectName} / ${group.name}`,
          value: group.name,
          fileCount,
          csvCount,
          tableCount: 0,
          files,
          groupProfile: group.groupProfile,
          local: true,
        }
      })
  } catch {
    return []
  }
}

function mergeDataGroupOptions(primary = [], fallback = []) {
  const byProjectGroup = new Map()
  ;[...fallback, ...primary].forEach(group => {
    if (!group?.groupName) return
    const projectId = group.projectId || 'unknown-project'
    const groupName = normalizeGroupMention(group.groupName)
    byProjectGroup.set(`${projectId}::${groupName}`, group)
  })
  return [...byProjectGroup.values()].sort((a, b) => String(a.label || '').localeCompare(String(b.label || '')))
}

function projectOptionsFromGroups(groups = []) {
  const byId = new Map()
  groups.forEach(group => {
    const projectId = group.projectId || DEFAULT_DATA_PROJECT_ID
    const projectName = group.projectName || projectId || DEFAULT_DATA_PROJECT_NAME
    if (projectId) byId.set(projectId, { id: projectId, name: projectName })
  })
  if (!byId.size) byId.set(DEFAULT_DATA_PROJECT_ID, { id: DEFAULT_DATA_PROJECT_ID, name: DEFAULT_DATA_PROJECT_NAME })
  return [...byId.values()].sort((a, b) => String(a.name).localeCompare(String(b.name)))
}

function normalizeGroupMention(value) {
  return String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim()
}

function findOutOfScopeGroup(prompt, selectedGroup, groups) {
  if (!selectedGroup) return null
  const text = ` ${normalizeGroupMention(prompt)} `
  const selectedNames = new Set([
    normalizeGroupMention(selectedGroup.groupName),
    normalizeGroupMention(selectedGroup.label),
  ].filter(Boolean))
  return groups.find(group => {
    if (!group?.groupName || group.id === selectedGroup.id) return false
    const candidates = [
      normalizeGroupMention(group.groupName),
      normalizeGroupMention(group.label),
    ].filter(Boolean)
    return candidates.some(candidate => (
      candidate.length >= 5
      && !selectedNames.has(candidate)
      && text.includes(` ${candidate} `)
    ))
  }) || null
}

function findGroupOutsideSelectedProject(prompt, selectedProjectId, groups) {
  if (!selectedProjectId) return null
  const text = ` ${normalizeGroupMention(prompt)} `
  return groups.find(group => {
    if (!group?.groupName || (group.projectId || DEFAULT_DATA_PROJECT_ID) === selectedProjectId) return false
    const candidates = [
      normalizeGroupMention(group.groupName),
      normalizeGroupMention(group.label),
    ].filter(Boolean)
    return candidates.some(candidate => (
      candidate.length >= 5
      && text.includes(` ${candidate} `)
    ))
  }) || null
}

function looksLikeGroupInventoryQuestion(question) {
  const text = normalizeGroupMention(question)
  return (
    (text.includes('list') || text.includes('show') || text.includes('summarize') || text.includes('describe'))
    && (
      text.includes('available files')
      || text.includes('available tables')
      || text.includes('files and tables')
      || text.includes('files found')
      || text.includes('tables in this group')
      || (text.includes('files') && text.includes('intent'))
    )
  )
}

function looksLikeDeterministicStructuredQuestion(question) {
  const text = normalizeGroupMention(question)
  const rawText = String(question || '')
  const hasVendorId = /\bV\d{3,6}\b/i.test(rawText)
  const hasDocumentTerm = [
    'invoice', 'invoices', 'contract', 'contracts', 'audit', 'audits',
    'credentialing', 'legal review', 'security review', 'rate sheet',
    'payment reconciliation', 'performance review', 'renewal memo',
    'scope of work', 'amendment', 'email thread', 'meeting notes',
  ].some(term => text.includes(term))
  const hasContractActivityLookup = (
    text.includes('expired contract')
    && (text.includes('invoice') || text.includes('payment'))
  )
  const hasVendorIntelligenceLookup = (
    (text.includes('security review') && text.includes('risk score'))
    || (text.includes('payment reconciliation') && text.includes('performance review'))
    || ((text.includes('contract') || text.includes('amendment')) && text.includes('contract end'))
  )
  return (
    looksLikeGroupInventoryQuestion(question)
    || (hasVendorId && hasDocumentTerm)
    || hasContractActivityLookup
    || hasVendorIntelligenceLookup
    || text.includes('logical first query')
    || text.includes('group key')
    || text.includes('groupkey')
    || text.includes('group key json')
    || text.includes('group_key json')
    || (
      text.includes('summarize')
      && text.includes('group')
      && (
        text.includes('row count')
        || text.includes('important columns')
        || text.includes('first questions')
      )
    )
  )
}

function buildDataGroupScopedPrompt(question, selectedGroup) {
  if (!selectedGroup) return question
  const minimalContext = looksLikeDeterministicStructuredQuestion(question)
  if (minimalContext) {
    return [
      'Resolved project/group context from the UI selector.',
      `Project: ${selectedGroup.projectName || selectedGroup.projectId || 'Selected project'} (${selectedGroup.projectId || 'unknown'})`,
      `Group: ${selectedGroup.groupName}`,
      `Selected group file count: ${selectedGroup.fileCount || selectedGroup.files?.length || 0}`,
      '',
      `User request:\n${question}`,
    ].filter(Boolean).join('\n')
  }
  const includeFileInventory = (
    looksLikeGroupInventoryQuestion(question)
    || dataGroupContentType(selectedGroup) !== 'structured'
  )
  const fileLines = selectedGroup.files?.length
    ? selectedGroup.files.slice(0, 100).map(file => `- ${file.name || 'Unnamed file'} (${file.type || 'file'}${file.glueTableHint ? `, table: ${file.glueTableHint}` : ''})`).join('\n')
    : ''
  const profile = selectedGroup.groupProfile || {}
  const profileText = profile.kind
    ? `Group setup profile: kind=${profile.kind}; confidence=${profile.confidence || 'unknown'}; columns=${(profile.columns || []).slice(0, 60).join(', ') || 'unknown'}`
    : ''
  const factCounts = selectedGroup.structuredFacts?.counts || profile.factIndex || {}
  const factTypes = factCounts.types ? Object.keys(factCounts.types).join(', ') : ''
  const factText = factCounts.factSources || factCounts.sourceCount
    ? `Structured text facts: sources=${factCounts.factSources || factCounts.sourceCount || 0}; lookupKeys=${factCounts.lookupKeys || factCounts.lookupKeyCount || 0}; types=${factTypes || 'generic'}`
    : ''

  return [
    'Resolved project/group context from the UI selector.',
    `Project: ${selectedGroup.projectName || selectedGroup.projectId || 'Selected project'} (${selectedGroup.projectId || 'unknown'})`,
    `Group: ${selectedGroup.groupName}`,
    selectedGroup.tableHints?.length
      ? `Allowed Glue table hints:\n${selectedGroup.tableHints.slice(0, 40).map(table => `- ${table}`).join('\n')}`
      : selectedGroup.local
        ? 'Allowed Glue table hints: none available for this local browser group; answer from the selected group file inventory.'
        : 'Allowed Glue table hints: use only tables that resolve to this selected group.',
    includeFileInventory && fileLines
      ? `Available files for selected group:\n${fileLines}`
      : `Selected group file count: ${selectedGroup.fileCount || selectedGroup.files?.length || 0}`,
    profileText,
    factText,
    '',
    `User request:\n${question}`,
  ].filter(Boolean).join('\n')
}

/* ─── Main page ─────────────────────────────────────────────────────── */

export default function MCPChat() {
  const restoredDraftRef = useRef(readMcpChatDraft())
  const restoredServer = MCP_SERVERS.find(s => s.id === restoredDraftRef.current?.selectedServerId) || MCP_SERVERS[0]
  const [selectedServer, setSelectedServer] = useState(restoredServer)
  const [messages, setMessages] = useState(() => restoredDraftRef.current?.messages || [])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [activeSessionId, setActiveSessionId] = useState(restoredDraftRef.current?.activeSessionId || null)
  const [activeSessionTitle, setActiveSessionTitle] = useState(restoredDraftRef.current?.activeSessionTitle || null)
  const [dataGroups, setDataGroups] = useState([])
  const [dataGroupsLoading, setDataGroupsLoading] = useState(false)
  const [selectedDataProjectId, setSelectedDataProjectId] = useState(restoredDraftRef.current?.selectedDataProjectId || '')
  const [selectedDataGroupId, setSelectedDataGroupId] = useState(restoredDraftRef.current?.selectedDataGroupId || '')
  const bottomRef = useRef(null)
  const statusById = useAgentStatus()
  const {
    sessions, list: listSessions, loadMessages,
    addLocalSession, bumpLocalSession, deleteSession, clearActive, bulkDeleteByScope,
    loading: sessionsLoading,
  } = useConversations({ type: 'mcp' })

  // Decorate a registry entry with its live status bucket/label/chat-enabled.
  const decorate = (s) => ({ ...s, ...deriveStatus(statusById[s.id], s.placeholder) })
  const servers = MCP_SERVERS.map(decorate)
  const sel = decorate(selectedServer)
  const selectedDataGroup = dataGroups.find(group => group.id === selectedDataGroupId) || null
  const selectedDataContentType = selectedDataGroup
    ? dataGroupContentType(selectedDataGroup)
    : restoredDraftRef.current?.selectedDataContentType || ''
  const dataGroupChat = isDataGroupChatTarget(selectedServer.id)
  const dataProjectOptions = projectOptionsFromGroups(dataGroups)
  const filteredDataGroups = selectedDataProjectId
    ? dataGroups.filter(group => (group.projectId || DEFAULT_DATA_PROJECT_ID) === selectedDataProjectId)
    : dataGroups

  const introMessage = (s) => ({
    role: 'assistant',
    system: true,
    content: `Connected to **${s.name}** at \`${s.host}\`.\n\n${s.description}\n\n`
      + (s.placeholder
        ? 'This agent is not deployed yet — chat is disabled.'
        : `I have access to ${s.tools.length} tool${s.tools.length === 1 ? '' : 's'}. Ask me anything, or describe what you need.`),
    toolCalls: [],
    time: new Date().toLocaleTimeString(),
  })

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  useEffect(() => {
    writeMcpChatDraft({
      selectedServerId: selectedServer.id,
      activeSessionId,
      activeSessionTitle,
      selectedDataProjectId,
      selectedDataGroupId,
      selectedDataContentType,
      messages,
    })
  }, [selectedServer.id, activeSessionId, activeSessionTitle, selectedDataProjectId, selectedDataGroupId, selectedDataContentType, messages])

  // Fetch the user's session list once on mount.
  useEffect(() => {
    listSessions().catch(() => { })
  }, [listSessions])

  useEffect(() => {
    if (!dataGroupChat) return
    let cancelled = false
    const localGroups = readLocalDataGroupingGroups()
    setDataGroups(localGroups)
    setDataGroupsLoading(true)
    listDataGroupingProjects()
      .then(data => {
        if (cancelled) return
        const groups = mergeDataGroupOptions(data.groups || [], localGroups)
        const projects = projectOptionsFromGroups(groups)
        setDataGroups(groups)
        if (!selectedDataProjectId && projects[0]?.id) {
          setSelectedDataProjectId(projects.find(project => project.id === DEFAULT_DATA_PROJECT_ID)?.id || projects[0].id)
        }
        if (selectedDataGroupId && !groups.some(group => group.id === selectedDataGroupId)) {
          setSelectedDataGroupId('')
        }
      })
      .catch(() => {
        if (!cancelled) {
          setDataGroups(localGroups)
          const projects = projectOptionsFromGroups(localGroups)
          if (!selectedDataProjectId && projects[0]?.id) {
            setSelectedDataProjectId(projects.find(project => project.id === DEFAULT_DATA_PROJECT_ID)?.id || projects[0].id)
          }
          if (selectedDataGroupId && !localGroups.some(group => group.id === selectedDataGroupId)) {
            setSelectedDataGroupId('')
          }
        }
      })
      .finally(() => {
        if (!cancelled) setDataGroupsLoading(false)
      })
    return () => { cancelled = true }
  }, [selectedServer.id])

  useEffect(() => {
    if (!selectedDataGroupId) return
    const selected = dataGroups.find(group => group.id === selectedDataGroupId)
    if (selected && selectedDataProjectId && (selected.projectId || DEFAULT_DATA_PROJECT_ID) !== selectedDataProjectId) {
      setSelectedDataGroupId('')
    }
  }, [dataGroups, selectedDataProjectId, selectedDataGroupId])

  // Reset chat when the user picks a different server (only if no session is loaded).
  useEffect(() => {
    if (activeSessionId) return
    setMessages(prev => prev.length ? prev : [introMessage(selectedServer)])
  }, [selectedServer.id, activeSessionId])

  async function openSession(sessionId, title) {
    const data = await loadMessages(sessionId)
    if (!data) return
    setActiveSessionId(sessionId)
    setActiveSessionTitle(title || sessionId)
    setMessages((data.messages || []).map(m => ({
      role: m.role,
      content: m.content,
      toolCalls: [],  // memory doesn't store tool_calls today; show empty
      time: m.ts ? new Date(m.ts).toLocaleTimeString() : '',
    })))
  }

  function newChat() {
    setActiveSessionId(null)
    setActiveSessionTitle(null)
    // Re-trigger the server-intro effect by setting messages here directly.
    setMessages([introMessage(selectedServer)])
  }

  async function clearChat() {
    const sessionToDelete = activeSessionId
    clearMcpChatDraft()
    clearActive()
    setInput('')
    setLoading(false)
    setSelectedDataGroupId('')
    setActiveSessionId(null)
    setActiveSessionTitle(null)
    setMessages([introMessage(selectedServer)])
    if (sessionToDelete) {
      try {
        await deleteSession(sessionToDelete)
      } catch {
        // Local reset has already happened; the next session-list refresh can
        // reconcile any server-side delete hiccup.
      }
    }
  }

  async function send(text) {
    const q = text || input.trim()
    if (!q) return
    // Placeholder agents (ServiceNow, or any not-yet-READY runtime) can't chat.
    if (!sel.chat) return
    setInput('')

    const userMsg = { role: 'user', content: q, time: new Date().toLocaleTimeString() }
    setMessages(prev => [...prev, userMsg])

    const outOfScopeGroup = dataGroupChat
      ? findOutOfScopeGroup(q, selectedDataGroup, dataGroups)
      : null
    if (outOfScopeGroup) {
      setMessages(prev => [...prev, {
        role: 'assistant',
        system: true,
        content: `This chat is locked to **${selectedDataGroup.label || selectedDataGroup.groupName}**. The data you asked for appears to be in **${outOfScopeGroup.label || outOfScopeGroup.groupName}**, which is outside the selected group boundary.\n\nChange the Data group selector to **${outOfScopeGroup.label || outOfScopeGroup.groupName}** or switch back to **All data** to ask that question.`,
        toolCalls: [],
        time: new Date().toLocaleTimeString(),
      }])
      return
    }

    const outsideProjectGroup = dataGroupChat && !selectedDataGroup
      ? findGroupOutsideSelectedProject(q, selectedDataProjectId, dataGroups)
      : null
    if (outsideProjectGroup) {
      const projectName = dataProjectOptions.find(project => project.id === selectedDataProjectId)?.name || selectedDataProjectId || 'the selected project'
      setMessages(prev => [...prev, {
        role: 'assistant',
        system: true,
        content: `This chat is scoped to **${projectName}**, but **${outsideProjectGroup.groupName}** is published under **${outsideProjectGroup.projectName || outsideProjectGroup.projectId || 'another project'}**. Select that project/group, or publish the group inside **${projectName}** before querying it here.`,
        toolCalls: [],
        time: new Date().toLocaleTimeString(),
      }])
      return
    }

    if (dataGroupChat && selectedDataProjectId && !selectedDataGroup) {
      const projectName = dataProjectOptions.find(project => project.id === selectedDataProjectId)?.name || selectedDataProjectId || 'the selected project'
      setMessages(prev => [...prev, {
        role: 'assistant',
        system: true,
        content: `Select a data group inside **${projectName}** before running this query. This prevents Arbiter from inferring a same-named group from another project.`,
        toolCalls: [],
        time: new Date().toLocaleTimeString(),
      }])
      return
    }

    setLoading(true)

    // Generate a session_id the first time the user sends a message in this chat.
    // The master agent uses this to detect "new conversation" and write the DDB
    // row + title. Stays the same across the rest of this conversation.
    let sid = activeSessionId
    let isNew = false
    if (!sid) {
      sid = `sess-${crypto.randomUUID().replace(/-/g, '').slice(0, 12)}`
      setActiveSessionId(sid)
      const title = q.slice(0, 80)
      setActiveSessionTitle(title)
      // Optimistic sidebar entry; the real row is written by the master agent.
      addLocalSession({
        session_id: sid,
        title,
        chat_type: 'mcp',
        created_at: new Date().toISOString(),
        last_message_at: new Date().toISOString(),
        message_count: 0,
      })
      isNew = true
    }

    try {
      // Mixed groups normally use the master so document and CSV evidence can
      // be combined. Explicit table/join questions should go straight to the
      // structured runtime with the concrete group name; otherwise the master
      // can reduce the scope to the ambiguous phrase "selected group".
      const effectiveTarget = selectedDataGroup && isStructuredEvidenceQuestion(q)
        ? 'structured'
        : selectedServer.id
      const scopedPrompt = dataGroupChat && selectedDataGroup
        ? buildDataGroupScopedPrompt(q, selectedDataGroup)
        : dataGroupChat && selectedDataProjectId
          ? [
              'Resolved project context from the UI selector.',
              `Project: ${dataProjectOptions.find(project => project.id === selectedDataProjectId)?.name || selectedDataProjectId} (${selectedDataProjectId})`,
              'Group: not selected',
              '',
              `User request:\n${q}`,
            ].join('\n')
          : q
      const { reply } = await sendChat({
        prompt: scopedPrompt,
        session_id: sid,
        chat_type: 'mcp',
        target: effectiveTarget,
        // Older deployed APIs force every request carrying data_group to the
        // structured runtime. Keep that legacy field CSV-only; document/mixed
        // scope is already embedded in scopedPrompt and data_content_type.
        data_group: dataGroupChat && selectedDataGroup && effectiveTarget === 'structured'
          ? selectedDataGroup.groupName
          : '',
        data_project_id: dataGroupChat && selectedDataGroup
          ? selectedDataGroup.projectId
          : dataGroupChat
            ? selectedDataProjectId
            : '',
        data_project_name: dataGroupChat && selectedDataGroup
          ? selectedDataGroup.projectName
          : dataGroupChat
            ? dataProjectOptions.find(project => project.id === selectedDataProjectId)?.name || ''
            : '',
        data_group_id: dataGroupChat && selectedDataGroup
          ? selectedDataGroup.id
          : '',
        data_content_type: selectedDataContentType,
      })
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: reply,
        toolCalls: [],
        time: new Date().toLocaleTimeString(),
      }])
      bumpLocalSession(sid, 2)
    } catch (e) {
      setMessages(prev => [...prev, {
        role: 'assistant',
        system: true,
        content: `⚠️ Chat failed: ${e.message || e}`,
        toolCalls: [],
        time: new Date().toLocaleTimeString(),
      }])
    } finally {
      setLoading(false)
    }

    // Note: a brand-new session's title is set from the first prompt here AND
    // on the server. The agent's PutItem is idempotent on session_id, so both
    // sides agree.
    void isNew
  }

  const suggestions = sel.chat ? (SUGGESTED[selectedServer.id] || []) : []

  return (
    <div className="flex h-full overflow-hidden">
      {/* Left: Server list */}
      <div className="w-64 flex-shrink-0 border-r border-slate-200 flex flex-col bg-slate-50">
        <div className="p-3 border-b border-slate-200">
          <p className="text-xs font-bold text-slate-600 uppercase tracking-wider mb-1">MCP Servers</p>
          <p className="text-[10px] text-slate-500">Select a server to chat with it directly</p>
        </div>
        <div className="flex-1 overflow-y-auto p-2 space-y-1">
          {servers.map(srv => (
            <ServerListItem
              key={srv.id}
              server={srv}
              selected={selectedServer}
              onSelect={(s) => {
                setActiveSessionId(null)
                setActiveSessionTitle(null)
                setSelectedServer(s)
                if (s.id !== 'structured') setSelectedDataGroupId('')
                setMessages([introMessage(s)])
              }}
            />
          ))}

          {/* History — sessions loaded from /conversations */}
          <div className="pt-3 mt-3 border-t border-slate-200">
            <div className="flex items-center justify-between mb-1.5 px-1">
              <p className="text-[10px] font-bold text-slate-600 uppercase tracking-wider flex items-center gap-1">
                <MessageSquare size={10} /> Recent Conversations
              </p>
              <div className="flex items-center gap-2">
                <button
                  onClick={newChat}
                  title="Start a new chat"
                  className="text-[10px] text-indigo-600 hover:text-indigo-800 flex items-center gap-0.5"
                >
                  <Plus size={10} /> New
                </button>
                <ClearChatsButton
                  sessions={sessions}
                  onBulkDelete={bulkDeleteByScope}
                  onAfter={listSessions}
                  activeSessionId={activeSessionId}
                  onActiveDeleted={() => {
                    setActiveSessionId(null)
                    setActiveSessionTitle(null)
                    setMessages([introMessage(selectedServer)])
                  }}
                />
              </div>
            </div>
            {sessionsLoading && (
              <div className="text-[10px] text-slate-400 px-2 py-1 flex items-center gap-1">
                <Loader2 size={10} className="animate-spin" /> loading…
              </div>
            )}
            {!sessionsLoading && sessions.length === 0 && (
              <div className="text-[10px] text-slate-400 px-2 py-1 italic">No history yet</div>
            )}
            {sessions.map(s => (
              <button
                key={s.session_id}
                onClick={() => openSession(s.session_id, s.title)}
                className={`w-full text-left px-2 py-1.5 rounded text-xs hover:bg-slate-100 transition-colors ${activeSessionId === s.session_id ? 'bg-indigo-50 border border-indigo-200' : ''
                  }`}
              >
                <p className="font-medium text-slate-800 truncate">{s.title || s.session_id}</p>
                <p className="text-[10px] text-slate-500">
                  {s.last_message_at ? new Date(s.last_message_at).toLocaleString() : ''} · {s.message_count || 0} msgs
                </p>
              </button>
            ))}
          </div>
        </div>
        <div className="p-3 border-t border-slate-200">
          <div className="flex items-center gap-1.5 text-[10px] text-slate-500">
            <Shield size={10} />
            <span>Admin access only · all queries logged</span>
          </div>
        </div>
      </div>

      {/* Right: Chat panel */}
      <div className="flex-1 flex flex-col overflow-hidden bg-white">
        {/* Server header */}
        <div className="px-5 py-3 border-b border-slate-200 flex items-center gap-3 bg-white">
          <div className={`w-2.5 h-2.5 rounded-full flex-shrink-0 ${DOT_CLASS[sel.bucket] || 'bg-slate-400'}`} />
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              <p className="text-sm font-semibold text-slate-900">{sel.name}</p>
              <span className={`text-[10px] font-mono ${TEXT_CLASS[sel.bucket] || 'text-slate-500'}`}>{sel.label}</span>
            </div>
            <p className="text-[10px] text-slate-500 font-mono">{sel.host} · {sel.tools.length} tool{sel.tools.length === 1 ? '' : 's'}</p>
          </div>
          {activeSessionId && (
            <span className="flex items-center gap-1 text-[10px] bg-indigo-50 border border-indigo-200 text-indigo-700 px-1.5 py-0.5 rounded-full">
              <MessageSquare size={9} /> History: {activeSessionTitle}
            </span>
          )}
          {dataGroupChat && selectedDataGroup && (
            <span className="flex max-w-[320px] items-center gap-1 rounded-full border border-emerald-200 bg-emerald-50 px-1.5 py-0.5 text-[10px] text-emerald-700">
              <Database size={9} /> <span className="truncate">{selectedDataGroup.label}</span>
            </span>
          )}
          {dataGroupChat && (
            <button
              onClick={clearChat}
              title="Clear chat history and reset this Data Group session"
              className="inline-flex items-center gap-1 text-[10px] border border-slate-200 text-slate-600 hover:text-slate-900 hover:bg-slate-50 px-2 py-1 rounded-lg transition-colors"
            >
              <RotateCcw size={11} /> Clear Chat
            </button>
          )}
        </div>

        {!sel.chat && (
          <div className="px-5 py-2 bg-amber-50 border-b border-amber-200 flex items-center gap-2">
            <AlertTriangle size={12} className="text-amber-600 flex-shrink-0" />
            <p className="text-xs text-amber-800">
              {selectedServer.placeholder
                ? 'Placeholder — this specialist agent has not been deployed yet. Chat is disabled.'
                : `Agent status: ${sel.label}. Chat is disabled until the runtime is READY.`}
            </p>
          </div>
        )}

        {/* Messages */}
        <div className="flex-1 overflow-y-auto p-5 space-y-4">
          {messages.map((msg, i) => {
            const isLastAssistant =
              msg.role === 'assistant' && i === messages.length - 1 && !loading
            const detected = isLastAssistant && !msg.ticketCreated
              ? detectProblem({ messages: messages.slice(0, i + 1), sessionId: activeSessionId, sessionTitle: activeSessionTitle })
              : null
            return (
              <div key={i}>
                <Message msg={msg} />
                {detected?.hasProblem && (
                  <div className="ml-10 mt-1 flex items-start gap-2">
                    <CreateTicketButton detected={detected} />
                    <button
                      onClick={() => downloadChatPdf({
                        title: reportTitleFromMessage(msg),
                        content: msg.content,
                      })}
                      title="Download this response as a PDF report"
                      className="mt-2 inline-flex items-center gap-2 border border-slate-200 text-slate-600 hover:text-indigo-700 hover:border-indigo-200 hover:bg-indigo-50 px-3 py-1.5 rounded-lg text-xs font-semibold transition-colors"
                    >
                      <Download size={13} /> PDF
                    </button>
                    {selectedServer.id === 'structured' && (
                      <button
                        onClick={clearChat}
                        title="Clear chat history and reset this Structured Data session"
                        className="mt-2 inline-flex items-center gap-2 border border-slate-200 text-slate-600 hover:text-slate-900 hover:bg-slate-50 px-3 py-1.5 rounded-lg text-xs font-semibold transition-colors"
                      >
                        <RotateCcw size={13} /> Clear Chat
                      </button>
                    )}
                  </div>
                )}
              </div>
            )
          })}

          {loading && (
            <div className="flex gap-3">
              <div className="w-7 h-7 rounded-lg bg-slate-50 border border-slate-200 flex items-center justify-center flex-shrink-0">
                <Terminal size={13} className="text-indigo-600" />
              </div>
              <div className="flex items-center gap-2 text-sm text-slate-500">
                <Loader2 size={13} className="animate-spin" />
                <span>Calling MCP tools…</span>
              </div>
            </div>
          )}

          <div ref={bottomRef} />
        </div>

        {/* Suggestions */}
        {suggestions.length > 0 && (messages.length <= 1 || selectedServer.id === 'structured') && (
          <div className="px-5 pb-2 flex flex-wrap gap-1.5">
            {suggestions.map(s => {
              const label = typeof s === 'string' ? s : s.label
              const prompt = typeof s === 'string' ? s : s.prompt
              return (
              <button
                key={label}
                onClick={() => send(prompt)}
                className="text-xs bg-slate-50 hover:bg-slate-100 border border-slate-200 text-slate-700 px-3 py-1.5 rounded-lg transition-colors"
              >
                {label}
              </button>
              )
            })}
          </div>
        )}

        {/* Input */}
        <div className="p-4 border-t border-slate-200 bg-white">
          <div className="flex gap-2">
            {dataGroupChat && (
              <>
                <label className="relative flex min-w-[190px] max-w-[260px] flex-1 items-center gap-2 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2">
                  <Database size={14} className="shrink-0 text-indigo-600" />
                  <div className="min-w-0 flex-1">
                    <p className="text-[9px] font-bold uppercase tracking-wider text-slate-400">Project</p>
                    <select
                      value={selectedDataProjectId}
                      onChange={(event) => {
                        setSelectedDataProjectId(event.target.value)
                        setSelectedDataGroupId('')
                      }}
                      disabled={loading || dataGroupsLoading}
                      className="mt-0.5 w-full bg-transparent text-xs font-semibold text-slate-800 outline-none disabled:text-slate-400"
                      title={dataProjectOptions.find(project => project.id === selectedDataProjectId)?.name || 'Project'}
                    >
                      {dataProjectOptions.map(project => (
                        <option key={project.id} value={project.id}>{project.name}</option>
                      ))}
                    </select>
                  </div>
                </label>
                <label className="relative flex min-w-[260px] max-w-[360px] flex-1 items-center gap-2 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2">
                  <Database size={14} className="shrink-0 text-indigo-600" />
                  <div className="min-w-0 flex-1">
                    <p className="text-[9px] font-bold uppercase tracking-wider text-slate-400">Data group</p>
                    <select
                      value={selectedDataGroupId}
                      onChange={(event) => setSelectedDataGroupId(event.target.value)}
                      disabled={loading || dataGroupsLoading}
                      className="mt-0.5 w-full bg-transparent text-xs font-semibold text-slate-800 outline-none disabled:text-slate-400"
                      title={selectedDataGroup?.label || 'All data'}
                    >
                      <option value="">{dataGroupsLoading ? 'Loading groups...' : 'Select group'}</option>
                      {filteredDataGroups.map(group => (
                        <option key={group.id} value={group.id}>
                          {group.groupName}{group.local ? ' · local' : ` · ${group.tableCount || 0} tables`}
                        </option>
                      ))}
                    </select>
                  </div>
                </label>
              </>
            )}
            <input
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && !e.shiftKey && send()}
              placeholder={sel.chat ? `Query ${sel.name}…` : 'Agent unavailable — chat disabled'}
              className="input flex-1"
              disabled={loading || !sel.chat}
            />
            <button
              onClick={() => send()}
              disabled={loading || !input.trim() || !sel.chat}
              className="btn-primary px-3 flex items-center gap-1.5"
            >
              <Send size={14} />
            </button>
          </div>
          <p className="text-[10px] text-slate-400 mt-1.5">
            {CHAT_URL ? 'Live agent connection' : 'Mock mode — responses are simulated'} · All queries are audit-logged · Admin only
          </p>
        </div>
      </div>
    </div>
  )
}
