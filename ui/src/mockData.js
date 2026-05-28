// Meridian Insurance Group — ARBITER POC mock dataset
// All 12 use cases from the ARBITER Scope document
// Policy references: MIG-POL-001 through MIG-POL-005

export const MOCK_CONFLICTS = [
  // ─── UC01: Policy Approves, Enforcement Blocks — Approved Cloud Storage ──────
  {
    conflict_id: 'ARBITER-UC01',
    severity: 'HIGH',
    type: 'CROSS_DOMAIN',
    title: 'Dropbox Business approved in policy but blocked by Zscaler',
    source_policy: 'MIG-POL-001-CS01 §2.1',
    source_technical: 'ZIA-URLCAT-CLOUD-BLK-042',
    finding:
      'MIG-POL-001 v3.4 (effective Jan 2026) explicitly approves Dropbox Business for all employees following Q3 2025 security assessment. ' +
      'Zscaler ZIA categorises dropbox.com under "Cloud Storage — Blocked" with action=BLOCK. ' +
      '~1,800 employees cannot access an approved collaboration tool.',
    impact:
      'All employees blocked from a tool approved by the CIO. Helpdesk volume elevated. Vendor file sharing workflows broken.',
    remediation: [
      'Remove dropbox.com from ZIA-URLCAT-CLOUD-BLK-042 or re-categorise to "Cloud Storage — Allowed"',
      'Alternatively revoke MIG-POL-001 §2.1 Dropbox approval if business need has lapsed',
      'Submit CAB change request with CISO approval',
    ],
    domains: ['SharePoint', 'Zscaler'],
    status: 'OPEN',
    detected_at: new Date(Date.now() - 2 * 3600000).toISOString(),
    policy_mandates: ['MIG-POL-001-CS01'],
    regulatory: [],
  },

  // ─── UC02: Remote Support Tools Blocked ──────────────────────────────────────
  {
    conflict_id: 'ARBITER-UC02',
    severity: 'HIGH',
    type: 'CROSS_DOMAIN',
    title: 'Approved remote support tools blocked by Zscaler for vendor users',
    source_policy: 'MIG-POL-001-RA01 §2.3, MIG-POL-005-RST01 §6',
    source_technical: 'ZIA-APP-CTRL-REMOTE-BLOCK-007',
    finding:
      'MIG-POL-001-RA01 and MIG-POL-005-RST01 explicitly approve TeamViewer Corporate, AnyDesk Enterprise, and BeyondTrust Remote Support ' +
      'for authorised IT personnel and managed service providers. Zscaler application control rule ZIA-APP-CTRL-REMOTE-BLOCK-007 blocks ' +
      'TeamViewer and AnyDesk categorically. MSP vendors cannot initiate approved support sessions.',
    impact:
      'Managed service providers are unable to perform scheduled maintenance. IT support SLAs at risk. Workarounds involve out-of-band access bypassing SIEM logging.',
    remediation: [
      'Add TeamViewer Corporate and AnyDesk Enterprise to ZIA allowed application list',
      'Scope exception to authorised vendor ZPA segments only',
      'Ensure all sessions are still logged to SIEM per MIG-POL-005-RST01 §6(d)',
    ],
    domains: ['SharePoint', 'Zscaler'],
    status: 'OPEN',
    detected_at: new Date(Date.now() - 4 * 3600000).toISOString(),
    policy_mandates: ['MIG-POL-001-RA01', 'MIG-POL-005-RST01'],
    regulatory: [],
  },

  // ─── UC03: Browser Restriction Mismatch ──────────────────────────────────────
  {
    conflict_id: 'ARBITER-UC03',
    severity: 'MEDIUM',
    type: 'CROSS_DOMAIN',
    title: 'Zscaler blocks Firefox — policy mandates browser freedom',
    source_policy: 'MIG-POL-001-WB01 §4',
    source_technical: 'ZIA-APP-CTRL-BROWSER-FF-009',
    finding:
      'MIG-POL-001-WB01 explicitly permits Chrome, Firefox, Edge, Safari, and Brave on corporate devices without CISO approval. ' +
      'Zscaler application control rule ZIA-APP-CTRL-BROWSER-FF-009 blocks Firefox download URLs and classifies Firefox traffic as "Restricted". ' +
      'Restricting browser choice without documented security justification explicitly requires CISO approval (MIG-POL-001-WB01).',
    impact:
      'Employees cannot use a policy-approved browser. Potential ADA/accessibility impacts for employees requiring Firefox-specific extensions.',
    remediation: [
      'Remove ZIA-APP-CTRL-BROWSER-FF-009 or change action from BLOCK to ALLOW',
      'If security justification exists, obtain CISO written approval and document in CAB',
    ],
    domains: ['SharePoint', 'Zscaler'],
    status: 'OPEN',
    detected_at: new Date(Date.now() - 6 * 3600000).toISOString(),
    policy_mandates: ['MIG-POL-001-WB01'],
    regulatory: [],
  },

  // ─── UC04: SSL Inspection Gaps (PCI DSS) ─────────────────────────────────────
  {
    conflict_id: 'ARBITER-UC04',
    severity: 'CRITICAL',
    type: 'CROSS_DOMAIN',
    title: 'SSL inspection bypassed for financial domains — PCI DSS violation',
    source_policy: 'MIG-POL-002-SSL01 §2.2',
    source_technical: 'ZIA-SSL-BYPASS-FIN-DOMAINS',
    finding:
      'MIG-POL-002-SSL01 mandates SSL/TLS inspection on ALL web traffic with zero exceptions unless CISO-approved and registered in the SSL Inspection Exception Register. ' +
      'Zscaler ZIA has an undocumented bypass rule ZIA-SSL-BYPASS-FIN-DOMAINS exempting 47 financial service domains from SSL inspection. ' +
      'As of Q1 2026 review, zero exceptions have been formally granted. This bypass is unregistered and violates PCI DSS Requirement 4.1.',
    impact:
      'PCI DSS Requirement 4.1 compliance gap. Encrypted threats can traverse the network uninspected. DLP cannot enforce policies on bypassed traffic. ' +
      'Likely finding in next QSA assessment.',
    remediation: [
      'Remove ZIA-SSL-BYPASS-FIN-DOMAINS immediately or submit CISO approval request (ISG-EXC-001)',
      'Register any legitimate exceptions in the SSL Inspection Exception Register with 90-day expiration',
      'Engage QSA to assess PCI DSS impact of the gap period',
    ],
    domains: ['SharePoint', 'Zscaler'],
    status: 'OPEN',
    detected_at: new Date(Date.now() - 30 * 60000).toISOString(),
    policy_mandates: ['MIG-POL-002-SSL01'],
    regulatory: ['PCI DSS 4.0 Req 4.1'],
  },

  // ─── UC05: MFA Only for Admins ───────────────────────────────────────────────
  {
    conflict_id: 'ARBITER-UC05',
    severity: 'CRITICAL',
    type: 'CROSS_DOMAIN',
    title: 'MFA enforcement limited to admin accounts — policy requires all users',
    source_policy: 'MIG-POL-002-MFA01 §4.1',
    source_technical: 'ZPA-AUTHPOL-ADMIN-MFA-ONLY',
    finding:
      'MIG-POL-002-MFA01 mandates MFA for ALL users regardless of role — including standard employees, contractors, and third-party vendors. ' +
      'Zscaler Private Access authentication policy ZPA-AUTHPOL-ADMIN-MFA-ONLY enforces MFA only for accounts in the "Privileged Admins" group. ' +
      'Standard employee, contractor, and vendor VPN/ZTNA sessions authenticate with password-only. Estimated 4,200 non-admin users affected.',
    impact:
      'Mass MFA gap across non-admin users. PCI DSS 8.4 violation. NAIC MDL-668 compliance exposure. Materially increases credential stuffing risk.',
    remediation: [
      'Expand ZPA MFA policy to apply to all user groups (not just Privileged Admins)',
      'Enforce MFA on all ZPA app segments immediately',
      'Phased rollout: contractors first (1 week), standard employees (2 weeks)',
    ],
    domains: ['SharePoint', 'Zscaler'],
    status: 'OPEN',
    detected_at: new Date(Date.now() - 90 * 60000).toISOString(),
    policy_mandates: ['MIG-POL-002-MFA01'],
    regulatory: ['PCI DSS 8.4', 'NAIC MDL-668'],
  },

  // ─── UC06: IoT Monitored Not Blocked ─────────────────────────────────────────
  {
    conflict_id: 'ARBITER-UC06',
    severity: 'HIGH',
    type: 'CROSS_DOMAIN',
    title: 'IoT devices in monitor-only mode — policy requires active blocking',
    source_policy: 'MIG-POL-002-IOT01 §5.1, MIG-POL-004-IOT01 §4',
    source_technical: 'ZIA-IOT-MONITOR-ONLY-VLAN-19',
    finding:
      'MIG-POL-002-IOT01 and MIG-POL-004-IOT01 both state that monitoring-only mode is NOT acceptable for IoT external communication controls — active blocking is required. ' +
      'VLAN-19 (IoT devices: 23 building management systems, 12 printers, 8 HVAC sensors) is in Zscaler ZIA monitor mode. ' +
      'External communications are logged but not blocked. Multiple IoT devices have established outbound connections to public IP addresses.',
    impact:
      'IoT devices actively communicating externally without enforcement. Potential C2 channel vector. Firmware update proxying not enforced — devices pulling directly from vendor CDNs.',
    remediation: [
      'Change VLAN-19 ZIA policy from MONITOR to BLOCK for external destinations',
      'Configure internal firmware update proxy for all IoT devices',
      'Generate SOC alert for all detected external IoT communications and triage',
    ],
    domains: ['SharePoint', 'Zscaler'],
    status: 'OPEN',
    detected_at: new Date(Date.now() - 3 * 3600000).toISOString(),
    policy_mandates: ['MIG-POL-002-IOT01', 'MIG-POL-004-IOT01'],
    regulatory: [],
  },

  // ─── UC07: Production ALB Exposed Without WAF ────────────────────────────────
  {
    conflict_id: 'ARBITER-UC07',
    severity: 'CRITICAL',
    type: 'CROSS_DOMAIN',
    title: 'Production ALB exposed to 0.0.0.0/0 without WAF — critical WAF bypass',
    source_policy: 'MIG-POL-004-WAF01 §2',
    source_technical: 'alb-mig-prod-claims-api-001',
    finding:
      'MIG-POL-004-WAF01 prohibits direct public internet access to any production application resource. ' +
      'ALB alb-mig-prod-claims-api-001 in vpc-mig-prod-001 has security group sg-mig-prod-alb-open allowing inbound 443 from 0.0.0.0/0 ' +
      'without routing through AWS WAF. OWASP CRS 4.0 not applied. The claims API is directly internet-facing.',
    impact:
      'Production claims API fully exposed without WAF protection. SQL injection, XSS, and volumetric attacks unmitigated. ' +
      'PCI DSS Requirement 6.4 violation. Violations classified Critical per MIG-POL-004-WAF01 requiring immediate remediation.',
    remediation: [
      'Associate AWS WAF web ACL with OWASP CRS 4.0 to alb-mig-prod-claims-api-001 immediately',
      'Update security group to restrict inbound 443 to WAF IP set only',
      'Enable rate limiting per MIG-POL-002-API01 (2,000 req/IP/5min)',
      'Forward WAF logs to SIEM within 60 seconds',
    ],
    domains: ['SharePoint', 'AWSConfig'],
    status: 'OPEN',
    detected_at: new Date(Date.now() - 1 * 3600000).toISOString(),
    policy_mandates: ['MIG-POL-004-WAF01'],
    regulatory: ['PCI DSS 6.4'],
  },

  // ─── UC08: Dev-to-Prod VPC Peering ───────────────────────────────────────────
  {
    conflict_id: 'ARBITER-UC08',
    severity: 'CRITICAL',
    type: 'CROSS_DOMAIN',
    title: 'Dev-to-prod VPC peering active — production segmentation violated',
    source_policy: 'MIG-POL-002-SSL01 §5.2, MIG-POL-004-SEG01 §3',
    source_technical: 'pcx-mig-prod-dev-001',
    finding:
      'MIG-POL-004-SEG01 prohibits VPC peering connections creating a direct packet path between production and non-production environments. ' +
      'AWS Config reports active VPC peering connection pcx-mig-prod-dev-001 between vpc-mig-prod-001 (production) and vpc-mig-dev-002 (development). ' +
      'Security group sg-mig-prod-peer-dev-001 allows ALL protocols inbound from 10.50.0.0/16 (dev VPC CIDR). Active 78 days.',
    impact:
      'Direct prod-dev data pathway active for 78 days. PCI DSS cardholder environment segmentation failure. ' +
      'Dev environment can reach production databases and APIs without restriction. Audit finding likely at next QSA review.',
    remediation: [
      'Terminate VPC peering pcx-mig-prod-dev-001 immediately',
      'Revoke inbound rule from 10.50.0.0/16 on sg-mig-prod-peer-dev-001',
      'If dev-prod data transfer is required, route through approved CI/CD pipeline with PII masking (MIG-POL-004-SEG01)',
    ],
    domains: ['SharePoint', 'AWSConfig'],
    status: 'OPEN',
    detected_at: new Date(Date.now() - 45 * 60000).toISOString(),
    policy_mandates: ['MIG-POL-002-SSL01', 'MIG-POL-004-SEG01'],
    regulatory: ['PCI DSS 1.3'],
  },

  // ─── UC09: S3 Replication to Ireland (NAIC) ──────────────────────────────────
  {
    conflict_id: 'ARBITER-UC09',
    severity: 'CRITICAL',
    type: 'CROSS_DOMAIN',
    title: 'S3 claims data replicating to eu-west-1 — NAIC data residency breach',
    source_policy: 'MIG-POL-003-DR01 §3',
    source_technical: 'mig-prod-claims-data-primary',
    finding:
      'MIG-POL-003-DR01 mandates that all customer insurance data must remain within the continental United States at all times with no exceptions. ' +
      'AWS Config shows S3 bucket mig-prod-claims-data-primary (Tier 1 PII, claims records) has active cross-region replication to eu-west-1 (Dublin, Ireland). ' +
      'Replication has been active 134 days. Legal & Compliance have not been notified. NAIC MDL-668 disclosure requirement may be triggered.',
    impact:
      'Active NAIC MDL-668 regulatory violation. Policyholder PII transmitted outside the US for 134 days. ' +
      'State insurance commissioner disclosure may be legally required in states that have adopted MDL-668.',
    remediation: [
      'Disable S3 cross-region replication on mig-prod-claims-data-primary immediately',
      'Delete replicated objects in eu-west-1 bucket',
      'Notify Legal & Compliance within 24 hours (MIG-POL-003-DR01)',
      'Engage external counsel to assess NAIC MDL-668 disclosure obligations',
    ],
    domains: ['SharePoint', 'AWSConfig'],
    status: 'OPEN',
    detected_at: new Date().toISOString(),
    policy_mandates: ['MIG-POL-003-DR01'],
    regulatory: ['NAIC MDL-668'],
  },

  // ─── UC10: DLP Blocks Authorized Transfers ───────────────────────────────────
  {
    conflict_id: 'ARBITER-UC10',
    severity: 'HIGH',
    type: 'CROSS_DOMAIN',
    title: 'DLP blanket rule blocking authorised actuarial data transfers',
    source_policy: 'MIG-POL-003-DT01 §2.1, MIG-POL-003-DLP01 §5',
    source_technical: 'ZIA-DLP-PII-BLOCK-ALL-EXTERNAL',
    finding:
      'MIG-POL-003-DT01 explicitly authorises PII data transfers to Milliman Inc., Willis Towers Watson, and Verisk Analytics. ' +
      'MIG-POL-003-DLP01 requires DLP rules to permit these authorised vendor transfers. ' +
      'Zscaler DLP rule ZIA-DLP-PII-BLOCK-ALL-EXTERNAL applies a blanket block on all PII transmission to external domains ' +
      'with no exception for the authorised actuarial vendors. Finance-to-Stripe and Finance-to-Deloitte transfers (MIG-POL-003-FIN01) are also affected.',
    impact:
      'Authorised actuarial data transfers failing silently. Finance department regulatory submissions blocked. ' +
      'DLP policy itself is non-compliant per MIG-POL-003-DLP01 — must be remediated within 48 hours of identification.',
    remediation: [
      'Add domain exceptions for milliman.com, willistowerswatson.com, verisk.com to ZIA-DLP-PII-BLOCK-ALL-EXTERNAL',
      'Add exceptions for Stripe, ACI Worldwide, Deloitte, PwC per MIG-POL-003-FIN01',
      'Maintain DLP Exception Register aligned with Authorized Transfer Register',
    ],
    domains: ['SharePoint', 'Zscaler'],
    status: 'OPEN',
    detected_at: new Date(Date.now() - 5 * 3600000).toISOString(),
    policy_mandates: ['MIG-POL-003-DT01', 'MIG-POL-003-DLP01'],
    regulatory: ['NAIC MDL-668'],
  },

  // ─── UC11: Vendor Access Geo-Blocked ─────────────────────────────────────────
  {
    conflict_id: 'ARBITER-UC11',
    severity: 'MEDIUM',
    type: 'CROSS_DOMAIN',
    title: 'ZTNA geo-restriction blocks approved vendor countries',
    source_policy: 'MIG-POL-003-VA01 §4, MIG-POL-005-GEO01 §5',
    source_technical: 'ZPA-GEO-RESTRICT-INDIA-US-ONLY',
    finding:
      'MIG-POL-003-VA01 and MIG-POL-005-GEO01 approve vendor access from 8 countries: US, India, UK, Singapore, Germany, Australia, Philippines, and Canada. ' +
      'MIG-POL-005-GEO01 explicitly states ZTNA policies restricting access to only India and the United States are non-compliant and must be updated. ' +
      'Zscaler Private Access geo-restriction policy ZPA-GEO-RESTRICT-INDIA-US-ONLY allows only India and US, blocking 6 approved countries.',
    impact:
      'UK, Singapore, Germany, Australia, Philippines, and Canada vendor personnel cannot access MIG systems from their registered offices. ' +
      'Approved reinsurance, actuarial, and technology partners blocked.',
    remediation: [
      'Update ZPA geo-restriction policy to include all 8 approved vendor countries',
      'Maintain sanctions compliance — OFAC countries remain blocked regardless (MIG-POL-005-SANC01)',
      'Update VAM system vendor location list as contracts change',
    ],
    domains: ['SharePoint', 'Zscaler'],
    status: 'OPEN',
    detected_at: new Date(Date.now() - 7 * 3600000).toISOString(),
    policy_mandates: ['MIG-POL-003-VA01', 'MIG-POL-005-GEO01'],
    regulatory: [],
  },

  // ─── UC12: Social Media Exceptions Missing ───────────────────────────────────
  {
    conflict_id: 'ARBITER-UC12',
    severity: 'MEDIUM',
    type: 'CROSS_DOMAIN',
    title: 'Social media blanket block ignores policy exemptions for 4 departments',
    source_policy: 'MIG-POL-001-SM01 §3',
    source_technical: 'ZIA-URLCAT-SOCIAL-BLOCK-ALL',
    finding:
      'MIG-POL-001-SM01 mandates URL filtering controls must include exceptions for Marketing, Communications, Human Resources, and Talent Acquisition departments. ' +
      'Blocking social media for these departments impairs critical business functions including employer branding and talent recruitment. ' +
      'Zscaler URL category rule ZIA-URLCAT-SOCIAL-BLOCK-ALL applies a blanket block on Facebook, LinkedIn, Twitter/X, Instagram, YouTube, and Reddit ' +
      'with no department-level exceptions configured.',
    impact:
      'Marketing cannot post to social channels. HR and Talent Acquisition cannot access LinkedIn for recruitment. ' +
      'Regulatory communications capability impaired. An estimated 340 employees in affected departments blocked.',
    remediation: [
      'Create department-based ZIA policy exception for Marketing, Communications, HR, and Talent Acquisition user groups',
      'Exception should apply to: facebook.com, linkedin.com, twitter.com, x.com, instagram.com, youtube.com, reddit.com',
      'General employees remain subject to guest-network-only policy for personal social media use',
    ],
    domains: ['SharePoint', 'Zscaler'],
    status: 'OPEN',
    detected_at: new Date(Date.now() - 8 * 3600000).toISOString(),
    policy_mandates: ['MIG-POL-001-SM01'],
    regulatory: [],
  },
]

