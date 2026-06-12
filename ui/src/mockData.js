// Meridian Insurance Group — ARBITER POC mock dataset
// All 12 use cases from the ARBITER Scope document
// Policy references: MIG-POL-001 through MIG-POL-005

const RAW_CONFLICTS = [
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
    regulatory: ['ISO 27001 A.5.10'],
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
    regulatory: ['ISO 27001 A.5.20', 'SOC 2 CC9.2'],
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
    regulatory: ['SOC 2 CC6.1'],
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
    regulatory: ['PCI DSS 1.4', 'ISO 27001 A.8.22'],
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
    regulatory: ['ISO 27001 A.5.23', 'SOC 2 CC6.6'],
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
    regulatory: ['SOC 2 CC7.4', 'ISO 27001 A.5.10'],
  },

  // ─── UC13: Perimeter Egress — Policy Default-Deny vs Palo Alto Any/Any Allow ──
  {
    conflict_id: 'ARBITER-UC13',
    severity: 'HIGH',
    type: 'CROSS_DOMAIN',
    title: 'Palo Alto permits any/any outbound — policy mandates default-deny egress',
    source_policy: 'MIG-POL-002-NS06 §6',
    source_technical: 'PAN-SEC-EGRESS-ANYANY-ALLOW-001',
    finding:
      'MIG-POL-002 §6 mandates default-deny perimeter egress with explicit allow-listing of approved destinations. ' +
      'Palo Alto NGFW security rule PAN-SEC-EGRESS-ANYANY-ALLOW-001 permits any source to any destination outbound (trust → untrust, application=any). ' +
      'The perimeter firewall contradicts the documented egress posture.',
    impact:
      'Unrestricted outbound path at the perimeter firewall. Data-exfiltration and C2 channels are unmitigated despite a default-deny policy. PCI DSS Req 1.3 egress-control gap.',
    remediation: [
      'Replace PAN-SEC-EGRESS-ANYANY-ALLOW-001 with an explicit allow-list of approved destinations / App-IDs',
      'Set the perimeter egress default action to deny-and-log',
      'Reconcile the allow-list with the Zscaler ZIA category policy',
    ],
    domains: ['SharePoint', 'PaloAlto'],
    status: 'OPEN',
    detected_at: new Date(Date.now() - 9 * 3600000).toISOString(),
    policy_mandates: ['MIG-POL-002-NS06'],
    regulatory: ['PCI DSS 1.3', 'ISO 27001 A.8.20'],
  },

  // ─── UC14: Tool-vs-Tool — Zscaler Blocks Anonymizer, Palo Alto Allows Tor ─────
  {
    conflict_id: 'ARBITER-UC14',
    severity: 'CRITICAL',
    type: 'CROSS_DOMAIN',
    title: 'Anonymizer traffic blocked by Zscaler but allowed by Palo Alto — enforcement bypass',
    source_policy: 'ZIA-URLCAT-ANONYMIZER-BLOCK',
    source_technical: 'PAN-SEC-APP-TOR-ALLOW-022',
    finding:
      'Zscaler ZIA rule ZIA-URLCAT-ANONYMIZER-BLOCK blocks the Anonymizer/Tor category, while Palo Alto rule PAN-SEC-APP-TOR-ALLOW-022 permits the "tor" App-ID outbound. ' +
      'Two security teams’ controls disagree: traffic egressing via the Palo Alto perimeter bypasses the Zscaler web-proxy control entirely.',
    impact:
      'Live enforcement bypass. Hosts routed through the firewall path can reach anonymizer/Tor destinations the proxy is meant to block. Data-exfiltration and C2 risk; undermines the SSL-inspection control.',
    remediation: [
      'Align the Palo Alto rulebase with the Zscaler category policy — deny the "tor" / anonymizer App-IDs at the perimeter',
      'Establish a single source of truth for category enforcement across Zscaler and Palo Alto',
      'Audit egress paths that bypass the Zscaler tunnel',
    ],
    domains: ['Zscaler', 'PaloAlto'],
    status: 'OPEN',
    detected_at: new Date(Date.now() - 1 * 3600000).toISOString(),
    policy_mandates: [],
    regulatory: ['PCI DSS 1.3', 'NAIC MDL-668'],
  },
]

