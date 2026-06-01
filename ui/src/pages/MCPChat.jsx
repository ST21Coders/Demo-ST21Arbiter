import { useState, useRef, useEffect } from 'react'
import {
  Terminal, Send, Loader2, ChevronRight, Server, Zap,
  CheckCircle, AlertTriangle, Activity, Clock, Copy,
  Shield, Wifi, WifiOff, MessageSquare, Plus,
} from 'lucide-react'
import { CHAT_URL } from '../config'
import { useConversations, sendChat } from '../hooks/useApi'
import { detectProblem } from '../detectProblem'
import CreateTicketButton from '../components/CreateTicketButton'

/* ─── MCP server registry (mirrors HeatMap data) ─────────────────────── */

const MCP_SERVERS = [
  {
    id: 'policy-scanner',
    name: 'Policy Scanner MCP',
    host: 'mcp-policy:8001',
    status: 'ONLINE',
    version: 'v2.1.0',
    description: 'Scans and queries policy documents, detects version conflicts, retrieves ownership data.',
    tools: [
      { name: 'search_policies',    desc: 'Semantic search across policy knowledge base' },
      { name: 'compare_versions',   desc: 'Diff two versions of the same policy document' },
      { name: 'list_owners',        desc: 'Return owner metadata for a given policy ID' },
      { name: 'get_policy_section', desc: 'Retrieve a specific section by policy ID and section number' },
      { name: 'find_conflicts',     desc: 'Run conflict detection against a specific policy' },
      { name: 'validate_policy',    desc: 'Check policy against compliance framework rules' },
      { name: 'get_history',        desc: 'Return version history for a policy document' },
      { name: 'get_metadata',       desc: 'Return classification, owner, effective date metadata' },
    ],
    latencyMs: 85,
    uptime: '99.8%',
    reqToday: 1247,
  },
  {
    id: 'conflict-detector',
    name: 'Conflict Detector MCP',
    host: 'mcp-conflict:8002',
    status: 'ONLINE',
    version: 'v1.8.3',
    description: 'Cross-domain conflict detection engine. Correlates policies with technical configurations.',
    tools: [
      { name: 'detect_conflicts',  desc: 'Run cross-domain conflict scan on a resource or policy' },
      { name: 'score_severity',    desc: 'Score a conflict and assign CRITICAL/HIGH/MEDIUM/LOW' },
      { name: 'find_related',      desc: 'Find conflicts related to a given conflict ID' },
      { name: 'classify_type',     desc: 'Classify conflict as CROSS_DOMAIN or INTRA_DOCUMENT' },
      { name: 'get_history',       desc: 'Return historical conflict detections for a resource' },
    ],
    latencyMs: 120,
    uptime: '99.2%',
    reqToday: 892,
  },
  {
    id: 'approval-engine',
    name: 'Approval Engine MCP',
    host: 'mcp-approval:8003',
    status: 'ONLINE',
    version: 'v3.0.1',
    description: 'Manages approval workflows, chains, and notification routing based on environment and severity.',
    tools: [
      { name: 'get_approvers',       desc: 'Resolve approval chain for env × severity matrix' },
      { name: 'check_matrix',        desc: 'Return full approval matrix for reference' },
      { name: 'create_workflow',     desc: 'Create an approval workflow for a change request' },
      { name: 'notify_approvers',    desc: 'Send notifications to pending approvers' },
      { name: 'escalate',            desc: 'Escalate a stalled approval to next authority' },
      { name: 'get_status',          desc: 'Return current approval status for a CR ID' },
    ],
    latencyMs: 65,
    uptime: '100%',
    reqToday: 341,
  },
  {
    id: 'servicenow-mcp',
    name: 'ServiceNow MCP',
    host: 'mcp-snow:8004',
    status: 'DEGRADED',
    version: 'v1.2.0',
    description: 'ITSM integration for creating INC, CHG, and RITM tickets. Currently experiencing high latency.',
    tools: [
      { name: 'create_incident',       desc: 'Create INC ticket for policy violation or security event' },
      { name: 'create_change_request', desc: 'Create CHG ticket with approvers pre-populated' },
      { name: 'get_ticket',            desc: 'Retrieve ticket status and details by ticket number' },
      { name: 'update_ticket',         desc: 'Append notes or update status on an existing ticket' },
    ],
    latencyMs: 850,
    uptime: '87.4%',
    reqToday: 156,
    warning: 'High latency: avg 850ms vs 200ms SLA. API gateway may be throttling.',
  },
  {
    id: 'zscaler-mcp',
    name: 'Zscaler ZIA MCP',
    host: 'mcp-zscaler:8005',
    status: 'ONLINE',
    version: 'v1.5.2',
    description: 'Reads and (with approval) updates Zscaler Internet Access URL categorization rules and policies.',
    tools: [
      { name: 'get_url_category',  desc: 'Return current category and action for a URL or domain' },
      { name: 'update_policy',     desc: 'Update URL policy rule (requires PROD approval chain)' },
      { name: 'get_blocked_list',  desc: 'Return all currently blocked URL categories' },
    ],
    latencyMs: 110,
    uptime: '99.5%',
    reqToday: 423,
  },
  {
    id: 'aws-config-mcp',
    name: 'AWS Config MCP',
    host: 'mcp-aws:8006',
    status: 'ONLINE',
    version: 'v2.0.4',
    description: 'Reads AWS infrastructure configuration snapshots. Security groups, S3, IAM, VPC, compliance drift.',
    tools: [
      { name: 'list_security_groups', desc: 'List security groups with open inbound rules' },
      { name: 'get_s3_config',        desc: 'Return S3 bucket config: replication, ACL, encryption' },
      { name: 'get_iam_policies',     desc: 'Return IAM policies attached to a principal or resource' },
      { name: 'get_vpc_config',       desc: 'Return VPC peering, CIDR, and routing configuration' },
      { name: 'check_drift',          desc: 'Check for config drift against last known-good state' },
      { name: 'list_compliance',      desc: 'Return AWS Config compliance rule results' },
      { name: 'get_resource_config',  desc: 'Return full config snapshot for a specific resource ARN' },
    ],
    latencyMs: 95,
    uptime: '99.9%',
    reqToday: 678,
  },
  {
    id: 'bedrock-kb',
    name: 'Bedrock KB MCP',
    host: 'mcp-kb:8007',
    status: 'ONLINE',
    version: 'v1.1.0',
    description: 'Knowledge base retrieval and document indexing. Backed by OpenSearch Serverless with Titan Embed Text v2.',
    tools: [
      { name: 'semantic_search',   desc: 'Perform semantic similarity search across all indexed documents' },
      { name: 'get_chunk',         desc: 'Retrieve a specific chunk by ID with surrounding context' },
      { name: 'index_document',    desc: 'Index a new document into the knowledge base' },
      { name: 'get_embeddings',    desc: 'Return embedding vector for a given text string' },
    ],
    latencyMs: 45,
    uptime: '99.7%',
    reqToday: 2341,
  },
]