export const MOCK_CHANGE_REQUESTS = [
  {
    cr_id: 'CR-20260519-WAF001',
    status: 'PENDING_APPROVAL',
    conflict_id: 'ARBITER-UC07',
    action_type: 'SECURITY_FIX',
    target_resource: 'alb-mig-prod-claims-api-001',
    target_environment: 'PROD',
    severity: 'CRITICAL',
    description: 'Associate AWS WAF web ACL with OWASP CRS 4.0 to production claims API load balancer',
    requested_by: 'sec.analyst@meridianinsurance.com',
    justification: 'Production ALB directly internet-accessible without WAF. PCI DSS Req 6.4 violation. Immediate remediation required per MIG-POL-004-WAF01.',
    created_at: new Date(Date.now() - 3600000).toISOString(),
    approvers: [
      { role: 'ciso', email: 'a.nakamura@meridianinsurance.com', status: 'PENDING', description: 'CISO approval required for PROD CRITICAL' },
      { role: 'vp_network', email: 'vp-network@meridianinsurance.com', status: 'PENDING', description: 'VP Network Engineering approval required' },
      { role: 'legal_notification', email: 'legal@meridianinsurance.com', type: 'NOTIFICATION', status: 'NOTIFIED', description: 'Legal notified of PCI DSS impact' },
    ],
    total_approvers_needed: 2,
    total_approvals_received: 0,
  },
  {
    cr_id: 'CR-20260519-VPC002',
    status: 'PENDING_APPROVAL',
    conflict_id: 'ARBITER-UC08',
    action_type: 'SECURITY_FIX',
    target_resource: 'pcx-mig-prod-dev-001',
    target_environment: 'PROD',
    severity: 'CRITICAL',
    description: 'Terminate dev-to-prod VPC peering and revoke sg-mig-prod-peer-dev-001 inbound rule',
    requested_by: 'sec.analyst@meridianinsurance.com',
    justification: 'Active prod-dev peering for 78 days. PCI DSS segmentation failure. Violates MIG-POL-004-SEG01.',
    created_at: new Date(Date.now() - 2 * 3600000).toISOString(),
    approvers: [
      { role: 'ciso', email: 'a.nakamura@meridianinsurance.com', status: 'PENDING', description: 'CISO approval required for PROD CRITICAL' },
      { role: 'vp_network', email: 'j.park@meridianinsurance.com', status: 'APPROVED', description: 'VP Network Engineering approved', approved_at: new Date(Date.now() - 1800000).toISOString() },
      { role: 'legal_notification', email: 'legal@meridianinsurance.com', type: 'NOTIFICATION', status: 'NOTIFIED', description: 'Legal notified' },
    ],
    total_approvers_needed: 2,
    total_approvals_received: 1,
  },
]