// ── Per-UC enrichment: new fields the Scanner + Dashboard require ─────────────
// Adds conflict_type (CONTRADICTION/GAP/DRIFT/OVERLAP), domain (UC-level), structured
// policy_citations + enforcement_evidence, rule_key, source_pair. Merged into the
// MOCK_CONFLICTS export below so existing flat fields (source_policy, source_technical,
// finding, impact, remediation) are preserved for backward compat.
const UC_ENRICHMENT = {
  'ARBITER-UC01': {
    conflict_type: 'CONTRADICTION', domain: 'ACCESS_MGMT', source_pair: 'SharePoint+Zscaler',
    rule_key: 'UC01', fp_score: 0.05, compliant: false,
    policy_citations: [{ doc: 'MIG-POL-001', version: 'v3.4', section: '2.1', quote: 'Dropbox Business listed as approved. Passed vendor assessment Q3 2025.', confidence: 0.97 }],
    enforcement_evidence: [{ source: 'Zscaler', rule_id: 'ZIA-URLCAT-CLOUD-BLK-042', action: 'BLOCK', raw: { category: 'Cloud Storage', domains: ['dropbox.com'] } }],
  },
  'ARBITER-UC02': {
    conflict_type: 'CONTRADICTION', domain: 'VENDOR_MGMT', source_pair: 'SharePoint+Zscaler',
    rule_key: 'UC02', fp_score: 0.07, compliant: false,
    policy_citations: [
      { doc: 'MIG-POL-001', version: 'v3.4', section: '2.3', quote: 'TeamViewer Corporate, AnyDesk Enterprise, BeyondTrust Remote Support are approved for authorised IT and MSP personnel.', confidence: 0.96 },
      { doc: 'MIG-POL-005', version: 'v2.8', section: '6', quote: 'All vendor remote-support sessions must be logged to SIEM.', confidence: 0.94 },
    ],
    enforcement_evidence: [{ source: 'Zscaler', rule_id: 'ZIA-APP-CTRL-REMOTE-BLOCK-007', action: 'BLOCK', raw: { apps: ['TeamViewer', 'AnyDesk'] } }],
  },
  'ARBITER-UC03': {
    conflict_type: 'CONTRADICTION', domain: 'ACCESS_MGMT', source_pair: 'SharePoint+Zscaler',
    rule_key: 'UC03', fp_score: 0.15, compliant: false,
    policy_citations: [{ doc: 'MIG-POL-001', version: 'v3.4', section: '4', quote: 'Chrome, Firefox, Edge, Safari, Brave are permitted on corporate devices without further approval.', confidence: 0.95 }],
    enforcement_evidence: [{ source: 'Zscaler', rule_id: 'ZIA-APP-CTRL-BROWSER-FF-009', action: 'BLOCK', raw: { app: 'Firefox' } }],
  },
  'ARBITER-UC04': {
    conflict_type: 'GAP', domain: 'COMPLIANCE', source_pair: 'SharePoint+Zscaler',
    rule_key: 'UC04', fp_score: 0.03, compliant: false,
    policy_citations: [{ doc: 'MIG-POL-002', version: 'v5.1', section: '2.2', quote: 'SSL/TLS inspection is mandatory on ALL web traffic. Exceptions only with documented CISO approval in the SSL Inspection Exception Register, 90-day max.', confidence: 0.98 }],
    enforcement_evidence: [{ source: 'Zscaler', rule_id: 'ZIA-SSL-BYPASS-FIN-DOMAINS', action: 'BYPASS_INSPECT', raw: { domains_count: 47, registered_exception: false } }],
  },
  'ARBITER-UC05': {
    conflict_type: 'GAP', domain: 'ACCESS_MGMT', source_pair: 'SharePoint+Zscaler',
    rule_key: 'UC05', fp_score: 0.04, compliant: false,
    policy_citations: [{ doc: 'MIG-POL-002', version: 'v5.1', section: '4.1', quote: 'MFA is required for ALL users — employees, contractors, vendors — regardless of privilege level.', confidence: 0.97 }],
    enforcement_evidence: [{ source: 'Zscaler', rule_id: 'ZPA-AUTHPOL-ADMIN-MFA-ONLY', action: 'MFA_REQUIRED', raw: { scope: 'Privileged Admins', non_admin_users_unprotected: 4200 } }],
  },
  'ARBITER-UC06': {
    conflict_type: 'GAP', domain: 'NETWORK_SECURITY', source_pair: 'SharePoint+Zscaler',
    rule_key: 'UC06', fp_score: 0.06, compliant: false,
    policy_citations: [{ doc: 'MIG-POL-002', version: 'v5.1', section: '5.1', quote: 'Monitoring-only mode is NOT acceptable for IoT external communication. Active blocking is required.', confidence: 0.96 }],
    enforcement_evidence: [{ source: 'Zscaler', rule_id: 'ZIA-IOT-MONITOR-ONLY-VLAN-19', action: 'MONITOR', raw: { vlan: 19, devices: 43 } }],
  },
  'ARBITER-UC07': {
    conflict_type: 'DRIFT', domain: 'CLOUD_SECURITY', source_pair: 'SharePoint+AWS Config',
    rule_key: 'UC07', fp_score: 0.02, compliant: false,
    policy_citations: [{ doc: 'MIG-POL-004', version: 'v4.0', section: '2', quote: 'No production application resource shall be directly accessible from the public internet without AWS WAF + OWASP CRS.', confidence: 0.98 }],
    enforcement_evidence: [{ source: 'AWSConfig', resource_id: 'alb-mig-prod-claims-api-001', action: 'NON_COMPLIANT', raw: { security_group: 'sg-mig-prod-alb-open', ingress: '0.0.0.0/0:443', waf_attached: false, age_days: 47 } }],
  },
  'ARBITER-UC08': {
    conflict_type: 'DRIFT', domain: 'NETWORK_SECURITY', source_pair: 'SharePoint+AWS Config',
    rule_key: 'UC08', fp_score: 0.02, compliant: false,
    policy_citations: [{ doc: 'MIG-POL-004', version: 'v4.0', section: '3', quote: 'VPC peering between production and non-production environments is prohibited.', confidence: 0.98 }],
    enforcement_evidence: [{ source: 'AWSConfig', resource_id: 'pcx-mig-prod-dev-001', action: 'NON_COMPLIANT', raw: { prod_vpc: 'vpc-mig-prod-001', dev_vpc: 'vpc-mig-dev-002', age_days: 78 } }],
  },
  'ARBITER-UC09': {
    conflict_type: 'DRIFT', domain: 'DATA_GOVERNANCE', source_pair: 'SharePoint+AWS Config',
    rule_key: 'UC09', fp_score: 0.03, compliant: false,
    policy_citations: [{ doc: 'MIG-POL-003', version: 'v2.2', section: '3', quote: 'All customer insurance data must remain within the continental United States. No exceptions.', confidence: 0.97 }],
    enforcement_evidence: [{ source: 'AWSConfig', resource_id: 'mig-prod-claims-data-primary', action: 'NON_COMPLIANT', raw: { replication_target: 'eu-west-1', pii_tier: 1, age_days: 134 } }],
  },
  'ARBITER-UC10': {
    conflict_type: 'CONTRADICTION', domain: 'DATA_GOVERNANCE', source_pair: 'SharePoint+Zscaler',
    rule_key: 'UC10', fp_score: 0.08, compliant: false,
    policy_citations: [{ doc: 'MIG-POL-003', version: 'v2.2', section: '2.1', quote: 'Authorised actuarial data transfers: Milliman Inc., Willis Towers Watson, Verisk Analytics.', confidence: 0.95 }],
    enforcement_evidence: [{ source: 'Zscaler', rule_id: 'ZIA-DLP-PII-BLOCK-ALL-EXTERNAL', action: 'BLOCK', raw: { exceptions: [] } }],
  },
  'ARBITER-UC11': {
    conflict_type: 'CONTRADICTION', domain: 'VENDOR_MGMT', source_pair: 'SharePoint+Zscaler',
    rule_key: 'UC11', fp_score: 0.10, compliant: false,
    policy_citations: [
      { doc: 'MIG-POL-003', version: 'v2.2', section: '4', quote: 'Approved vendor countries: US, India, UK, Singapore, Germany, Australia, Philippines, Canada.', confidence: 0.96 },
      { doc: 'MIG-POL-005', version: 'v2.8', section: '5', quote: 'ZTNA restrictions limited to India and US only are non-compliant.', confidence: 0.97 },
    ],
    enforcement_evidence: [{ source: 'Zscaler', rule_id: 'ZPA-GEO-RESTRICT-INDIA-US-ONLY', action: 'ALLOW', raw: { countries: ['IN', 'US'] } }],
  },
  'ARBITER-UC12': {
    conflict_type: 'GAP', domain: 'ACCESS_MGMT', source_pair: 'SharePoint+Zscaler',
    rule_key: 'UC12', fp_score: 0.12, compliant: false,
    policy_citations: [{ doc: 'MIG-POL-001', version: 'v3.4', section: '3', quote: 'URL filtering controls must include exceptions for Marketing, Communications, HR, and Talent Acquisition.', confidence: 0.95 }],
    enforcement_evidence: [{ source: 'Zscaler', rule_id: 'ZIA-URLCAT-SOCIAL-BLOCK-ALL', action: 'BLOCK', raw: { department_exceptions: [] } }],
  },
  'ARBITER-UC13': {
    conflict_type: 'GAP', domain: 'NETWORK_SECURITY', source_pair: 'SharePoint+Palo Alto',
    rule_key: 'UC13', fp_score: 0.05, compliant: false,
    policy_citations: [{ doc: 'MIG-POL-002', version: 'v5.1', section: '6', quote: 'Perimeter egress must be default-deny. Outbound access to high-risk or uncategorised destinations is prohibited without an explicit, documented allow-list entry.', confidence: 0.96 }],
    enforcement_evidence: [{ source: 'PaloAlto', rule_id: 'PAN-SEC-EGRESS-ANYANY-ALLOW-001', action: 'ALLOW', raw: { source: 'any', destination: 'any', application: 'any' } }],
  },
  'ARBITER-UC14': {
    conflict_type: 'CONTRADICTION', domain: 'NETWORK_SECURITY', source_pair: 'Zscaler+Palo Alto',
    rule_key: 'UC14', fp_score: 0.04, compliant: false,
    policy_citations: [],
    enforcement_evidence: [
      { source: 'Zscaler', rule_id: 'ZIA-URLCAT-ANONYMIZER-BLOCK', action: 'BLOCK', raw: { category: 'Anonymizer' } },
      { source: 'PaloAlto', rule_id: 'PAN-SEC-APP-TOR-ALLOW-022', action: 'ALLOW', raw: { application: ['tor', 'ultrasurf'] } },
    ],
  },
}

// ── Team / tag ownership ──────────────────────────────────────────────────────
// Mirrors scripts/seed_mock_data.py OWNERSHIP (keyed by rule_key) so mock mode
// renders identical team data to a live scan. owner_team = policy owner;
// consumer_team = team affected/blocked; platform_team = control manager.
export const TEAM_LABELS = {
  'platform-security': 'Security Platform',
  'network-eng':       'Network Engineering',
  'cloud-infra':       'Cloud Infrastructure',
  'data-governance':   'Data Governance',
  'app-dev':           'Application Development',
  'vendor-mgmt':       'Vendor Management',
  'unassigned':        'Unassigned',
}
export const TEAMS = Object.keys(TEAM_LABELS).filter(t => t !== 'unassigned')
export const TAGS = ['infrastructure', 'database', 'network', 'application', 'identity', 'data-residency', 'vendor']

