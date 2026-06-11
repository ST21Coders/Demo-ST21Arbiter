const UC_REF_RE = /ARBITER-UC\d+/

export const FRAMEWORKS = [
  {
    id: 'naic',
    name: 'NAIC MDL-668',
    score: 65,
    accent: '#4f46e5',
    controls: [
      { id: 'MDL-668 §3', name: 'Data Residency', note: 'ARBITER-UC09: claims data replicating to eu-west-1', defaultStatus: 'FAIL' },
      { id: 'MDL-668 §4', name: 'Authorised Transfers', note: 'ARBITER-UC10: DLP blocking authorised vendor transfers', defaultStatus: 'FAIL' },
      { id: 'MDL-668 §5', name: 'Universal MFA Coverage', note: 'ARBITER-UC05: MFA limited to admins', defaultStatus: 'FAIL' },
      { id: 'MDL-668 §6', name: 'Approved SaaS Access', note: 'ARBITER-UC01: approved cloud storage blocked', defaultStatus: 'FAIL' },
      { id: 'MDL-668 §7', name: 'Vendor Remote Support', note: 'ARBITER-UC02: approved vendor tools blocked', defaultStatus: 'FAIL' },
      { id: 'MDL-668 §8', name: 'Third-Party Country Access', note: 'ARBITER-UC11: ZTNA geo restriction blocks approved vendors', defaultStatus: 'FAIL' },
      { id: 'MDL-668 §9', name: 'Enforcement Consistency', note: 'ARBITER-UC14: anonymizer enforcement bypass across tools', defaultStatus: 'FAIL' },
    ],
  },
  {
    id: 'pci-dss',
    name: 'PCI-DSS 4.0',
    score: 73,
    accent: '#0284c7',
    controls: [
      { id: '4.1', name: 'SSL/TLS Inspection', note: 'ARBITER-UC04: SSL bypass for finance domains', defaultStatus: 'FAIL' },
      { id: '8.4', name: 'MFA Coverage', note: 'ARBITER-UC05: MFA limited to admins', defaultStatus: 'FAIL' },
      { id: '6.2', name: 'Approved SaaS Access', note: 'ARBITER-UC01: approved business app blocked by Zscaler', defaultStatus: 'FAIL' },
      { id: '6.3', name: 'Vendor Tool Access', note: 'ARBITER-UC02: remote support tools blocked for vendors', defaultStatus: 'FAIL' },
      { id: '1.2', name: 'IoT Enforcement', note: 'ARBITER-UC06: IoT devices in monitor-only mode', defaultStatus: 'FAIL' },
      { id: '6.4', name: 'Browser Policy Alignment', note: 'ARBITER-UC03: Firefox blocked despite policy approval', defaultStatus: 'FAIL' },
    ],
  },
  {
    id: 'sox',
    name: 'SOX',
    score: 74,
    accent: '#059669',
    controls: [
      { id: 'SOX ITGC-1', name: 'Public Entry Controls', note: 'ARBITER-UC07: production ALB missing WAF', defaultStatus: 'FAIL' },
      { id: 'SOX ITGC-2', name: 'Production Segmentation', note: 'ARBITER-UC08: dev-to-prod VPC peering violates segmentation', defaultStatus: 'FAIL' },
      { id: 'SOX ITGC-3', name: 'Regulated Data Residency', note: 'ARBITER-UC09: claims data replicating out of region', defaultStatus: 'FAIL' },
      { id: 'SOX ITGC-4', name: 'Department Exceptions', note: 'ARBITER-UC12: social media exemptions not enforced', defaultStatus: 'FAIL' },
    ],
  },
  {
    id: 'nist',
    name: 'NIST CSF 2.0',
    score: 63,
    accent: '#d97706',
    controls: [
      { id: 'PR.AA-01', name: 'MFA Coverage', note: 'ARBITER-UC05: MFA enforcement limited to admins', defaultStatus: 'FAIL' },
      { id: 'PR.PS-01', name: 'Public Workload Protection', note: 'ARBITER-UC07: production ALB exposed without WAF', defaultStatus: 'FAIL' },
      { id: 'PR.DS-01', name: 'Cross-Region Data Controls', note: 'ARBITER-UC09: claims data replicating to eu-west-1', defaultStatus: 'FAIL' },
      { id: 'PR.AA-02', name: 'Approved Cloud Storage', note: 'ARBITER-UC01: Dropbox Business approved but blocked', defaultStatus: 'FAIL' },
      { id: 'PR.PT-01', name: 'IoT Blocking', note: 'ARBITER-UC06: IoT devices in monitor-only mode', defaultStatus: 'FAIL' },
      { id: 'PR.IR-01', name: 'Default-Deny Egress', note: 'ARBITER-UC13: Palo Alto permits any/any outbound', defaultStatus: 'FAIL' },
      { id: 'PR.AA-03', name: 'Browser Standard', note: 'ARBITER-UC03: approved browser blocked', defaultStatus: 'FAIL' },
      { id: 'PR.AA-04', name: 'Vendor Country Access', note: 'ARBITER-UC11: approved vendor countries blocked', defaultStatus: 'FAIL' },
    ],
  },
  {
    id: 'iso27001',
    name: 'ISO 27001:2022',
    score: 63,
    accent: '#7c3aed',
    controls: [
      { id: 'A.5.15', name: 'Access Control', note: 'ARBITER-UC05: MFA enforcement limited to admins', defaultStatus: 'FAIL' },
      { id: 'A.8.20', name: 'Network Security', note: 'ARBITER-UC08: production segmentation failure', defaultStatus: 'FAIL' },
      { id: 'A.5.23', name: 'Cloud Services', note: 'ARBITER-UC09: regulated data replicated cross-region', defaultStatus: 'FAIL' },
      { id: 'A.5.10', name: 'Use of Information Assets', note: 'ARBITER-UC02: approved remote support tools blocked', defaultStatus: 'FAIL' },
      { id: 'A.5.34', name: 'Privacy and PII Protection', note: 'ARBITER-UC10: authorised data transfers blocked by DLP', defaultStatus: 'FAIL' },
      { id: 'A.8.22', name: 'Segregation of Networks', note: 'ARBITER-UC13: firewall egress permits any/any', defaultStatus: 'FAIL' },
      { id: 'A.8.1', name: 'User Endpoint Devices', note: 'ARBITER-UC03: approved browser blocked by enforcement', defaultStatus: 'FAIL' },
      { id: 'A.5.19', name: 'Supplier Relationships', note: 'ARBITER-UC11: approved vendor countries blocked', defaultStatus: 'FAIL' },
    ],
  },
]