/* ─── Mock MCP responses ─────────────────────────────────────────────── */

function mockMCPResponse(serverId, message) {
  const msg = message.toLowerCase()
  const server = MCP_SERVERS.find(s => s.id === serverId)

  if (serverId === 'policy-scanner') {
    if (msg.includes('conflict') || msg.includes('scan')) {
      return {
        text: `**Policy Scanner** executed \`find_conflicts\` across all indexed MIG policy documents.\n\n**Results (5 active conflicts):**\n- \`MIG-POL-001-CS01 §2.1\` vs \`ZIA-URLCAT-CLOUD-BLK-004\` — dropbox.com approved, BLOCKED in Zscaler (**HIGH** · UC01)\n- \`MIG-POL-001-RA01 §3.1\` vs \`ZIA-URLCAT-REMOTE-BLK-007\` — TeamViewer/AnyDesk approved, BLOCKED (**HIGH** · UC02)\n- \`MIG-POL-002-SSL01 §2.2\` + PCI DSS 4.0 r1.3 — SSL inspection bypassed for financial domains (**CRITICAL** · UC04)\n- \`MIG-POL-002-MFA01 §2.1\` — MFA enforced only for admins, policy requires ALL users (**CRITICAL** · UC05)\n- \`MIG-POL-004-WAF01 §2.1\` — Production ALB exposed without WAF layer (**CRITICAL** · UC07)\n\n**Tool calls made:** \`search_policies\` → \`find_conflicts\` → \`score_severity\`\n\n\`\`\`json\n{\n  "conflicts_found": 12,\n  "critical": 5,\n  "high": 4,\n  "medium": 3,\n  "scan_duration_ms": 412,\n  "documents_scanned": 5\n}\n\`\`\``,
        toolCalls: ['search_policies', 'find_conflicts', 'score_severity'],
      }
    }
    if (msg.includes('owner') || msg.includes('who owns')) {
      return {
        text: `**Policy Scanner** called \`list_owners\` for all MIG policies.\n\n| Policy ID | Owner Team | Team Lead | Department |\n|---|---|---|---|\n| MIG-POL-001 | Cloud & Endpoint Security | Rachel Kim | CISO Office |\n| MIG-POL-002 | Network Security | David Torres | Infrastructure |\n| MIG-POL-003 | Data Governance | Sandra Patel | Legal & Compliance |\n| MIG-POL-004 | Cloud Architecture | James Wu | AWS CoE |\n| MIG-POL-005 | Zero Trust & Identity | Maria Santos | Identity & Access |\n\n**Tool calls made:** \`list_owners\` → \`get_metadata\``,
        toolCalls: ['list_owners', 'get_metadata'],
      }
    }
    if (msg.includes('version') || msg.includes('compare') || msg.includes('mfa') || msg.includes('pol-002')) {
      return {
        text: `**Policy Scanner** ran \`compare_versions\` on MIG-POL-002 (Network & Access Controls).\n\n**Diff summary (v1.2 → v2.0):**\n- \`MIG-POL-002-MFA01 §2.1\` — scope changed from **admins only** → **ALL employees** (breaking change)\n- \`MIG-POL-002-IOT01 §3.2\` — IoT enforcement changed from **monitor-only** → **active blocking**\n- \`MIG-POL-002-SSL01 §2.2\` — PCI DSS 4.0 alignment added for financial domains\n\n⚠️ **Zscaler and Azure AD configurations have NOT been updated** to reflect v2.0. Current enforce is still v1.2 behavior — creating 3 active conflicts.\n\n**Tool calls made:** \`compare_versions\` → \`get_history\` → \`find_conflicts\``,
        toolCalls: ['compare_versions', 'get_history', 'find_conflicts'],
      }
    }
    if (msg.includes('mig-pol-001') || msg.includes('cloud') || msg.includes('collaboration')) {
      return {
        text: `**Policy Scanner** retrieved \`MIG-POL-001\` — Cloud, Endpoint & Collaboration.\n\n**Sections:**\n- \`MIG-POL-001-CS01 §2.1\` — Approved cloud storage: Dropbox, Box, OneDrive (corporate accounts only)\n- \`MIG-POL-001-RA01 §3.1\` — Remote access tools: TeamViewer, AnyDesk (IT-managed endpoints only)\n- \`MIG-POL-001-WB01 §4.1\` — All major browsers approved; no browser-level blocking\n- \`MIG-POL-001-SM01 §5.1\` — Social media: blanket block, but Marketing & PR departments have explicit exemption\n\n**Effective date:** 2025-01-15 · **Review cycle:** Annual · **Owner:** CISO Office\n\n**Tool calls made:** \`get_policy_section\` → \`get_metadata\``,
        toolCalls: ['get_policy_section', 'get_metadata'],
      }
    }
  }

  if (serverId === 'conflict-detector') {
    if (msg.includes('detect') || msg.includes('check') || msg.includes('run') || msg.includes('all')) {
      return {
        text: `**Conflict Detector** ran a full cross-domain scan against 5 MIG policy documents.\n\n**Conflicts detected (12 total):**\n\n1. 🔴 **CRITICAL** (UC04) — SSL inspection bypassed for 47 financial domains. MIG-POL-002-SSL01 §2.2 + PCI DSS 4.0 r1.3 violation.\n2. 🔴 **CRITICAL** (UC05) — MFA enforced admins only; MIG-POL-002-MFA01 §2.1 requires ALL 8,400 employees.\n3. 🔴 **CRITICAL** (UC07) — Production ALB \`mig-prod-alb-claims-01\` exposed without WAF. MIG-POL-004-WAF01 §2.1.\n4. 🔴 **CRITICAL** (UC08) — Dev→prod VPC peering active 61 days. MIG-POL-004-SEG01 §2.3. PCI DSS 4.0 r1.3.2.\n5. 🔴 **CRITICAL** (UC09) — Claims S3 replicating to eu-west-1. MIG-POL-003-DR01 §3.1 + NAIC MDL-668 violation.\n6. 🟠 **HIGH** (UC01) — dropbox.com blocked in Zscaler; MIG-POL-001-CS01 §2.1 explicitly approves it.\n7. 🟠 **HIGH** (UC02) — TeamViewer/AnyDesk blocked; MIG-POL-001-RA01 §3.1 approves for managed endpoints.\n8. 🟠 **HIGH** (UC06) — IoT in monitor-only mode; MIG-POL-002-IOT01 §3.2 requires active blocking.\n9. 🟠 **HIGH** (UC10) — DLP rule blocking authorized actuarial model transfers. MIG-POL-003-DT01 §2.3.\n10. 🟡 **MEDIUM** (UC03) — Firefox blocked; MIG-POL-001-WB01 §4.1 approves all major browsers.\n11. 🟡 **MEDIUM** (UC11) — ZTNA geo-restriction blocks 6 of 8 MIG-POL-005-GEO01 approved vendor countries.\n12. 🟡 **MEDIUM** (UC12) — Social media blanket block ignores Marketing/PR exemption in MIG-POL-001-SM01 §5.1.\n\n**Tool calls made:** \`detect_conflicts\` → \`score_severity\` → \`classify_type\`\n\n\`\`\`json\n{"critical": 5, "high": 4, "medium": 3, "low": 0, "scan_ms": 2104}\n\`\`\``,
        toolCalls: ['detect_conflicts', 'score_severity', 'classify_type'],
      }
    }
    if (msg.includes('vpc') || msg.includes('peering') || msg.includes('uc08') || msg.includes('seg')) {
      return {
        text: `**Conflict Detector** ran \`detect_conflicts\` for UC08 — Dev-to-Prod VPC Peering.\n\n**Finding: ARBITER-UC08** (CRITICAL)\n- Security group \`sg-mig-prod-peer-dev-001\` allows ALL traffic from \`10.50.0.0/16\` (dev VPC) into production\n- Active for **61 days** — introduced during sprint velocity push, marked TEMP, never removed\n- **Violates:** MIG-POL-004-SEG01 §2.3 (production isolation) + PCI DSS 4.0 r1.3.2\n- **ARBITER recommendation:** Remove peering immediately + submit CHG via ServiceNow → CISO + VPE approval required\n\n**Tool calls made:** \`detect_conflicts\` → \`score_severity\` → \`find_related\``,
        toolCalls: ['detect_conflicts', 'score_severity', 'find_related'],
      }
    }
  }

  if (serverId === 'approval-engine') {
    if (msg.includes('approval') || msg.includes('who') || msg.includes('chain') || msg.includes('prod') || msg.includes('critical')) {
      return {
        text: `**Approval Engine** called \`get_approvers\` for **PROD environment, CRITICAL severity**.\n\n**Required approval chain (MIG standard):**\n\n| # | Role | Contact | Type |\n|---|---|---|---|\n| 1 | CISO | ciso@meridianinsurance.com | Approval required |\n| 2 | VP Engineering | vpe@meridianinsurance.com | Approval required |\n| 3 | Legal/Compliance | legal@meridianinsurance.com | Required for regulatory findings |\n| 4 | Change Advisory Board | cab@meridianinsurance.com | Notification only |\n\n**Cross-team rule:** If requesting team ≠ owning team, owning Team Lead automatically added to chain.\n\n**Estimated SLA:** 4–8 business hours for CISO-level approvals. PCI DSS findings require Legal sign-off before implementation.\n\n**Tool calls made:** \`get_approvers\` → \`check_matrix\``,
        toolCalls: ['get_approvers', 'check_matrix'],
      }
    }
    if (msg.includes('escalat') || msg.includes('stuck') || msg.includes('stall') || msg.includes('pending')) {
      return {
        text: `**Approval Engine** triggered \`escalate\` for CR-MIG-20260519-UC08.\n\nApproval has been pending >6 hours from CISO (ciso@meridianinsurance.com). Escalation path:\n1. Auto-reminder sent to ciso@meridianinsurance.com\n2. Alert sent to vpe@meridianinsurance.com (pending notification)\n3. If no response in 2h → Slack #sec-escalation-mig pinged\n4. CAB notified of SLA breach risk\n\n**Status:** Escalation dispatched. CR-MIG-20260519-UC08 now flagged priority in CAB queue.\n\n**Tool calls made:** \`get_status\` → \`escalate\` → \`notify_approvers\``,
        toolCalls: ['get_status', 'escalate', 'notify_approvers'],
      }
    }
    if (msg.includes('matrix') || msg.includes('all approval')) {
      return {
        text: `**Approval Engine** returned the full MIG approval matrix.\n\n| Environment | Severity | Required Approvers |\n|---|---|---|\n| PROD | CRITICAL | CISO + VPE + Legal |\n| PROD | HIGH | CISO + VPE |\n| PROD | MEDIUM | VPE + Team Lead |\n| STG | CRITICAL | VPE + Team Lead |\n| STG | HIGH/MEDIUM | Team Lead |\n| DEV | Any | Team Lead |\n\n**Regulatory override:** NAIC MDL-668 and PCI DSS findings always require Legal regardless of environment.\n\n**Tool calls made:** \`check_matrix\``,
        toolCalls: ['check_matrix'],
      }
    }
  }

  if (serverId === 'aws-config-mcp') {
    if (msg.includes('security group') || msg.includes('sg-') || msg.includes('vpc') || msg.includes('peering')) {
      return {
        text: `**AWS Config MCP** called \`list_security_groups\` and \`get_resource_config\`.\n\n**Finding: sg-mig-prod-peer-dev-001 (UC08)**\n\`\`\`json\n{\n  "GroupId": "sg-mig-prod-peer-dev-001",\n  "GroupName": "prod-dev-peering",\n  "VpcId": "vpc-mig-prod-001a2b3c4d",\n  "InboundRules": [\n    {\n      "Protocol": "ALL",\n      "FromPort": -1,\n      "ToPort": -1,\n      "CidrBlock": "10.50.0.0/16",\n      "Description": "VPC peering to dev - TEMP"\n    }\n  ],\n  "CreatedDaysAgo": 61,\n  "ComplianceStatus": "NON_COMPLIANT",\n  "PolicyViolations": ["MIG-POL-004-SEG01 §2.3", "PCI DSS 4.0 r1.3.2"]\n}\n\`\`\`\n\n**Tool calls made:** \`list_security_groups\` → \`get_resource_config\` → \`list_compliance\``,
        toolCalls: ['list_security_groups', 'get_resource_config', 'list_compliance'],
      }
    }
    if (msg.includes('s3') || msg.includes('bucket') || msg.includes('replication') || msg.includes('claims')) {
      return {
        text: `**AWS Config MCP** queried S3 bucket configuration (UC09).\n\n\`\`\`json\n{\n  "BucketName": "mig-prod-claims-data-primary",\n  "Region": "us-east-1",\n  "Replication": {\n    "Status": "ENABLED",\n    "DestinationBucket": "mig-claims-backup-eu",\n    "DestinationRegion": "eu-west-1"\n  },\n  "Classification": "TIER_1_PII",\n  "ActiveDays": 134,\n  "LegalNotified": false,\n  "PolicyViolations": ["MIG-POL-003-DR01 §3.1", "NAIC MDL-668 §4.2"]\n}\n\`\`\`\n\n🔴 **CRITICAL**: Replication to eu-west-1 violates MIG-POL-003-DR01 §3.1 (US-only storage for PII claims data) and NAIC MDL-668 §4.2. Legal has NOT been notified — 134 days of non-compliance.\n\n**Tool calls made:** \`get_s3_config\` → \`check_drift\` → \`list_compliance\``,
        toolCalls: ['get_s3_config', 'check_drift', 'list_compliance'],
      }
    }
    if (msg.includes('alb') || msg.includes('waf') || msg.includes('load balancer') || msg.includes('uc07')) {
      return {
        text: `**AWS Config MCP** checked WAF configuration for production ALBs (UC07).\n\n\`\`\`json\n{\n  "ResourceId": "mig-prod-alb-claims-01",\n  "ResourceType": "AWS::ElasticLoadBalancingV2::LoadBalancer",\n  "WAFAssociation": null,\n  "PubliclyAccessible": true,\n  "Scheme": "internet-facing",\n  "ComplianceStatus": "NON_COMPLIANT",\n  "PolicyViolations": ["MIG-POL-004-WAF01 §2.1"]\n}\n\`\`\`\n\n🔴 **CRITICAL**: Production claims ALB is internet-facing with no WAF association. MIG-POL-004-WAF01 §2.1 requires WAF on all internet-facing production resources.\n\n**Remediation:** Associate \`mig-waf-acl-prod-standard\` with ALB. PROD CRITICAL approval chain required.\n\n**Tool calls made:** \`get_resource_config\` → \`check_drift\` → \`list_compliance\``,
        toolCalls: ['get_resource_config', 'check_drift', 'list_compliance'],
      }
    }
    if (msg.includes('iam') || msg.includes('drift') || msg.includes('compliance')) {
      return {
        text: `**AWS Config MCP** ran \`check_drift\` across production resources.\n\n**Compliance summary:**\n\n| Rule | Status | Non-Compliant Resources |\n|---|---|---|\n| MIG-WAF-Required | NON_COMPLIANT | 1 (mig-prod-alb-claims-01) |\n| MIG-S3-US-Only | NON_COMPLIANT | 1 (mig-prod-claims-data-primary) |\n| MIG-VPC-Isolation | NON_COMPLIANT | 1 (sg-mig-prod-peer-dev-001) |\n| MIG-Encryption-At-Rest | COMPLIANT | 0 |\n| MIG-MFA-Root | COMPLIANT | 0 |\n\n**Tool calls made:** \`check_drift\` → \`list_compliance\``,
        toolCalls: ['check_drift', 'list_compliance'],
      }
    }
  }

  if (serverId === 'zscaler-mcp') {
    if (msg.includes('dropbox') || msg.includes('uc01')) {
      return {
        text: `**Zscaler ZIA MCP** called \`get_url_category\` for dropbox.com (UC01).\n\n\`\`\`json\n{\n  "url": "dropbox.com",\n  "category": "Cloud Storage - Blocked",\n  "policy_rule": "ZIA-URLCAT-CLOUD-BLK-004",\n  "action": "BLOCK",\n  "created": "2025-11-12",\n  "modified_by": "netops-automation@meridianinsurance.com"\n}\n\`\`\`\n\n⚠️ **Conflict**: dropbox.com is explicitly **approved** in MIG-POL-001-CS01 §2.1 (cloud storage whitelist) but is **BLOCKED** by Zscaler rule ZIA-URLCAT-CLOUD-BLK-004. Approximately **2,800 employees** affected.\n\nTo remediate: submit PROD CHG to remove dropbox.com from ZIA-URLCAT-CLOUD-BLK-004 and add to corporate-approved exception list. Requires CISO + VPE approval.\n\n**Tool calls made:** \`get_url_category\` → \`get_blocked_list\``,
        toolCalls: ['get_url_category', 'get_blocked_list'],
      }
    }
    if (msg.includes('teamviewer') || msg.includes('anydesk') || msg.includes('uc02') || msg.includes('remote')) {
      return {
        text: `**Zscaler ZIA MCP** checked categories for TeamViewer and AnyDesk (UC02).\n\n\`\`\`json\n[\n  {"url": "teamviewer.com", "action": "BLOCK", "rule": "ZIA-URLCAT-REMOTE-BLK-007"},\n  {"url": "anydesk.com",    "action": "BLOCK", "rule": "ZIA-URLCAT-REMOTE-BLK-007"}\n]\n\`\`\`\n\n⚠️ **Conflict**: Both tools are **approved** in MIG-POL-001-RA01 §3.1 for IT-managed endpoints, but ZIA-URLCAT-REMOTE-BLK-007 blocks them universally (no endpoint scope check). Affects ~450 IT support staff.\n\n**Tool calls made:** \`get_url_category\` → \`get_blocked_list\``,
        toolCalls: ['get_url_category', 'get_blocked_list'],
      }
    }
    if (msg.includes('firefox') || msg.includes('browser') || msg.includes('uc03')) {
      return {
        text: `**Zscaler ZIA MCP** checked browser URL categories (UC03).\n\n\`\`\`json\n{\n  "category": "Uncategorized Browser - Firefox Update",\n  "action": "BLOCK",\n  "rule": "ZIA-URLCAT-BROWSER-BLK-011",\n  "affected_domains": ["download.mozilla.org", "update.mozilla.org"],\n  "affected_users": 620\n}\n\`\`\`\n\n⚠️ **Conflict**: MIG-POL-001-WB01 §4.1 explicitly approves all major browsers including Firefox. Zscaler is blocking Firefox update/download domains, preventing 620 users from updating. **MEDIUM** severity.\n\n**Tool calls made:** \`get_url_category\` → \`get_blocked_list\``,
        toolCalls: ['get_url_category', 'get_blocked_list'],
      }
    }
    if (msg.includes('social') || msg.includes('uc12') || msg.includes('marketing') || msg.includes('twitter') || msg.includes('linkedin')) {
      return {
        text: `**Zscaler ZIA MCP** checked social media categories (UC12).\n\n\`\`\`json\n{\n  "category": "Social Networking",\n  "action": "BLOCK",\n  "rule": "ZIA-URLCAT-SOCIAL-BLK-002",\n  "scope": "ALL_USERS",\n  "affected_domains": ["linkedin.com", "twitter.com", "facebook.com"]\n}\n\`\`\`\n\n⚠️ **Conflict**: ZIA-URLCAT-SOCIAL-BLK-002 blocks ALL users with no department exceptions. MIG-POL-001-SM01 §5.1 explicitly grants Marketing and PR departments social media access. Approximately **340 Marketing/PR employees** are incorrectly blocked.\n\n**Tool calls made:** \`get_url_category\` → \`get_blocked_list\``,
        toolCalls: ['get_url_category', 'get_blocked_list'],
      }
    }
    if (msg.includes('block') || msg.includes('list') || msg.includes('all') || msg.includes('categor')) {
      return {
        text: `**Zscaler ZIA MCP** returned all currently blocked categories.\n\n**Blocked URL categories with policy conflicts:**\n\n| Rule ID | Category | Conflicts MIG Policy |\n|---|---|---|\n| ZIA-URLCAT-CLOUD-BLK-004 | Cloud Storage | MIG-POL-001-CS01 (dropbox) |\n| ZIA-URLCAT-REMOTE-BLK-007 | Remote Access | MIG-POL-001-RA01 (TeamViewer) |\n| ZIA-URLCAT-BROWSER-BLK-011 | Browser Downloads | MIG-POL-001-WB01 (Firefox) |\n| ZIA-URLCAT-SOCIAL-BLK-002 | Social Networking | MIG-POL-001-SM01 (Marketing) |\n\n**Total:** 4 Zscaler rules conflict with MIG-POL-001 sections.\n\n**Tool calls made:** \`get_blocked_list\``,
        toolCalls: ['get_blocked_list'],
      }
    }
  }

  if (serverId === 'bedrock-kb') {
    if (msg.includes('search') || msg.includes('find') || msg.includes('query')) {
      return {
        text: `**Bedrock KB** executed \`semantic_search\` across 5 indexed MIG policy documents.\n\n**Top 3 results** (query: "${message.substring(0, 45)}…"):\n\n1. **MIG-POL-004-SEG01** (score: 0.94) — §2.3 Production Network Isolation Standard\n2. **MIG-POL-001-CS01** (score: 0.89) — §2.1 Approved Cloud Collaboration Tools\n3. **MIG-POL-002-MFA01** (score: 0.84) — §2.1 Multi-Factor Authentication Requirements\n\n**Index stats:**\n- 5 policy documents, 47 sub-sections indexed\n- 1,284 chunks\n- Embedding model: Titan Embed Text v2\n- Vector store: OpenSearch Serverless\n- Last sync: ${new Date().toLocaleDateString()}\n\n**Tool calls made:** \`semantic_search\` → \`get_chunk\``,
        toolCalls: ['semantic_search', 'get_chunk'],
      }
    }
    if (msg.includes('pci') || msg.includes('naic') || msg.includes('sox') || msg.includes('regulat') || msg.includes('nist')) {
      return {
        text: `**Bedrock KB** searched regulatory framework cross-references.\n\n**Regulatory mappings in MIG policies:**\n\n| Framework | MIG Policy Section | ARBITER Use Case |\n|---|---|---|\n| PCI DSS 4.0 r1.3 | MIG-POL-002-SSL01 §2.2 | UC04 (SSL bypass) |\n| PCI DSS 4.0 r1.3.2 | MIG-POL-004-SEG01 §2.3 | UC08 (VPC peering) |\n| NAIC MDL-668 §4.2 | MIG-POL-003-DR01 §3.1 | UC09 (S3 geo-replication) |\n| SOX §404 | MIG-POL-003-DT01 §2.3 | UC10 (DLP blocking) |\n| NIST CSF 2.0 PR.AC | MIG-POL-005-GEO01 §2.1 | UC11 (ZTNA geo) |\n\n**Tool calls made:** \`semantic_search\` → \`get_chunk\` → \`get_embeddings\``,
        toolCalls: ['semantic_search', 'get_chunk', 'get_embeddings'],
      }
    }
  }

  if (serverId === 'servicenow-mcp') {
    if (msg.includes('incident') || msg.includes('inc') || msg.includes('create inc')) {
      return {
        text: `**ServiceNow MCP** [⚠️ DEGRADED - high latency] called \`create_incident\`.\n\n\`\`\`json\n{\n  "ticket_id": "INC0291847",\n  "type": "Incident",\n  "title": "ARBITER: NAIC MDL-668 data residency violation — claims S3 replication to eu-west-1",\n  "priority": "P1 - Critical",\n  "assigned_to": "data-governance@meridianinsurance.com",\n  "notify": ["legal@meridianinsurance.com", "ciso@meridianinsurance.com"],\n  "state": "New",\n  "created": "${new Date().toISOString()}",\n  "sla_breach_in": "4h",\n  "policy_refs": ["MIG-POL-003-DR01 §3.1", "NAIC MDL-668 §4.2"]\n}\n\`\`\`\n\n⚠️ Response took 843ms (SLA: 200ms). API gateway throttling suspected — monitor for timeouts.\n\n**Tool calls made:** \`create_incident\``,
        toolCalls: ['create_incident'],
      }
    }
    if (msg.includes('change') || msg.includes('chg') || msg.includes('request')) {
      return {
        text: `**ServiceNow MCP** [⚠️ DEGRADED] called \`create_change_request\`.\n\n\`\`\`json\n{\n  "ticket_id": "CHG0089234",\n  "type": "Change Request",\n  "title": "ARBITER-UC08: Remove dev-to-prod VPC peering (sg-mig-prod-peer-dev-001)",\n  "risk": "HIGH",\n  "environment": "PRODUCTION",\n  "approvers": [\n    "ciso@meridianinsurance.com",\n    "vpe@meridianinsurance.com"\n  ],\n  "state": "Pending Approval",\n  "policy_refs": ["MIG-POL-004-SEG01 §2.3"],\n  "regulatory_refs": ["PCI DSS 4.0 r1.3.2"]\n}\n\`\`\`\n\n**Tool calls made:** \`create_change_request\` → \`notify_approvers\``,
        toolCalls: ['create_change_request', 'notify_approvers'],
      }
    }
    if (msg.includes('ticket') || msg.includes('status') || msg.includes('get')) {
      return {
        text: `**ServiceNow MCP** [⚠️ DEGRADED] retrieved recent ARBITER-generated tickets.\n\n| Ticket | Type | Title | Status |\n|---|---|---|---|\n| INC0291847 | Incident | NAIC MDL-668 S3 violation | New |\n| INC0291848 | Incident | SSL inspection bypass PCI | In Progress |\n| CHG0089234 | Change | Remove VPC peering UC08 | Pending Approval |\n| CHG0089235 | Change | WAF association ALB UC07 | Draft |\n\n**Tool calls made:** \`get_ticket\``,
        toolCalls: ['get_ticket'],
      }
    }
  }

  // Generic fallback
  return {
    text: `**${server?.name}** is ready. Available tools:\n\n${server?.tools.map(t => `- \`${t.name}\` — ${t.desc}`).join('\n')}\n\nAsk me to run any of these, or describe what you need in plain English.`,
    toolCalls: [],
  }
}

/* ─── Components ─────────────────────────────────────────────────────── */

function ServerListItem({ server, selected, onSelect }) {
  const isOnline   = server.status === 'ONLINE'
  const isDegraded = server.status === 'DEGRADED'

  return (
    <button
      onClick={() => onSelect(server)}
      className={`w-full text-left px-3 py-3 rounded-lg border transition-all ${
        selected?.id === server.id
          ? 'bg-indigo-50 border-indigo-300 text-indigo-700'
          : 'bg-white border-slate-200 hover:border-slate-300 hover:bg-slate-50 text-slate-700'
      }`}
    >
      <div className="flex items-center gap-2 mb-1">
        <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${isOnline ? 'bg-emerald-500' : isDegraded ? 'bg-amber-500' : 'bg-red-500'}`} />
        <span className="text-xs font-semibold truncate">{server.name}</span>
      </div>
      <div className="flex items-center justify-between text-[10px] text-slate-500 pl-3.5">
        <span className="font-mono truncate">{server.host}</span>
        <span className={isOnline ? 'text-emerald-600' : isDegraded ? 'text-amber-600' : 'text-red-600'}>{server.status}</span>
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
  'policy-scanner':   ['Scan for all conflicts', 'Who owns MIG-POL-002 (Network Controls)?', 'Compare versions of MIG-POL-002', 'Show MIG-POL-001 cloud collaboration policy'],
  'conflict-detector':['Detect all cross-domain conflicts', 'Check UC08 VPC peering issue', 'What is the severity of the dev-to-prod peering?'],
  'approval-engine':  ['What approvals are needed for PROD CRITICAL?', 'Show full MIG approval matrix', 'Escalate stalled approval on CR-MIG-20260519-UC08'],
  'servicenow-mcp':   ['Create an incident for the NAIC MDL-668 violation', 'Create a change request for VPC peering removal', 'Get recent ARBITER tickets'],
  'zscaler-mcp':      ['Check category for dropbox.com', 'Check TeamViewer and AnyDesk categories', 'List all blocked categories with policy conflicts'],
  'aws-config-mcp':   ['Show security groups with open inbound rules', 'Check S3 replication on mig-prod-claims-data-primary', 'Check WAF configuration for production ALBs', 'Run compliance drift check'],
  'bedrock-kb':       ['Search for production isolation policies', 'Find documents about NAIC MDL-668 and PCI DSS', 'Show all regulatory framework mappings'],
}