const OWNERSHIP_BY_RULE = {
  UC01: { owner_team: 'data-governance',   consumer_team: 'app-dev',     platform_team: 'network-eng',       tags: ['application', 'network'] },
  UC02: { owner_team: 'vendor-mgmt',       consumer_team: 'app-dev',     platform_team: 'network-eng',       tags: ['vendor', 'network'] },
  UC03: { owner_team: 'data-governance',   consumer_team: 'app-dev',     platform_team: 'network-eng',       tags: ['application', 'network'] },
  UC04: { owner_team: 'platform-security', consumer_team: 'cloud-infra', platform_team: 'network-eng',       tags: ['network', 'data-residency'] },
  UC05: { owner_team: 'platform-security', consumer_team: 'app-dev',     platform_team: 'platform-security', tags: ['identity'] },
  UC06: { owner_team: 'network-eng',       consumer_team: 'cloud-infra', platform_team: 'network-eng',       tags: ['network', 'infrastructure'] },
  UC07: { owner_team: 'cloud-infra',       consumer_team: 'app-dev',     platform_team: 'cloud-infra',       tags: ['infrastructure', 'network'] },
  UC08: { owner_team: 'cloud-infra',       consumer_team: 'app-dev',     platform_team: 'cloud-infra',       tags: ['infrastructure', 'network'] },
  UC09: { owner_team: 'data-governance',   consumer_team: 'cloud-infra', platform_team: 'cloud-infra',       tags: ['data-residency', 'infrastructure'] },
  UC10: { owner_team: 'data-governance',   consumer_team: 'app-dev',     platform_team: 'network-eng',       tags: ['data-residency', 'application'] },
  UC11: { owner_team: 'vendor-mgmt',       consumer_team: 'app-dev',     platform_team: 'network-eng',       tags: ['vendor', 'network'] },
  UC12: { owner_team: 'data-governance',   consumer_team: 'app-dev',     platform_team: 'network-eng',       tags: ['application', 'network'] },
  UC13: { owner_team: 'network-eng',       consumer_team: 'app-dev',     platform_team: 'platform-security', tags: ['network', 'infrastructure'] },
  UC14: { owner_team: 'network-eng',       consumer_team: 'app-dev',     platform_team: 'platform-security', tags: ['network', 'application'] },
}
const OWNERSHIP_DEFAULT = { owner_team: 'unassigned', consumer_team: '', platform_team: '', tags: ['untriaged'] }
export function ownershipForRule(ruleKey) { return OWNERSHIP_BY_RULE[ruleKey] || OWNERSHIP_DEFAULT }