export const SCORE_TREND_SERIES = [
  { key: 'naic', name: 'NAIC MDL-668', color: '#4f46e5' },
  { key: 'pci-dss', name: 'PCI-DSS 4.0', color: '#0284c7' },
  { key: 'sox', name: 'SOX', color: '#059669' },
  { key: 'nist', name: 'NIST CSF 2.0', color: '#d97706' },
  { key: 'iso27001', name: 'ISO 27001:2022', color: '#7c3aed' },
]

export const SCORE_TREND_POINTS = [
  { month: '2025-12', naic: 58, 'pci-dss': 64, sox: 68, nist: 59, iso27001: 60 },
  { month: '2026-01', naic: 61, 'pci-dss': 67, sox: 69, nist: 61, iso27001: 61 },
  { month: '2026-02', naic: 60, 'pci-dss': 68, sox: 71, nist: 62, iso27001: 62 },
  { month: '2026-03', naic: 63, 'pci-dss': 70, sox: 72, nist: 61, iso27001: 64 },
  { month: '2026-04', naic: 64, 'pci-dss': 71, sox: 73, nist: 62, iso27001: 62 },
  { month: '2026-05', naic: 66, 'pci-dss': 72, sox: 73, nist: 64, iso27001: 63 },
  { month: '2026-06', naic: 65, 'pci-dss': 73, sox: 74, nist: 63, iso27001: 63 },
]

export const TREND_RANGES = ['1M', '3M', '6M', '12M', 'ALL']

export function extractUC(note) {
  const match = note?.match(UC_REF_RE)
  return match ? match[0] : null
}

export function evaluateControl(ctrl, findingByUC) {
  const uc = extractUC(ctrl.note)
  const linked = uc ? findingByUC[uc] : null
  if (!linked) return { ctrl, uc, linked: null, status: ctrl.defaultStatus || 'PASS', severity: null }
  const status = linked.status === 'OPEN' || linked.status === 'IN_REVIEW' ? 'FAIL' : 'PASS'
  return { ctrl, uc, linked, status, severity: linked.severity || null }
}

export function evaluateFramework(framework, findingByUC) {
  const evals = framework.controls.map(ctrl => evaluateControl(ctrl, findingByUC))
  const open = evals.filter(e => e.status === 'FAIL')
  const severityCounts = open.reduce((acc, e) => {
    const severity = e.severity || 'HIGH'
    acc[severity] = (acc[severity] || 0) + 1
    return acc
  }, {})
  return {
    ...framework,
    evals,
    score: framework.score,
    openCount: open.length,
    criticalCount: severityCounts.CRITICAL || 0,
    highCount: severityCounts.HIGH || 0,
    mediumCount: severityCounts.MEDIUM || 0,
    lowCount: severityCounts.LOW || 0,
    passCount: evals.length - open.length,
  }
}

export function frameworkSummaries(findings) {
  const findingByUC = {}
  findings.forEach(f => { if (f.conflict_id) findingByUC[f.conflict_id] = f })
  return FRAMEWORKS.map(framework => evaluateFramework(framework, findingByUC))
}

export function filterScoreTrend(points, range) {
  if (range === 'ALL') return points
  const months = { '1M': 2, '3M': 4, '6M': 7, '12M': 13 }[range] || points.length
  return points.slice(-months)
}