export const MOCK_AUDIT = [
  {
    log_id: '1',
    timestamp: new Date().toISOString(),
    action_type: 'SCAN_TRIGGERED',
    resource: 'full-scan',
    user: 'sec.analyst@meridianinsurance.com',
    status: 'COMPLETED',
    details: '{"conflicts_found": 12, "critical": 4, "high": 4, "medium": 3, "low": 0, "scan_ms": 3241}',
  },
  {
    log_id: '2',
    timestamp: new Date(Date.now() - 3600000).toISOString(),
    action_type: 'CR_CREATED',
    resource: 'alb-mig-prod-claims-api-001',
    user: 'sec.analyst@meridianinsurance.com',
    status: 'PENDING_APPROVAL',
    details: '{"cr_id": "CR-20260519-WAF001", "conflict_id": "ARBITER-UC07"}',
  },
  {
    log_id: '3',
    timestamp: new Date(Date.now() - 2 * 3600000).toISOString(),
    action_type: 'CR_CREATED',
    resource: 'pcx-mig-prod-dev-001',
    user: 'sec.analyst@meridianinsurance.com',
    status: 'PENDING_APPROVAL',
    details: '{"cr_id": "CR-20260519-VPC002", "conflict_id": "ARBITER-UC08"}',
  },
  {
    log_id: '4',
    timestamp: new Date(Date.now() - 2.5 * 3600000).toISOString(),
    action_type: 'APPROVAL_GRANTED',
    resource: 'CR-20260519-VPC002',
    user: 'j.park@meridianinsurance.com',
    status: 'APPROVED',
    details: '{"approver_role": "vp_network", "comment": "Confirmed — dev peering was temporary, should have been removed 60 days ago."}',
  },
  {
    log_id: '5',
    timestamp: new Date(Date.now() - 5 * 3600000).toISOString(),
    action_type: 'INGESTION_COMPLETE',
    resource: 'SharePoint — MIG-POL-001 through MIG-POL-005',
    user: 'system',
    status: 'COMPLETED',
    details: '{"documents_indexed": 5, "chunks": 1247, "model": "Titan Embed Text v2"}',
  },
]

// Helper: count by severity
export function countBySeverity(findings) {
  return {
    CRITICAL: findings.filter(f => f?.severity === 'CRITICAL').length,
    HIGH:     findings.filter(f => f?.severity === 'HIGH').length,
    MEDIUM:   findings.filter(f => f?.severity === 'MEDIUM').length,
    LOW:      findings.filter(f => f?.severity === 'LOW').length,
  }
}

// Helper: build domain × severity matrix
export function buildConflictMatrix(findings) {
  const domains = ['SharePoint', 'Zscaler', 'AWSConfig']
  const severities = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']
  const matrix = {}
  domains.forEach(d => {
    matrix[d] = {}
    severities.forEach(s => { matrix[d][s] = 0 })
  })
  findings.forEach(f => {
    const seen = new Set()
    f.domains?.forEach(d => {
      if (matrix[d] && f.severity && !seen.has(d)) {
        matrix[d][f.severity]++
        seen.add(d)
      }
    })
  })
  return matrix
}

// Helper: filter findings by compliance framework
export function filterByRegulatory(findings, framework) {
  return findings.filter(f => f.regulatory?.some(r => r.startsWith(framework)))
}