// ── Additional seed conflicts (demo volume) ─────────────────────────────────
// Extra findings so the Dashboard KPI tiles (Open / Critical / Resolved / Total)
// reflect a fuller workload — matched 1:1 by scripts/seed_mock_data.py for live mode.
// All rows are compliant:false so the live /findings API (which drops compliant=true
// rows) still returns them. OPEN rows are active conflicts; RESOLVED rows carry
// status:'RESOLVED' at non-critical severity, so they raise Total + Resolved without
// inflating Open (status filter) or Critical (severity is never CRITICAL on resolved).
// Each row is already fully shaped (ownership inline), so it bypasses the .map below.
const EXTRA_CONFLICTS = [
  // — Active conflicts (OPEN) —
  { conflict_id: 'ARBITER-UC15', severity: 'CRITICAL', status: 'OPEN', compliant: false, conflict_type: 'DRIFT', type: 'CROSS_DOMAIN',
    domain: 'CLOUD_SECURITY', source_pair: 'SharePoint+AWS Config', domains: ['SharePoint', 'AWSConfig'],
    title: 'Public RDS snapshot exposes production claims database', fp_score: 0.03,
    source_policy: 'MIG-POL-004 v4.0 §4', source_technical: 'rds-mig-prod-claims-snap-0417',
    finding: 'An automated RDS snapshot of mig-prod-claims-db is shared publicly (restore attribute = all). MIG-POL-004 §4 prohibits public exposure of any production data store.',
    impact: 'Full claims database recoverable by any AWS account. Tier-1 PII at risk.',
    remediation: ['Remove the public share attribute from rds-mig-prod-claims-snap-0417', 'Audit all shared snapshots across prod accounts', 'Enable AWS Config rule rds-snapshots-public-prohibited'],
    policy_mandates: ['MIG-POL-004-CS04'], regulatory: ['NAIC MDL-668 §4', 'SOC 2 CC6.1'],
    detected_at: new Date(Date.now() - 18 * 60000).toISOString(),
    owner_team: 'cloud-infra', consumer_team: 'app-dev', platform_team: 'cloud-infra', tags: ['infrastructure', 'data-residency'] },
  { conflict_id: 'ARBITER-UC16', severity: 'CRITICAL', status: 'OPEN', compliant: false, conflict_type: 'DRIFT', type: 'CROSS_DOMAIN',
    domain: 'DATA_GOVERNANCE', source_pair: 'SharePoint+AWS Config', domains: ['SharePoint', 'AWSConfig'],
    title: 'Customer-document S3 bucket public and unencrypted', fp_score: 0.02,
    source_policy: 'MIG-POL-003 v2.2 §2', source_technical: 'mig-prod-customer-docs',
    finding: 'Bucket mig-prod-customer-docs has BlockPublicAcls=false and default encryption disabled. MIG-POL-003 §2 mandates encryption-at-rest and private ACLs for all customer data.',
    impact: 'Policyholder documents readable anonymously; no encryption at rest.',
    remediation: ['Enable S3 Block Public Access on mig-prod-customer-docs', 'Apply SSE-KMS default encryption', 'Add bucket to the data-residency Config conformance pack'],
    policy_mandates: ['MIG-POL-003-DG02'], regulatory: ['NAIC MDL-668 §3', 'ISO 27001 A.8.24'],
    detected_at: new Date(Date.now() - 26 * 60000).toISOString(),
    owner_team: 'data-governance', consumer_team: 'cloud-infra', platform_team: 'cloud-infra', tags: ['data-residency', 'infrastructure'] },
  { conflict_id: 'ARBITER-UC17', severity: 'CRITICAL', status: 'OPEN', compliant: false, conflict_type: 'CONTRADICTION', type: 'CROSS_DOMAIN',
    domain: 'NETWORK_SECURITY', source_pair: 'Zscaler+Palo Alto', domains: ['Zscaler', 'PaloAlto'],
    title: 'Anonymizer egress re-enabled on Palo Alto perimeter', fp_score: 0.04,
    source_policy: 'MIG-POL-002 v5.1 §6', source_technical: 'PAN-SEC-APP-TOR-ALLOW-031',
    finding: 'A new Palo Alto rule PAN-SEC-APP-TOR-ALLOW-031 permits tor/ultrasurf outbound, contradicting Zscaler ZIA-URLCAT-ANONYMIZER-BLOCK and MIG-POL-002 §6 default-deny egress.',
    impact: 'Data-exfiltration channel reopened; enforcement inconsistent across the two control planes.',
    remediation: ['Disable PAN-SEC-APP-TOR-ALLOW-031', 'Reconcile Palo Alto egress with the Zscaler anonymizer block', 'Add a CAB gate for any new any-application allow rules'],
    policy_mandates: ['MIG-POL-002-NS06'], regulatory: ['ISO 27001 A.8.20'],
    detected_at: new Date(Date.now() - 41 * 60000).toISOString(),
    owner_team: 'network-eng', consumer_team: 'app-dev', platform_team: 'platform-security', tags: ['network', 'application'] },
  { conflict_id: 'ARBITER-UC18', severity: 'HIGH', status: 'OPEN', compliant: false, conflict_type: 'GAP', type: 'CROSS_DOMAIN',
    domain: 'ACCESS_MGMT', source_pair: 'SharePoint+Zscaler', domains: ['SharePoint', 'Zscaler'],
    title: 'Terminated contractor accounts retain ZTNA access', fp_score: 0.09,
    source_policy: 'MIG-POL-001 v3.4 §5', source_technical: 'ZPA-USERGRP-CONTRACTORS-2024',
    finding: 'Eleven contractor identities offboarded in HR remain in the Zscaler ZPA contractor group with active app segments. MIG-POL-001 §5 requires access revocation within 24h of termination.',
    impact: 'Former contractors retain private-app access to internal systems.',
    remediation: ['Remove the 11 offboarded identities from ZPA-USERGRP-CONTRACTORS-2024', 'Wire SCIM deprovisioning from the HR system', 'Schedule a weekly stale-account reconciliation'],
    policy_mandates: ['MIG-POL-001-AM05'], regulatory: ['SOC 2 CC6.2'],
    detected_at: new Date(Date.now() - 55 * 60000).toISOString(),
    owner_team: 'platform-security', consumer_team: 'app-dev', platform_team: 'platform-security', tags: ['identity'] },
  { conflict_id: 'ARBITER-UC19', severity: 'HIGH', status: 'OPEN', compliant: false, conflict_type: 'CONTRADICTION', type: 'CROSS_DOMAIN',
    domain: 'VENDOR_MGMT', source_pair: 'SharePoint+Zscaler', domains: ['SharePoint', 'Zscaler'],
    title: 'Unapproved SaaS (Notion) permitted by URL policy', fp_score: 0.11,
    source_policy: 'MIG-POL-001 v3.4 §2.1', source_technical: 'ZIA-URLCAT-COLLAB-ALLOW-058',
    finding: 'Zscaler allows notion.so under Collaboration, but Notion is not on the MIG-POL-001 approved-vendor list and has no completed vendor assessment.',
    impact: 'Corporate data may flow to an unvetted third-party processor.',
    remediation: ['Block notion.so pending a vendor risk assessment, or add it to the approved list once assessed', 'Reconcile ZIA collaboration allow-list against the approved-vendor register'],
    policy_mandates: ['MIG-POL-001-VM02'], regulatory: ['ISO 27001 A.5.19'],
    detected_at: new Date(Date.now() - 72 * 60000).toISOString(),
    owner_team: 'vendor-mgmt', consumer_team: 'app-dev', platform_team: 'network-eng', tags: ['vendor', 'application'] },
  { conflict_id: 'ARBITER-UC20', severity: 'MEDIUM', status: 'OPEN', compliant: false, conflict_type: 'GAP', type: 'CROSS_DOMAIN',
    domain: 'COMPLIANCE', source_pair: 'SharePoint+Zscaler', domains: ['SharePoint', 'Zscaler'],
    title: 'Web-log retention below the 365-day policy minimum', fp_score: 0.14,
    source_policy: 'MIG-POL-002 v5.1 §7', source_technical: 'ZIA-NSS-LOG-RETENTION-90D',
    finding: 'Zscaler NSS log streaming is configured for 90-day retention; MIG-POL-002 §7 requires a minimum of 365 days for audit traceability.',
    impact: 'Insufficient log history for incident forensics and audit evidence.',
    remediation: ['Extend ZIA NSS retention to 365 days', 'Stream logs to the long-term S3 audit archive', 'Document the retention setting in the controls register'],
    policy_mandates: ['MIG-POL-002-CP07'], regulatory: ['SOC 2 CC7.2', 'ISO 27001 A.8.15'],
    detected_at: new Date(Date.now() - 96 * 60000).toISOString(),
    owner_team: 'platform-security', consumer_team: 'cloud-infra', platform_team: 'platform-security', tags: ['infrastructure'] },

  // — Resolved findings (status RESOLVED, non-critical → raise Total + Resolved only) —
  { conflict_id: 'ARBITER-UC21', severity: 'HIGH', status: 'RESOLVED', compliant: false, conflict_type: 'DRIFT', type: 'CROSS_DOMAIN',
    domain: 'CLOUD_SECURITY', source_pair: 'SharePoint+AWS Config', domains: ['SharePoint', 'AWSConfig'],
    title: 'ALB alb-mig-prod-quotes-004 now fronted by AWS WAF', fp_score: 0.02,
    source_policy: 'MIG-POL-004 v4.0 §2', source_technical: 'alb-mig-prod-quotes-004',
    finding: 'WAF web ACL with OWASP CRS attached to the previously-exposed ALB; ingress restricted from 0.0.0.0/0. Closed via CR-20260604-WAF014.',
    impact: 'Resolved — production ALB no longer internet-exposed without WAF.',
    remediation: ['WAF web ACL attached', 'Security-group ingress tightened', 'Change executed and verified'],
    policy_mandates: ['MIG-POL-004-CS01'], regulatory: ['SOC 2 CC6.1'],
    detected_at: new Date(Date.now() - 30 * 3600000).toISOString(),
    owner_team: 'cloud-infra', consumer_team: 'app-dev', platform_team: 'cloud-infra', tags: ['infrastructure', 'network'] },
  { conflict_id: 'ARBITER-UC22', severity: 'HIGH', status: 'RESOLVED', compliant: false, conflict_type: 'GAP', type: 'CROSS_DOMAIN',
    domain: 'NETWORK_SECURITY', source_pair: 'SharePoint+Palo Alto', domains: ['SharePoint', 'PaloAlto'],
    title: 'Palo Alto any-any egress rule removed', fp_score: 0.03,
    source_policy: 'MIG-POL-002 v5.1 §6', source_technical: 'PAN-SEC-EGRESS-ANYANY-ALLOW-001',
    finding: 'The permissive any/any/any egress rule was replaced with an explicit allow-list and a default-deny. Closed via CR-20260602-EGR009.',
    impact: 'Resolved — perimeter egress is now default-deny per policy.',
    remediation: ['any-any rule deleted', 'Explicit allow-list authored', 'Default-deny verified in production'],
    policy_mandates: ['MIG-POL-002-NS06'], regulatory: ['ISO 27001 A.8.20'],
    detected_at: new Date(Date.now() - 44 * 3600000).toISOString(),
    owner_team: 'network-eng', consumer_team: 'app-dev', platform_team: 'platform-security', tags: ['network', 'infrastructure'] },
  { conflict_id: 'ARBITER-UC23', severity: 'MEDIUM', status: 'RESOLVED', compliant: false, conflict_type: 'GAP', type: 'CROSS_DOMAIN',
    domain: 'ACCESS_MGMT', source_pair: 'SharePoint+Zscaler', domains: ['SharePoint', 'Zscaler'],
    title: 'MFA enforcement extended to all non-admin users', fp_score: 0.04,
    source_policy: 'MIG-POL-002 v5.1 §4.1', source_technical: 'ZPA-AUTHPOL-ALL-USERS-MFA',
    finding: 'ZPA authentication policy now requires MFA for every user, not just privileged admins. Closed via CR-20260531-MFA006.',
    impact: 'Resolved — MFA universally enforced per MIG-POL-002 §4.1.',
    remediation: ['Auth policy scope widened to all users', 'Rollout verified for the 4,200 non-admin accounts'],
    policy_mandates: ['MIG-POL-002-AM04'], regulatory: ['SOC 2 CC6.1'],
    detected_at: new Date(Date.now() - 58 * 3600000).toISOString(),
    owner_team: 'platform-security', consumer_team: 'app-dev', platform_team: 'platform-security', tags: ['identity'] },
  { conflict_id: 'ARBITER-UC24', severity: 'MEDIUM', status: 'RESOLVED', compliant: false, conflict_type: 'DRIFT', type: 'CROSS_DOMAIN',
    domain: 'DATA_GOVERNANCE', source_pair: 'SharePoint+AWS Config', domains: ['SharePoint', 'AWSConfig'],
    title: 'Cross-region replication retargeted in-region (us-west-2)', fp_score: 0.03,
    source_policy: 'MIG-POL-003 v2.2 §3', source_technical: 'mig-prod-actuarial-data',
    finding: 'Replication target moved from eu-west-1 to us-west-2, restoring US data-residency. Closed via CR-20260528-RES004.',
    impact: 'Resolved — customer data remains within the continental US.',
    remediation: ['Replication rule retargeted to us-west-2', 'eu-west-1 replica purged', 'Residency Config rule passing'],
    policy_mandates: ['MIG-POL-003-DG03'], regulatory: ['NAIC MDL-668 §3'],
    detected_at: new Date(Date.now() - 70 * 3600000).toISOString(),
    owner_team: 'data-governance', consumer_team: 'cloud-infra', platform_team: 'cloud-infra', tags: ['data-residency', 'infrastructure'] },
  { conflict_id: 'ARBITER-UC25', severity: 'LOW', status: 'RESOLVED', compliant: false, conflict_type: 'CONTRADICTION', type: 'CROSS_DOMAIN',
    domain: 'VENDOR_MGMT', source_pair: 'SharePoint+Zscaler', domains: ['SharePoint', 'Zscaler'],
    title: 'Vendor geo allow-list corrected to the approved country set', fp_score: 0.06,
    source_policy: 'MIG-POL-003 v2.2 §4', source_technical: 'ZPA-GEO-RESTRICT-APPROVED',
    finding: 'ZTNA geo restriction expanded from India/US-only to the full approved-country list (US, IN, UK, SG, DE, AU, PH, CA). Closed via CR-20260526-GEO003.',
    impact: 'Resolved — vendor access matches MIG-POL-003 §4 approved countries.',
    remediation: ['Geo allow-list aligned to the approved-vendor register', 'Change verified with vendor management'],
    policy_mandates: ['MIG-POL-003-VM04'], regulatory: ['ISO 27001 A.5.19'],
    detected_at: new Date(Date.now() - 88 * 3600000).toISOString(),
    owner_team: 'vendor-mgmt', consumer_team: 'app-dev', platform_team: 'network-eng', tags: ['vendor', 'network'] },
  { conflict_id: 'ARBITER-UC26', severity: 'LOW', status: 'RESOLVED', compliant: false, conflict_type: 'GAP', type: 'CROSS_DOMAIN',
    domain: 'COMPLIANCE', source_pair: 'SharePoint+Zscaler', domains: ['SharePoint', 'Zscaler'],
    title: 'SSL inspection exception register backfilled', fp_score: 0.05,
    source_policy: 'MIG-POL-002 v5.1 §2.2', source_technical: 'ZIA-SSL-EXCEPTION-REGISTER',
    finding: 'The 47 undocumented SSL bypass domains were reviewed; valid ones registered with 90-day expiry, the rest removed. Closed via CR-20260524-SSL002.',
    impact: 'Resolved — all SSL inspection exceptions documented and time-bound.',
    remediation: ['Exception register populated', 'Stale bypasses removed', '90-day expiry enforced'],
    policy_mandates: ['MIG-POL-002-CP02'], regulatory: ['SOC 2 CC7.2'],
    detected_at: new Date(Date.now() - 110 * 3600000).toISOString(),
    owner_team: 'platform-security', consumer_team: 'cloud-infra', platform_team: 'platform-security', tags: ['infrastructure'] },
]

