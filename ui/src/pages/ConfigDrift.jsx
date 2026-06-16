import { useMemo, useState } from 'react'
import {
  AlertTriangle, Bell, CheckCircle2, Clock, Database, RotateCcw, ShieldAlert,
} from 'lucide-react'

const STORAGE_KEY = 'arbiter.configDrift.securityGroupBaseline.v1'

const APPROVED_SECURITY_GROUPS = [
  {
    resourceId: 'sg-lm-prod-peer-dev-001',
    resourceName: 'lm-prod-peer-dev',
    vpcId: 'vpc-lm-prod-001a2b3c4d',
    environment: 'PRODUCTION',
    ingress: [
      {
        protocol: '-1',
        fromPort: -1,
        toPort: -1,
        cidr: '10.50.0.0/16',
        description: 'dev VPC CIDR',
      },
    ],
    egress: [],
  },
]

const LATEST_SECURITY_GROUPS = [
  {
    resourceId: 'sg-lm-prod-peer-dev-001',
    resourceName: 'lm-prod-peer-dev',
    vpcId: 'vpc-lm-prod-001a2b3c4d',
    environment: 'PRODUCTION',
    ingress: [
      {
        protocol: '-1',
        fromPort: -1,
        toPort: -1,
        cidr: '10.50.0.0/16',
        description: 'dev VPC CIDR',
      },
      {
        protocol: 'tcp',
        fromPort: 22,
        toPort: 22,
        cidr: '0.0.0.0/0',
        description: 'console-added temporary SSH access',
      },
    ],
    egress: [],
  },
]

function nowIso() {
  return new Date().toISOString()
}

function ruleKey(rule) {
  return [
    rule.protocol || '',
    rule.fromPort ?? '',
    rule.toPort ?? '',
    rule.cidr || '',
  ].join('|')
}

function formatPort(rule) {
  if (rule.protocol === '-1') return 'All'
  if (rule.fromPort === rule.toPort) return String(rule.fromPort)
  return `${rule.fromPort}-${rule.toPort}`
}

function formatRule(rule) {
  return `${rule.protocol || 'any'} ${formatPort(rule)} from ${rule.cidr || 'unknown'}`
}

function severityFor(rule) {
  if (rule.cidr === '0.0.0.0/0' || rule.cidr === '::/0') {
    if (rule.fromPort === 22 || rule.fromPort === 3389 || rule.protocol === '-1') return 'CRITICAL'
    return 'HIGH'
  }
  return 'MEDIUM'
}

function recommendationFor(rule) {
  if (rule.cidr === '0.0.0.0/0' || rule.cidr === '::/0') {
    return 'Revoke public ingress or replace it with an approved corporate/WAF source range.'
  }
  return 'Review whether the new source range is approved; approve baseline only if documented.'
}

function compareSecurityGroups(baseline, latest) {
  const baselineById = new Map(baseline.map(group => [group.resourceId, group]))
  const latestById = new Map(latest.map(group => [group.resourceId, group]))
  const findings = []

  latest.forEach(group => {
    const prior = baselineById.get(group.resourceId)
    if (!prior) {
      findings.push({
        id: `${group.resourceId}-new-group`,
        resourceId: group.resourceId,
        resourceName: group.resourceName,
        driftType: 'New security group',
        before: 'Not present in baseline',
        after: group.resourceName,
        severity: 'MEDIUM',
        recommendation: 'Confirm this security group is approved, tagged, and owned before updating baseline.',
      })
      return
    }

    const priorIngress = new Map((prior.ingress || []).map(rule => [ruleKey(rule), rule]))
    const latestIngress = new Map((group.ingress || []).map(rule => [ruleKey(rule), rule]))

    latestIngress.forEach((rule, key) => {
      if (priorIngress.has(key)) return
      findings.push({
        id: `${group.resourceId}-ingress-added-${key}`,
        resourceId: group.resourceId,
        resourceName: group.resourceName,
        driftType: 'Ingress rule added',
        before: 'No matching baseline rule',
        after: formatRule(rule),
        severity: severityFor(rule),
        recommendation: recommendationFor(rule),
      })
    })

    priorIngress.forEach((rule, key) => {
      if (latestIngress.has(key)) return
      findings.push({
        id: `${group.resourceId}-ingress-removed-${key}`,
        resourceId: group.resourceId,
        resourceName: group.resourceName,
        driftType: 'Ingress rule removed',
        before: formatRule(rule),
        after: 'Missing from latest observation',
        severity: 'LOW',
        recommendation: 'Confirm removal was intentional before updating baseline.',
      })
    })
  })

  baseline.forEach(group => {
    if (latestById.has(group.resourceId)) return
    findings.push({
      id: `${group.resourceId}-missing-group`,
      resourceId: group.resourceId,
      resourceName: group.resourceName,
      driftType: 'Security group missing',
      before: group.resourceName,
      after: 'Not present in latest observation',
      severity: 'HIGH',
      recommendation: 'Confirm whether the security group was deleted or the AWS Config feed is incomplete.',
    })
  })

  return findings
}