/* ─── Main page ─────────────────────────────────────────────────────── */

export default function MCPChat() {
  const [selectedServer, setSelectedServer] = useState(MCP_SERVERS[0])
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [activeSessionId, setActiveSessionId] = useState(null)
  const [activeSessionTitle, setActiveSessionTitle] = useState(null)
  const bottomRef = useRef(null)
  const {
    sessions, list: listSessions, loadMessages,
    addLocalSession, bumpLocalSession, loading: sessionsLoading,
  } = useConversations({ type: 'mcp' })

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // Fetch the user's session list once on mount.
  useEffect(() => {
    listSessions().catch(() => {})
  }, [listSessions])

  // Reset chat when the user picks a different server (only if no session is loaded).
  useEffect(() => {
    if (activeSessionId) return
    setMessages([{
      role: 'assistant',
      system: true,
      content: `Connected to **${selectedServer.name}** (${selectedServer.version}) at \`${selectedServer.host}\`.\n\n${selectedServer.description}\n\nI have access to ${selectedServer.tools.length} tools. Ask me anything about this server's data, or describe what you need.`,
      toolCalls: [],
      time: new Date().toLocaleTimeString(),
    }])
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
    setMessages([{
      role: 'assistant',
      system: true,
      content: `Connected to **${selectedServer.name}** (${selectedServer.version}) at \`${selectedServer.host}\`.\n\n${selectedServer.description}\n\nI have access to ${selectedServer.tools.length} tools. Ask me anything about this server's data, or describe what you need.`,
      toolCalls: [],
      time: new Date().toLocaleTimeString(),
    }])
  }

  async function send(text) {
    const q = text || input.trim()
    if (!q) return
    setInput('')

    const userMsg = { role: 'user', content: q, time: new Date().toLocaleTimeString() }
    setMessages(prev => [...prev, userMsg])
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
      const { reply } = await sendChat({ prompt: q, session_id: sid, chat_type: 'mcp' })
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

  const suggestions = SUGGESTED[selectedServer.id] || []
  const isOnline   = selectedServer.status === 'ONLINE'
  const isDegraded = selectedServer.status === 'DEGRADED'

  return (
    <div className="flex h-full overflow-hidden">
      {/* Left: Server list */}
      <div className="w-64 flex-shrink-0 border-r border-slate-200 flex flex-col bg-slate-50">
        <div className="p-3 border-b border-slate-200">
          <p className="text-xs font-bold text-slate-600 uppercase tracking-wider mb-1">MCP Servers</p>
          <p className="text-[10px] text-slate-500">Select a server to chat with it directly</p>
        </div>
        <div className="flex-1 overflow-y-auto p-2 space-y-1">
          {MCP_SERVERS.map(srv => (
            <ServerListItem
              key={srv.id}
              server={srv}
              selected={selectedServer}
              onSelect={(s) => { setActiveSessionId(null); setActiveSessionTitle(null); setSelectedServer(s) }}
            />
          ))}

          {/* History — sessions loaded from /conversations */}
          <div className="pt-3 mt-3 border-t border-slate-200">
            <div className="flex items-center justify-between mb-1.5 px-1">
              <p className="text-[10px] font-bold text-slate-600 uppercase tracking-wider flex items-center gap-1">
                <MessageSquare size={10} /> Recent Conversations
              </p>
              <button
                onClick={newChat}
                title="Start a new chat"
                className="text-[10px] text-indigo-600 hover:text-indigo-800 flex items-center gap-0.5"
              >
                <Plus size={10} /> New
              </button>
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
                className={`w-full text-left px-2 py-1.5 rounded text-xs hover:bg-slate-100 transition-colors ${
                  activeSessionId === s.session_id ? 'bg-indigo-50 border border-indigo-200' : ''
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
          <div className={`w-2.5 h-2.5 rounded-full flex-shrink-0 ${isOnline ? 'bg-emerald-500' : isDegraded ? 'bg-amber-500' : 'bg-red-500'}`} />
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              <p className="text-sm font-semibold text-slate-900">{selectedServer.name}</p>
              <span className="text-[10px] text-slate-500 font-mono">{selectedServer.version}</span>
              {isDegraded && (
                <span className="flex items-center gap-1 text-[10px] bg-amber-50 border border-amber-200 text-amber-700 px-1.5 py-0.5 rounded-full">
                  <AlertTriangle size={9} /> DEGRADED
                </span>
              )}
            </div>
            <p className="text-[10px] text-slate-500 font-mono">{selectedServer.host} · {selectedServer.tools.length} tools · {selectedServer.latencyMs}ms · {selectedServer.uptime} uptime</p>
          </div>
          <div className="flex items-center gap-2">
            {activeSessionId && (
              <span className="flex items-center gap-1 text-[10px] bg-indigo-50 border border-indigo-200 text-indigo-700 px-1.5 py-0.5 rounded-full">
                <MessageSquare size={9} /> History: {activeSessionTitle}
              </span>
            )}
            <span className="text-[10px] text-slate-500">{selectedServer.reqToday.toLocaleString()} req/day</span>
          </div>
        </div>

        {selectedServer.warning && (
          <div className="px-5 py-2 bg-amber-50 border-b border-amber-200 flex items-center gap-2">
            <AlertTriangle size={12} className="text-amber-600 flex-shrink-0" />
            <p className="text-xs text-amber-800">{selectedServer.warning}</p>
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
                  <div className="ml-10 mt-1">
                    <CreateTicketButton detected={detected} />
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
        {suggestions.length > 0 && messages.length <= 1 && (
          <div className="px-5 pb-2 flex flex-wrap gap-1.5">
            {suggestions.map(s => (
              <button
                key={s}
                onClick={() => send(s)}
                className="text-xs bg-slate-50 hover:bg-slate-100 border border-slate-200 text-slate-700 px-3 py-1.5 rounded-lg transition-colors"
              >
                {s}
              </button>
            ))}
          </div>
        )}

        {/* Input */}
        <div className="p-4 border-t border-slate-200 bg-white">
          <div className="flex gap-2">
            <input
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && !e.shiftKey && send()}
              placeholder={`Query ${selectedServer.name}…`}
              className="input flex-1"
              disabled={loading}
            />
            <button
              onClick={() => send()}
              disabled={loading || !input.trim()}
              className="btn-primary px-3 flex items-center gap-1.5"
            >
              <Send size={14} />
            </button>
          </div>
          <p className="text-[10px] text-slate-400 mt-1.5">
            {CHAT_URL ? 'Live MCP connection' : 'Mock mode — responses are simulated'} · All queries are audit-logged · Admin only
          </p>
        </div>
      </div>
    </div>
  )
}