// Final export — RAW_CONFLICTS rows enriched with scanner schema fields + ownership,
// plus the fully-shaped EXTRA_CONFLICTS (demo volume) appended verbatim.
export const MOCK_CONFLICTS = [
  ...RAW_CONFLICTS.map(c => {
    const merged = { ...c, ...(UC_ENRICHMENT[c.conflict_id] || {}) }
    return { ...ownershipForRule(merged.rule_key), ...merged }
  }),
  ...EXTRA_CONFLICTS,
]

// 14 compliant alignments the scanner records as positive evidence of working
// controls. Heat map cells aggregate these alongside conflicts (compliant=true).
// Used as the false-positive guard: scanner must NOT flag any of these as conflicts.
const MOCK_COMPLIANT_RAW = [
  { conflict_id: 'COMPLIANT-UC01-BOX', rule_key: 'UC01', compliant: true, domain: 'ACCESS_MGMT', source_pair: 'SharePoint+Zscaler', domains: ['SharePoint','Zscaler'], title: 'Box.com approved and accessible — policy ↔ enforcement aligned', detected_at: new Date(Date.now() - 90 * 60000).toISOString() },
  { conflict_id: 'COMPLIANT-UC02-BEYONDTRUST', rule_key: 'UC02', compliant: true, domain: 'VENDOR_MGMT', source_pair: 'SharePoint+Zscaler', domains: ['SharePoint','Zscaler'], title: 'BeyondTrust Remote Support whitelisted and SIEM-logged', detected_at: new Date(Date.now() - 100 * 60000).toISOString() },
  { conflict_id: 'COMPLIANT-UC03-CHROME', rule_key: 'UC03', compliant: true, domain: 'ACCESS_MGMT', source_pair: 'SharePoint+Zscaler', domains: ['SharePoint','Zscaler'], title: 'Chrome Enterprise permitted — browser policy aligned', detected_at: new Date(Date.now() - 110 * 60000).toISOString() },
  { conflict_id: 'COMPLIANT-UC04-HEALTHCARE', rule_key: 'UC04', compliant: true, domain: 'COMPLIANCE', source_pair: 'SharePoint+Zscaler', domains: ['SharePoint','Zscaler'], title: 'Healthcare category SSL-inspected per policy', detected_at: new Date(Date.now() - 120 * 60000).toISOString() },
  { conflict_id: 'COMPLIANT-UC04-GOV', rule_key: 'UC04', compliant: true, domain: 'COMPLIANCE', source_pair: 'SharePoint+Zscaler', domains: ['SharePoint','Zscaler'], title: 'Government category SSL-inspected per policy', detected_at: new Date(Date.now() - 130 * 60000).toISOString() },
  { conflict_id: 'COMPLIANT-UC05-ADMIN', rule_key: 'UC05', compliant: true, domain: 'ACCESS_MGMT', source_pair: 'SharePoint+Zscaler', domains: ['SharePoint','Zscaler'], title: 'Privileged Admin MFA actively enforced (sub-control compliant)', detected_at: new Date(Date.now() - 140 * 60000).toISOString() },
  { conflict_id: 'COMPLIANT-UC06-PRINTERS', rule_key: 'UC06', compliant: true, domain: 'NETWORK_SECURITY', source_pair: 'SharePoint+Zscaler', domains: ['SharePoint','Zscaler'], title: 'VLAN-12 managed printers blocked from external — IoT policy aligned', detected_at: new Date(Date.now() - 150 * 60000).toISOString() },
  { conflict_id: 'COMPLIANT-UC07-API002', rule_key: 'UC07', compliant: true, domain: 'CLOUD_SECURITY', source_pair: 'SharePoint+AWS Config', domains: ['SharePoint','AWSConfig'], title: 'alb-mig-prod-api-002 protected by AWS WAF + OWASP CRS', detected_at: new Date(Date.now() - 160 * 60000).toISOString() },
  { conflict_id: 'COMPLIANT-UC07-PORTAL003', rule_key: 'UC07', compliant: true, domain: 'CLOUD_SECURITY', source_pair: 'SharePoint+AWS Config', domains: ['SharePoint','AWSConfig'], title: 'alb-mig-prod-portal-003 protected by AWS WAF + OWASP CRS', detected_at: new Date(Date.now() - 170 * 60000).toISOString() },
  { conflict_id: 'COMPLIANT-UC08-TGW', rule_key: 'UC08', compliant: true, domain: 'NETWORK_SECURITY', source_pair: 'SharePoint+AWS Config', domains: ['SharePoint','AWSConfig'], title: 'Cross-prod-account routing via Transit Gateway — segmentation preserved', detected_at: new Date(Date.now() - 180 * 60000).toISOString() },
  { conflict_id: 'COMPLIANT-UC09-USREP', rule_key: 'UC09', compliant: true, domain: 'DATA_GOVERNANCE', source_pair: 'SharePoint+AWS Config', domains: ['SharePoint','AWSConfig'], title: 'mig-prod-customer-data-secondary replication us-east-1 → us-west-2 (in-region)', detected_at: new Date(Date.now() - 190 * 60000).toISOString() },
  { conflict_id: 'COMPLIANT-UC10-INTERNAL', rule_key: 'UC10', compliant: true, domain: 'DATA_GOVERNANCE', source_pair: 'SharePoint+Zscaler', domains: ['SharePoint','Zscaler'], title: 'Internal-only data flows correctly unblocked by DLP', detected_at: new Date(Date.now() - 200 * 60000).toISOString() },
  { conflict_id: 'COMPLIANT-UC11-US', rule_key: 'UC11', compliant: true, domain: 'VENDOR_MGMT', source_pair: 'SharePoint+Zscaler', domains: ['SharePoint','Zscaler'], title: 'US-based vendor access permitted by ZTNA — country list compliant', detected_at: new Date(Date.now() - 210 * 60000).toISOString() },
  { conflict_id: 'COMPLIANT-UC12-GENERAL', rule_key: 'UC12', compliant: true, domain: 'ACCESS_MGMT', source_pair: 'SharePoint+Zscaler', domains: ['SharePoint','Zscaler'], title: 'General employee social-media block applied as intended', detected_at: new Date(Date.now() - 220 * 60000).toISOString() },
  { conflict_id: 'COMPLIANT-UC13-MGMTDENY', rule_key: 'UC13', compliant: true, domain: 'NETWORK_SECURITY', source_pair: 'SharePoint+Palo Alto', domains: ['SharePoint','PaloAlto'], title: 'PAN-SEC-MGMT-DENY-EXTERNAL denies management-plane access from the internet — egress policy aligned', detected_at: new Date(Date.now() - 230 * 60000).toISOString() },
  { conflict_id: 'COMPLIANT-UC14-MALWARE', rule_key: 'UC14', compliant: true, domain: 'NETWORK_SECURITY', source_pair: 'Zscaler+Palo Alto', domains: ['Zscaler','PaloAlto'], title: 'Zscaler and Palo Alto both block the Malware/Botnet category — enforcement consistent', detected_at: new Date(Date.now() - 240 * 60000).toISOString() },
]
export const MOCK_COMPLIANT = MOCK_COMPLIANT_RAW.map(c => ({ ...ownershipForRule(c.rule_key), ...c }))

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
  const domains = ['SharePoint', 'Zscaler', 'AWSConfig', 'PaloAlto']
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