function loadBaseline() {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null')
    return parsed?.resources ? parsed : null
  } catch {
    return null
  }
}

function SeverityBadge({ severity }) {
  const cls = {
    CRITICAL: 'bg-red-50 text-red-700 border-red-200',
    HIGH: 'bg-orange-50 text-orange-700 border-orange-200',
    MEDIUM: 'bg-amber-50 text-amber-700 border-amber-200',
    LOW: 'bg-slate-50 text-slate-600 border-slate-200',
  }[severity] || 'bg-slate-50 text-slate-600 border-slate-200'
  return (
    <span className={`inline-flex rounded-full border px-2 py-0.5 text-[10px] font-bold ${cls}`}>
      {severity}
    </span>
  )
}

export default function ConfigDrift() {
  const [baseline, setBaseline] = useState(loadBaseline)
  const [driftFindings, setDriftFindings] = useState([])
  const [lastCheckAt, setLastCheckAt] = useState(null)
  const [notice, setNotice] = useState('')

  const baselineAge = useMemo(() => (
    baseline?.capturedAt ? new Date(baseline.capturedAt).toLocaleString() : 'Not captured'
  ), [baseline])

  function captureBaseline() {
    const nextBaseline = {
      capturedAt: nowIso(),
      source: 'Manual baseline capture',
      resources: APPROVED_SECURITY_GROUPS,
    }
    localStorage.setItem(STORAGE_KEY, JSON.stringify(nextBaseline))
    setBaseline(nextBaseline)
    setDriftFindings([])
    setLastCheckAt(null)
    setNotice('Baseline captured from approved security group configuration.')
  }

  function runDriftCheck() {
    if (!baseline?.resources) {
      setNotice('Capture a baseline before running a drift check.')
      return
    }
    const findings = compareSecurityGroups(baseline.resources, LATEST_SECURITY_GROUPS)
    setDriftFindings(findings)
    setLastCheckAt(nowIso())
    setNotice(findings.length ? 'Drift detected. HITL notification would be opened now.' : 'No drift detected against baseline.')
  }

  const hasBaseline = Boolean(baseline?.resources)
  const driftDetected = driftFindings.length > 0

  return (
    <div className="p-6 space-y-6 page-container">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="flex items-center gap-2 text-lg font-bold tracking-tight text-slate-900">
            <ShieldAlert size={19} className="text-indigo-600" /> Config Drift
          </h1>
          <p className="mt-1 max-w-3xl text-xs text-slate-500">
            Manual AWS Config drift workflow for security groups. Capture a known-good baseline, then compare the latest observed configuration against it.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={captureBaseline}
            className="inline-flex items-center gap-1.5 rounded-lg bg-slate-900 px-3 py-2 text-xs font-semibold text-white hover:bg-slate-800"
          >
            <Database size={13} /> Baseline
          </button>
          <button
            type="button"
            onClick={runDriftCheck}
            disabled={!hasBaseline}
            className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-2 text-xs font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-slate-200 disabled:text-slate-500"
          >
            <RotateCcw size={13} /> Drift Check
          </button>
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-3">
        <div className="rounded-lg border border-slate-200 bg-white p-4">
          <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">Baseline</p>
          <p className="mt-2 text-sm font-bold text-slate-900">{hasBaseline ? 'Captured' : 'Not captured'}</p>
          <p className="mt-1 text-xs text-slate-500">{baselineAge}</p>
        </div>
        <div className="rounded-lg border border-slate-200 bg-white p-4">
          <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">Latest Observation</p>
          <p className="mt-2 text-sm font-bold text-slate-900">{LATEST_SECURITY_GROUPS.length} security group</p>
          <p className="mt-1 text-xs text-slate-500">Demo AWS Config snapshot · local only</p>
        </div>
        <div className={`rounded-lg border p-4 ${driftDetected ? 'border-red-200 bg-red-50' : 'border-emerald-200 bg-emerald-50'}`}>
          <p className={`text-[10px] font-semibold uppercase tracking-wider ${driftDetected ? 'text-red-500' : 'text-emerald-600'}`}>Drift Status</p>
          <p className={`mt-2 flex items-center gap-1.5 text-sm font-bold ${driftDetected ? 'text-red-800' : 'text-emerald-800'}`}>
            {driftDetected ? <AlertTriangle size={14} /> : <CheckCircle2 size={14} />}
            {driftDetected ? `${driftFindings.length} change detected` : 'No active finding'}
          </p>
          <p className={`mt-1 text-xs ${driftDetected ? 'text-red-700' : 'text-emerald-700'}`}>
            {lastCheckAt ? `Last checked ${new Date(lastCheckAt).toLocaleString()}` : 'Run Drift Check after baseline capture.'}
          </p>
        </div>
      </div>

      {notice && (
        <div className={`rounded-lg border p-3 text-xs ${driftDetected ? 'border-red-200 bg-red-50 text-red-800' : 'border-slate-200 bg-white text-slate-600'}`}>
          {notice}
        </div>
      )}

      {driftDetected && (
        <div className="rounded-xl border border-red-200 bg-white shadow-sm">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-red-100 p-4">
            <div>
              <p className="text-sm font-bold text-slate-900">Detected Drift</p>
              <p className="mt-1 text-xs text-slate-500">A HITL request would be opened now. Future version can start the 10-minute revert timer.</p>
            </div>
            <span className="inline-flex items-center gap-1.5 rounded-full border border-amber-200 bg-amber-50 px-2.5 py-1 text-[10px] font-bold text-amber-700">
              <Bell size={11} /> HITL NOTIFY
            </span>
          </div>
          <div className="overflow-auto">
            <table className="min-w-full text-left text-xs">
              <thead className="bg-slate-50 text-slate-500">
                <tr>
                  <th className="px-4 py-3 font-semibold">Resource</th>
                  <th className="px-4 py-3 font-semibold">Drift Type</th>
                  <th className="px-4 py-3 font-semibold">Before</th>
                  <th className="px-4 py-3 font-semibold">After</th>
                  <th className="px-4 py-3 font-semibold">Severity</th>
                  <th className="px-4 py-3 font-semibold">Recommended Action</th>
                </tr>
              </thead>
              <tbody>
                {driftFindings.map(finding => (
                  <tr key={finding.id} className="border-t border-slate-100">
                    <td className="px-4 py-3">
                      <p className="font-semibold text-slate-900">{finding.resourceName}</p>
                      <p className="font-mono text-[10px] text-slate-400">{finding.resourceId}</p>
                    </td>
                    <td className="px-4 py-3 text-slate-700">{finding.driftType}</td>
                    <td className="max-w-[220px] px-4 py-3 text-slate-600">{finding.before}</td>
                    <td className="max-w-[220px] px-4 py-3 font-medium text-slate-900">{finding.after}</td>
                    <td className="px-4 py-3"><SeverityBadge severity={finding.severity} /></td>
                    <td className="max-w-[260px] px-4 py-3 text-slate-600">{finding.recommendation}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {!driftDetected && (
        <div className="rounded-xl border border-slate-200 bg-white p-6 text-center">
          <Clock size={22} className="mx-auto text-slate-300" />
          <p className="mt-2 text-sm font-semibold text-slate-800">Ready for manual drift checks</p>
          <p className="mx-auto mt-1 max-w-lg text-xs text-slate-500">
            Capture the baseline once, then run Drift Check whenever you want Arbiter to compare latest AWS Config observations back to that original state.
          </p>
        </div>
      )}
    </div>
  )
}
