// AgentsCatalog — the specialist fleet segregated into assist groups for the
// Smart Rabbit page. Each agent's `id` is the routing `target` sent to
// sendChat(); api_handler resolves it to that specialist's AgentCore runtime
// ARN (SPECIALIST_RUNTIME_ARNS). Names mirror MCP_SERVERS in pages/MCPChat.jsx
// and _AGENT_DISPLAY_NAMES in api_handler.py.

export const AGENT_CATALOG = [
  {
    id: 'it_assist_admin',
    name: 'IT Assist – Admin',
    description: 'Policy, network, and cloud-posture specialists for IT administrators.',
    agents: [
      { id: 'sharepoint', name: 'SharePoint Specialist', description: 'Enterprise policy documents from the SharePoint-backed knowledge base.' },
      { id: 'zscaler', name: 'Zscaler ZIA Specialist', description: 'Zscaler Internet Access URL allowlists and category policy.' },
      { id: 'awsconfig', name: 'AWS Resource & Posture Specialist', description: 'Read-only AWS inventory, network/exposure, and Config compliance.' },
      { id: 'paloalto', name: 'Palo Alto NGFW Specialist', description: 'Perimeter firewall rules, App-ID enforcement, and egress controls.' },
    ],
  },
  {
    id: 'it_assist_work',
    name: 'IT Assist – Work',
    description: 'ITSM and issue-tracking specialists for day-to-day IT work.',
    agents: [
      { id: 'servicenow', name: 'ServiceNow Specialist', description: 'CMDB (incl. list CIs by class), Incident, Problem, Change, and Asset Management over the ServiceNow API.' },
      { id: 'jira', name: 'JIRA Specialist', description: 'Reads and creates Jira issues, projects, and sprints via MCP.' },
    ],
  },
  {
    id: 'employee_assist',
    name: 'Employee Assist',
    description: 'Workplace policy help for every employee.',
    agents: [
      { id: 'hr', name: 'HR Specialist', description: 'HR policy RAG: leave/PTO, benefits, compensation, conduct, payroll.' },
    ],
  },
  {
    id: 'data_assist',
    name: 'Data Assist',
    description: 'Structured-data and analytics specialists.',
    agents: [
      { id: 'structured', name: 'Structured Data Specialist', description: 'Project-centric questions over grouped files and Glue-catalogued datasets.' },
      { id: 'sales', name: 'Sales Specialist', description: 'Hybrid RAG + read-only SQL over sales data (totals, top-N, trends).' },
    ],
  },
  {
    id: 'insurance_assist',
    name: 'Insurance Assist',
    description: 'Insurance claims and fraud advisory specialists.',
    agents: [
      { id: 'claim', name: 'Claim Specialist', description: 'Claims intake and lifecycle guidance — checklists, coverage concepts, claimant comms.' },
      { id: 'fraud', name: 'Fraud Specialist', description: 'Fraud red flags, SIU referral criteria, and investigation checklists.' },
    ],
  },
  {
    id: 'oncall_assist',
    name: 'OnCall Assist',
    description: 'Incident triage and debugging support for on-call engineers.',
    agents: [
      { id: 'debug', name: 'Debug Specialist', description: 'Stack-trace/log interpretation, triage paths, and runbook next steps.' },
    ],
  },
]

/** Resolve an agent id to { agent, group } across the catalog, or null. */
export function findAgent(agentId) {
  for (const group of AGENT_CATALOG) {
    const agent = group.agents.find((a) => a.id === agentId)
    if (agent) return { agent, group }
  }
  return null
}