// Heat map (the one the docs require): 6 domains × 2 source pairs, cell count = #conflicts.
// Compliant rows are intentionally excluded from the cell counts; pass only conflicts.
export const DOMAIN_LABELS = {
  ACCESS_MGMT:      'Access Mgmt',
  NETWORK_SECURITY: 'Network Security',
  DATA_GOVERNANCE:  'Data Governance',
  CLOUD_SECURITY:   'Cloud Security',
  COMPLIANCE:       'Compliance',
  VENDOR_MGMT:      'Vendor Mgmt',
}
export const DOMAIN_KEYS = Object.keys(DOMAIN_LABELS)
export const SOURCE_PAIRS = ['SharePoint+Zscaler', 'SharePoint+AWS Config', 'SharePoint+Palo Alto', 'Zscaler+Palo Alto']

export function buildDomainSourceMatrix(findings) {
  // matrix[domainKey][sourcePair] = count
  const matrix = {}
  DOMAIN_KEYS.forEach(d => {
    matrix[d] = {}
    SOURCE_PAIRS.forEach(s => { matrix[d][s] = 0 })
  })
  findings.forEach(f => {
    if (f.compliant) return
    const d = f.domain
    const s = f.source_pair
    if (matrix[d] && matrix[d][s] !== undefined) matrix[d][s]++
  })
  return matrix
}

// Filter helpers used by the Findings page (domain, conflict_type, framework).
export function filterByDomain(findings, domain) {
  return domain ? findings.filter(f => f.domain === domain) : findings
}
export function filterByConflictType(findings, type) {
  return type ? findings.filter(f => f.conflict_type === type) : findings
}

// Findings → CSV blob (used by the Findings page Export button).
// Produces a flat single-row-per-finding CSV with structured fields JSON-encoded.
export function findingsToCsv(findings) {
  const headers = [
    'conflict_id','severity','status','domain','conflict_type','source_pair',
    'title','policy_citations','enforcement_evidence','regulatory','detected_at',
  ]
  const escape = v => {
    if (v == null) return ''
    const s = typeof v === 'string' ? v : JSON.stringify(v)
    return '"' + s.replace(/"/g, '""') + '"'
  }
  const rows = [headers.join(',')]
  findings.forEach(f => {
    rows.push([
      f.conflict_id, f.severity, f.status, f.domain, f.conflict_type, f.source_pair,
      f.title, f.policy_citations, f.enforcement_evidence, f.regulatory, f.detected_at,
    ].map(escape).join(','))
  })
  return rows.join('\n')
}

// ── Token Tracking mock data (CISO-only Governance tab) ──────────────────────
// 30 days of synthetic per-invocation records mirroring the shape the live DDB
// table will return. Generated with a seeded PRNG so reloads are stable; the
// time window slides with Date.now() so "today" always feels current. Skew is
// deliberate — see PERSONAS / SPECIALIST_PROB / dayOfWeek inside.

export const NOVA_LITE_MODEL_ID = 'us.amazon.nova-2-lite-v1:0'
// Bedrock list pricing per 1M tokens. MUST mirror agents/_shared/token_usage.py
// — both files independently compute estimated_cost (the backend writes it onto
// each row at PutItem; the mock generator computes it client-side). Keep keys
// and rates identical when adding a model.
export const MODEL_PRICING = {
  // Amazon Nova 2 Lite — specialist agents
  [NOVA_LITE_MODEL_ID]:                          { input: 0.06, output: 0.24 },
  // Anthropic Claude Sonnet 4.6 — master_orchestrator in this deploy. Two keys
  // because the backend writes whichever string MODEL_ID env var holds.
  'us.anthropic.claude-sonnet-4-6':              { input: 3.00, output: 15.00 },
  'anthropic.claude-sonnet-4-6-20251006-v1:0':   { input: 3.00, output: 15.00 },
}

function _mulberry32(seed) {
  return function() {
    seed = (seed + 0x6D2B79F5) | 0
    let t = seed
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

const _TOKEN_PERSONAS = [
  { id: 'ciso',     email: 'ciso_diana@meridianinsurance.com',  weight: 0.30 },
  { id: 'soc',      email: 'soc_marcus@meridianinsurance.com',  weight: 0.32 },
  { id: 'grc',      email: 'grc_priya@meridianinsurance.com',   weight: 0.25 },
  { id: 'employee', email: 'emp_sarah@meridianinsurance.com',   weight: 0.13 },
]

// Specialist fan-out probability per chat. Sharepoint dominates (policy lookups
// are the first move); awsconfig + zscaler are situational.
const _SPECIALIST_PROB = { sharepoint: 0.85, awsconfig: 0.55, zscaler: 0.45 }

function _costFor(model, inTok, outTok) {
  const p = MODEL_PRICING[model] || { input: 0, output: 0 }
  return Number(((inTok * p.input + outTok * p.output) / 1_000_000).toFixed(6))
}

function _buildRecord(tsMs, agent, persona, sessionId, inTok, outTok, blocked) {
  return {
    pk: `persona#${persona.id}`,
    sk: `ts#${new Date(tsMs).toISOString()}#${sessionId}#${agent}`,
    timestamp: new Date(tsMs).toISOString(),
    agent,
    persona: persona.id,
    user_email: persona.email,
    session_id: sessionId,
    model_id: NOVA_LITE_MODEL_ID,
    input_tokens: inTok,
    output_tokens: outTok,
    total_tokens: inTok + outTok,
    estimated_cost: _costFor(NOVA_LITE_MODEL_ID, inTok, outTok),
    guardrail_blocked: blocked,
    chat_type: 'analyst',
    ttl: Math.floor(tsMs / 1000) + 90 * 24 * 3600,
  }
}

function _generateTokenUsage() {
  const rng = _mulberry32(42)
  const cum = []; let acc = 0
  for (const p of _TOKEN_PERSONAS) { acc += p.weight; cum.push(acc) }
  const pickPersona = () => {
    const r = rng()
    for (let i = 0; i < cum.length; i++) if (r < cum[i]) return _TOKEN_PERSONAS[i]
    return _TOKEN_PERSONAS[_TOKEN_PERSONAS.length - 1]
  }
  const records = []
  const now = Date.now()
  const DAY_MS = 24 * 3600 * 1000
  for (let d = 0; d < 30; d++) {
    const dayStart = now - (29 - d) * DAY_MS
    const dow = new Date(dayStart).getDay()           // 0=Sun, 6=Sat
    const weekend = dow === 0 || dow === 6
    const chats = weekend ? 12 + Math.floor(rng() * 8) : 40 + Math.floor(rng() * 25)
    for (let c = 0; c < chats; c++) {
      const persona = pickPersona()
      // Working-hours bias — 75% of traffic 08:00–18:00 local.
      const hour = rng() < 0.75 ? 8 + Math.floor(rng() * 11) : Math.floor(rng() * 24)
      const minute = Math.floor(rng() * 60)
      const second = Math.floor(rng() * 60)
      const ts = dayStart + hour * 3600_000 + minute * 60_000 + second * 1000
      const sessionId = `sess_${ts.toString(36)}_${Math.floor(rng() * 1e6).toString(36)}`
      // Master row for every chat. ~3% are guardrail-blocked — input is billed,
      // output is zero, and no fan-out happens.
      const blocked = rng() < 0.03
      const mIn = 700 + Math.floor(rng() * 600)
      const mOut = blocked ? 0 : 300 + Math.floor(rng() * 500)
      records.push(_buildRecord(ts, 'master', persona, sessionId, mIn, mOut, blocked))
      if (blocked) continue
      // Specialist fan-out — independent coin flips, staggered timestamps.
      let lag = 200
      for (const [spec, prob] of Object.entries(_SPECIALIST_PROB)) {
        if (rng() < prob) {
          const sIn = 400 + Math.floor(rng() * 400)
          const sOut = 150 + Math.floor(rng() * 300)
          records.push(_buildRecord(ts + lag, spec, persona, sessionId, sIn, sOut, false))
          lag += 250
        }
      }
    }
  }
  // Newest-first so the table renders naturally without sort UI work.
  records.sort((a, b) => (a.timestamp < b.timestamp ? 1 : -1))
  return records
}

export const MOCK_TOKEN_USAGE = _generateTokenUsage()

// ─── ServiceNow change-impact analysis (mock) ───────────────────────────
// Mirrors the /servicenow/impact-analysis response shape (CMDB facts + the
// grafted approver chain). The affected-CI graph matches the seeded demo CMDB
// in scripts/seed_servicenow_cmdb.py so mock and live modes look the same.
const _IMPACT_FIXTURES = {
  'alb-mig-prod-claims-api-001': {
    changed_resource: { name: 'alb-mig-prod-claims-api-001', class: 'cmdb_ci_lb', correlation_id: 'arn:aws:elasticloadbalancing:us-east-1:669810405473:loadbalancer/app/alb-mig-prod-claims-api-001' },
    owner_team: 'Cloud Infrastructure',
    affected_cis: [
      { name: 'Claims API', class: 'cmdb_ci_appl', depth: 1, via: 'Depends on::Used by', direction: 'downstream' },
    ],
  },
  'mig-prod-claims-data-primary': {
    changed_resource: { name: 'mig-prod-claims-data-primary', class: 'cmdb_ci_db_instance', correlation_id: 'arn:aws:rds:us-east-1:669810405473:db:mig-prod-claims-data-primary' },
    owner_team: 'Data Governance',
    affected_cis: [
      { name: 'Claims API', class: 'cmdb_ci_appl', depth: 1, via: 'Depends on::Used by', direction: 'downstream' },
    ],
  },
  'pcx-mig-prod-dev-001': {
    changed_resource: { name: 'pcx-mig-prod-dev-001', class: 'cmdb_ci_network', correlation_id: 'arn:aws:ec2:us-east-1:669810405473:vpc-peering-connection/pcx-mig-prod-dev-001' },
    owner_team: 'Network Engineering',
    affected_cis: [
      { name: 'vpc-mig-prod-001', class: 'cmdb_ci_network', depth: 1, via: 'Connects to::Connected by', direction: 'downstream' },
      { name: 'vpc-mig-dev-002', class: 'cmdb_ci_network', depth: 1, via: 'Connects to::Connected by', direction: 'downstream' },
    ],
  },
}

function _mockApproverChain(env, severity) {
  const e = (env || '').toUpperCase(), s = (severity || '').toUpperCase()
  if (e === 'DEV') return []
  if (e === 'STAGING') return [{ role: 'team_lead', email: 'team-lead@meridianinsurance.com', status: 'PENDING', description: 'Team Lead approval required for STAGING' }]
  if (e === 'PRE_PROD') return [
    { role: 'manager', email: 'manager@meridianinsurance.com', status: 'PENDING', description: 'Manager approval required for PRE_PROD' },
    { role: 'owning_team_lead', email: 'owning-team-lead@meridianinsurance.com', status: 'PENDING', description: 'Owning Team Lead approval required for PRE_PROD' },
  ]
  const chain = [
    { role: 'ciso', email: 'ciso_diana@meridianinsurance.com', status: 'PENDING', description: 'CISO approval required for PROD' },
    { role: 'vp_security', email: 'vp-security@meridianinsurance.com', status: 'PENDING', description: 'VP Security approval required for PROD' },
  ]
  if (s === 'CRITICAL' || s === 'HIGH') chain.push({ role: 'legal', type: 'NOTIFICATION', email: 'legal@meridianinsurance.com', status: 'NOTIFIED', description: 'Legal notified of regulatory impact' })
  return chain
}

export function mockImpactAnalysis({ resource, target_environment = 'PROD', severity = 'HIGH', draft_change = false }) {
  const fx = _IMPACT_FIXTURES[resource]
  const cab = (target_environment || '').toUpperCase() === 'PROD' || ['CRITICAL', 'HIGH'].includes((severity || '').toUpperCase())
  const out = {
    configured: true,
    changed_resource: fx ? { input: resource, ...fx.changed_resource } : { input: resource, name: resource },
    affected_cis: fx ? fx.affected_cis : [],
    owner_team: fx ? fx.owner_team : 'unassigned',
    cab_required: cab,
    target_environment, severity,
    approver_chain: _mockApproverChain(target_environment, severity),
    note: fx ? undefined : `(mock) No seeded CMDB CI for '${resource}'.`,
  }
  if (draft_change) {
    const n = 30000 + (resource.length * 7) % 900
    out.change = { number: `CHG00${n}`, sys_id: `mock${n}`, url: '#' }
    out.affected_attached = out.affected_cis.length
  }
  return out
}

// Token records → CSV blob (used by the Token Tracking page Export button).
export function tokenUsageToCsv(records) {
  const headers = [
    'timestamp','agent','persona','user_email','session_id','model_id',
    'input_tokens','output_tokens','total_tokens','estimated_cost','guardrail_blocked',
  ]
  const escape = v => {
    if (v == null) return ''
    const s = typeof v === 'string' ? v : String(v)
    return '"' + s.replace(/"/g, '""') + '"'
  }
  const rows = [headers.join(',')]
  records.forEach(r => {
    rows.push([
      r.timestamp, r.agent, r.persona, r.user_email, r.session_id, r.model_id,
      r.input_tokens, r.output_tokens, r.total_tokens, r.estimated_cost, r.guardrail_blocked,
    ].map(escape).join(','))
  })
  return rows.join('\n')
}

// ── Reports (mock) ───────────────────────────────────────────────────────────
// Mirrors Infra/functions/api_handler/report_catalog.py so the Reports page
// renders the same catalog with USE_MOCK. In mock mode we can't run reportlab /
// openpyxl, so mockGenerateReport builds a real client-side CSV/JSON download and
// falls back to a JSON data export for pdf/xlsx/zip (the deployed backend produces
// the true binary formats).
export const MOCK_REPORT_CATEGORIES = ['Compliance', 'Risk', 'Audit']

export const MOCK_REPORT_CATALOG = [
  { id: 'executive_compliance', title: 'Executive Compliance Briefing', category: 'Compliance', audience: 'CISO, Board, Executive Risk Committee', formats: ['pdf'], default_format: 'pdf', icon: 'FileText', estimated_seconds: 3, parameters: [], tags: ['board', 'summary'],
    description: 'Board-ready summary: overall score, per-framework breakdown, and the top open risks. Reflects current posture at the moment of generation.' },
  { id: 'technical_compliance', title: 'Technical Compliance Report', category: 'Compliance', audience: 'QSA, External Auditor, GRC Analyst', formats: ['pdf', 'xlsx'], default_format: 'pdf', icon: 'FileSpreadsheet', estimated_seconds: 5,
    parameters: [
      { id: 'frameworks', label: 'Frameworks', type: 'multi_select', default: ['naic', 'pci-dss', 'sox', 'nist', 'iso27001'], options: [{ id: 'naic', label: 'NAIC MDL-668' }, { id: 'pci-dss', label: 'PCI-DSS 4.0' }, { id: 'sox', label: 'SOX' }, { id: 'nist', label: 'NIST CSF 2.0' }, { id: 'iso27001', label: 'ISO 27001:2022' }] },
    ], tags: ['audit-handoff'],
    description: 'Per-framework control posture: every control, its PASS/FAIL state, the linked conflict, severity and status.' },
  { id: 'conflict_register', title: 'Conflict Register', category: 'Risk', audience: 'GRC Analyst, Risk Owner', formats: ['csv', 'xlsx', 'json'], default_format: 'csv', icon: 'Table', estimated_seconds: 2,
    parameters: [
      { id: 'severity', label: 'Severity', type: 'multi_select', default: ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'], options: [{ id: 'CRITICAL', label: 'Critical' }, { id: 'HIGH', label: 'High' }, { id: 'MEDIUM', label: 'Medium' }, { id: 'LOW', label: 'Low' }] },
      { id: 'status', label: 'Status', type: 'multi_select', default: ['OPEN', 'IN_REVIEW', 'RESOLVED'], options: [{ id: 'OPEN', label: 'Open' }, { id: 'IN_REVIEW', label: 'In review' }, { id: 'RESOLVED', label: 'Resolved' }] },
    ], tags: ['register', 'inventory'],
    description: 'The full conflict inventory — id, severity, status, domains, regulatory mappings — as a flat export.' },
  { id: 'audit_trail', title: 'Audit Trail Export', category: 'Audit', audience: 'External Auditor, Regulator', formats: ['csv', 'json'], default_format: 'csv', icon: 'ScrollText', estimated_seconds: 2, parameters: [], tags: ['audit', 'evidence'],
    description: 'Chronological audit-log events — scans, change requests, approvals, ingestion — for evidence and forensics.' },
  { id: 'evidence_package', title: 'Evidence Package', category: 'Audit', audience: 'External Auditor, QSA, Regulator', formats: ['zip'], default_format: 'zip', icon: 'Package', estimated_seconds: 6,
    parameters: [
      { id: 'frameworks', label: 'Frameworks', type: 'multi_select', default: ['naic', 'pci-dss', 'sox', 'nist', 'iso27001'], options: [{ id: 'naic', label: 'NAIC MDL-668' }, { id: 'pci-dss', label: 'PCI-DSS 4.0' }, { id: 'sox', label: 'SOX' }, { id: 'nist', label: 'NIST CSF 2.0' }, { id: 'iso27001', label: 'ISO 27001:2022' }] },
    ], tags: ['audit-ready', 'complete'],
    description: 'ZIP bundle: technical compliance PDF, conflict register CSV, audit-trail JSON and a scores snapshot.' },
]

const _REPORT_TITLES = Object.fromEntries(MOCK_REPORT_CATALOG.map(r => [r.id, r.title]))

function _csvCell(v) {
  if (v == null) return ''
  const s = Array.isArray(v) ? v.join('; ') : String(v)
  return '"' + s.replace(/"/g, '""') + '"'
}

function _conflictsCsv(conflicts) {
  const head = ['conflict_id', 'severity', 'status', 'title', 'domains', 'regulatory', 'detected_at']
  const rows = [head.join(',')]
  conflicts.forEach(c => rows.push([c.conflict_id, c.severity, c.status, c.title, c.domains, c.regulatory, c.detected_at].map(_csvCell).join(',')))
  return rows.join('\n')
}

function _auditCsv(events) {
  const head = ['timestamp', 'action_type', 'resource', 'user', 'status', 'details']
  const rows = [head.join(',')]
  events.forEach(e => rows.push([e.timestamp, e.action_type, e.resource, e.user, e.status,
    typeof e.details === 'string' ? e.details : JSON.stringify(e.details || {})].map(_csvCell).join(',')))
  return rows.join('\n')
}

// Client-side report generation for USE_MOCK. Returns the same payload shape as
// the live POST /reports/generate (download_url ready to download).
export function mockGenerateReport(reportId, format, _params) {
  const spec = MOCK_REPORT_CATALOG.find(r => r.id === reportId)
  const fmt = (format || spec?.default_format || 'json').toLowerCase()
  const conflicts = MOCK_CONFLICTS.filter(c => !c.compliant)
  const ts = new Date().toISOString().replace(/[:.]/g, '-')

  let content, mime, ext
  if (reportId === 'audit_trail') {
    if (fmt === 'csv') { content = _auditCsv(MOCK_AUDIT); mime = 'text/csv'; ext = 'csv' }
    else { content = JSON.stringify({ events: MOCK_AUDIT }, null, 2); mime = 'application/json'; ext = 'json' }
  } else if (reportId === 'conflict_register') {
    if (fmt === 'csv' || fmt === 'xlsx') { content = _conflictsCsv(conflicts); mime = 'text/csv'; ext = 'csv' }
    else { content = JSON.stringify({ count: conflicts.length, conflicts }, null, 2); mime = 'application/json'; ext = 'json' }
  } else {
    // executive / technical / evidence_package → JSON data export in mock mode.
    content = JSON.stringify({
      report: reportId,
      note: 'Mock export — deploy the backend for the true PDF / XLSX / ZIP output.',
      frameworks: [
        { id: 'naic', name: 'NAIC MDL-668', score: 65 }, { id: 'pci-dss', name: 'PCI-DSS 4.0', score: 73 },
        { id: 'sox', name: 'SOX', score: 74 }, { id: 'nist', name: 'NIST CSF 2.0', score: 63 },
        { id: 'iso27001', name: 'ISO 27001:2022', score: 63 },
      ],
      conflicts,
    }, null, 2)
    mime = 'application/json'; ext = 'json'
  }

  const blob = new Blob([content], { type: mime })
  const filename = `${reportId}-${ts}.${ext}`
  return {
    report_type: reportId, report_title: _REPORT_TITLES[reportId] || reportId,
    format: ext, filename, size_bytes: blob.size,
    download_url: URL.createObjectURL(blob), report_url: URL.createObjectURL(blob),
    expires_in: 0, generated_at: new Date().toISOString(), mock: true,
  }
}
