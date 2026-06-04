import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { CheckCircle, XCircle, AlertCircle, ChevronRight, Zap, Download } from 'lucide-react'
import { useFindings, useChangeRequests } from '../hooks/useApi'
import ActionRequestModal from '../components/ActionRequestModal'

// ── Framework definitions ─────────────────────────────────────────────────────
// Per-framework baseline score. Live posture is derived by:
//   1. Extracting an ARBITER-UC reference from each control's `note`
//   2. Looking up the matching finding's live status + severity
//   3. Penalising by severity weight: CRITICAL=10, HIGH=5, MEDIUM=2, LOW=1
// Controls without a UC reference fall back to the hard-coded status.
// `defaultStatus` is what shows when no finding is linked (e.g. PASS/WARN evidence
// the demo wants to display regardless of scanner state).

const FRAMEWORKS = [
  {
    id: 'pci-dss',
    name: 'PCI-DSS v4.0',
    baseScore: 100,
    controls: [
      { id: '1.3.2',  name: 'Network Access Controls', note: 'ARBITER-UC08: dev→prod VPC peering violates segmentation', defaultStatus: 'FAIL' },
      { id: '6.4',    name: 'Public Web Resources',    note: 'ARBITER-UC07: production ALB missing WAF',                  defaultStatus: 'FAIL' },
      { id: '4.1',    name: 'SSL/TLS Inspection',      note: 'ARBITER-UC04: SSL bypass for finance domains',              defaultStatus: 'FAIL' },
      { id: '8.4',    name: 'MFA Coverage',            note: 'ARBITER-UC05: MFA limited to admins',                       defaultStatus: 'FAIL' },
      { id: '10.2.1', name: 'Audit Logs',              note: 'ARBITER audit trail operational across all scans',          defaultStatus: 'PASS' },
      { id: '3.5.1',  name: 'Data Protection',         note: 'Encryption at rest enabled on all DDB + S3 buckets',        defaultStatus: 'PASS' },
    ],
  },
  {
    id: 'naic',
    name: 'NAIC MDL-668',
    baseScore: 100,
    controls: [
      { id: 'MDL-668 §3', name: 'Data Residency',      note: 'ARBITER-UC09: claims data replicating to eu-west-1',        defaultStatus: 'FAIL' },
      { id: 'MDL-668 §4', name: 'Authorised Transfers',note: 'ARBITER-UC10: DLP blocking authorised vendor transfers',    defaultStatus: 'FAIL' },
      { id: 'MDL-668 §5', name: 'Incident Disclosure', note: 'Legal not notified of active data-residency violation',     defaultStatus: 'WARN' },
      { id: 'MDL-668 §7', name: 'Third-Party Risk',    note: 'Vendor risk assessments current',                           defaultStatus: 'PASS' },
    ],
  },
  {
    id: 'soc2',
    name: 'SOC 2 Type II',
    baseScore: 100,
    controls: [
      { id: 'CC6.1', name: 'Logical Access',    note: 'ARBITER-UC05: MFA gap on non-admin users',           defaultStatus: 'FAIL' },
      { id: 'CC6.6', name: 'Network Controls',  note: 'ARBITER-UC08: production isolation violated',        defaultStatus: 'FAIL' },
      { id: 'CC7.2', name: 'System Monitoring', note: 'ARBITER detection operational',                      defaultStatus: 'PASS' },
      { id: 'CC8.1', name: 'Change Management', note: 'CR workflow in place; full audit trail',             defaultStatus: 'PASS' },
    ],
  },
  {
    id: 'iso27001',
    name: 'ISO 27001:2022',
    baseScore: 100,
    controls: [
      { id: 'A.8.20', name: 'Network Security',     note: 'ARBITER-UC08: production segmentation failure',  defaultStatus: 'FAIL' },
      { id: 'A.5.10', name: 'Use of Information',   note: 'ARBITER-UC01: Zscaler blocking approved tool',   defaultStatus: 'WARN' },
      { id: 'A.5.23', name: 'Information Security in Use of Cloud Services', note: 'ARBITER-UC09: cross-region replication of regulated data', defaultStatus: 'FAIL' },
      { id: 'A.5.36', name: 'Compliance Monitoring', note: 'Policy framework + monthly review cycle in place', defaultStatus: 'PASS' },
    ],
  },
]

const SEVERITY_PENALTY = { CRITICAL: 10, HIGH: 5, MEDIUM: 2, LOW: 1 }
const WARN_PENALTY = 2

const UC_REF_RE = /ARBITER-UC\d+/

function extractUC(note) {
  const m = note?.match(UC_REF_RE)
  return m ? m[0] : null
}

// ── Visual atoms ──────────────────────────────────────────────────────────────

function ScoreRing({ score }) {
  const color = score >= 90 ? '#10b981' : score >= 80 ? '#f59e0b' : '#ef4444'
  const r = 28
  const circ = 2 * Math.PI * r
  const dash = (score / 100) * circ
  return (
    <svg width={72} height={72} viewBox="0 0 72 72">
      <circle cx={36} cy={36} r={r} fill="none" stroke="#e2e8f0" strokeWidth={6} />
      <circle
        cx={36} cy={36} r={r} fill="none"
        stroke={color} strokeWidth={6}
        strokeDasharray={`${dash} ${circ - dash}`}
        strokeLinecap="round"
        transform="rotate(-90 36 36)"
      />
      <text x={36} y={40} textAnchor="middle" fontSize={14} fontWeight="bold" fill="#0f172a">{score}%</text>
    </svg>
  )
}

function StatusIcon({ status }) {
  if (status === 'PASS') return <CheckCircle size={14} className="text-emerald-600 flex-shrink-0" />
  if (status === 'FAIL') return <XCircle size={14} className="text-red-600 flex-shrink-0" />
  return <AlertCircle size={14} className="text-amber-600 flex-shrink-0" />
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function Governance() {
  const navigate = useNavigate()
  const { findings, load: loadFindings } = useFindings()
  const { createAction } = useChangeRequests()
  const [actionTarget, setActionTarget] = useState(null)

  useEffect(() => { loadFindings() }, [loadFindings])

  // Map ARBITER-UCxx → finding (so we can resolve status, severity, etc. per control).
  const findingByUC = useMemo(() => {
    const m = {}
    findings.forEach(f => { if (f.conflict_id) m[f.conflict_id] = f })
    return m
  }, [findings])

  // Per-control live evaluation. If a UC is linked AND found, use its live status;
  // otherwise fall back to the hard-coded defaultStatus.
  function evalControl(ctrl) {
    const uc = extractUC(ctrl.note)
    const linked = uc ? findingByUC[uc] : null
    let status
    let severity = null
    if (linked) {
      status = (linked.status === 'OPEN' || linked.status === 'IN_REVIEW') ? 'FAIL' : 'PASS'
      severity = linked.severity || null
    } else {
      status = ctrl.defaultStatus || 'PASS'
    }
    return { ctrl, uc, linked, status, severity }
  }

  function liveScore(fw) {
    let penalty = 0
    fw.controls.forEach(c => {
      const { status, severity } = evalControl(c)
      if (status === 'FAIL') {
        penalty += SEVERITY_PENALTY[severity] ?? SEVERITY_PENALTY.HIGH
      } else if (status === 'WARN') {
        penalty += WARN_PENALTY
      }
    })
    return Math.max(0, fw.baseScore - penalty)
  }

  const openCritical = findings.filter(f => f.severity === 'CRITICAL' && f.status === 'OPEN').length

  function openCRForControl(eval_) {
    // Prefer the full live finding as `conflict` (it carries severity, source_policy,
    // remediation, etc. that the modal prefill reads). Fall back to a minimal shape
    // when the UC isn't tracked yet — that still lets the user fill in the form.
    if (eval_.linked) {
      setActionTarget(eval_.linked)
    } else {
      setActionTarget({
        conflict_id: eval_.uc || `MANUAL-${eval_.ctrl.id}`,
        title: `${eval_.ctrl.id} ${eval_.ctrl.name}`,
        severity: 'HIGH',
        source_policy: eval_.ctrl.id,
        domains: [],
      })
    }
  }

  return (
    <div className="p-6 space-y-6 page-container">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-lg font-bold text-slate-900 tracking-tight">Governance & Compliance</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            Framework posture derived live from ARBITER findings. FAIL controls drop the score by severity weight (CRITICAL −10, HIGH −5, MEDIUM −2, LOW −1).
          </p>
        </div>
      </div>

      {openCritical > 0 && (
        <div className="rounded-xl p-4 flex items-center gap-3 bg-red-50 border border-red-200"
             style={{ borderLeft: '3px solid #ef4444' }}>
          <XCircle size={14} className="text-red-600 flex-shrink-0" />
          <p className="text-sm text-red-800 font-medium">
            {openCritical} critical open conflict{openCritical !== 1 ? 's' : ''} actively degrading compliance posture
          </p>
        </div>
      )}

      {/* Framework cards */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {FRAMEWORKS.map(fw => {
          const evals = fw.controls.map(evalControl)
          const fails = evals.filter(e => e.status === 'FAIL').length
          const warns = evals.filter(e => e.status === 'WARN').length
          const passes = evals.filter(e => e.status === 'PASS').length
          const score = liveScore(fw)
          const scoreColor = score >= 90 ? 'rgba(16,185,129,0.05)' : score >= 80 ? 'rgba(245,158,11,0.05)' : 'rgba(239,68,68,0.05)'
          const scoreBorder = score >= 90 ? 'rgba(16,185,129,0.3)' : score >= 80 ? 'rgba(245,158,11,0.3)' : 'rgba(239,68,68,0.3)'
          return (
            <div key={fw.id} className="rounded-xl p-4 bg-white"
                 style={{ border: `1px solid ${scoreBorder}`, boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }}>
              <div className="flex items-center gap-4 mb-4">
                <div className="relative rounded-full p-1" style={{ background: scoreColor }}>
                  <ScoreRing score={score} />
                </div>
                <div className="flex-1 min-w-0">
                  <p className="font-semibold text-slate-900">{fw.name}</p>
                  <p className="text-xs text-slate-500 mt-0.5">
                    <span className="text-emerald-700">{passes} passing</span>
                    {' · '}
                    <span className="text-amber-700">{warns} warning{warns !== 1 ? 's' : ''}</span>
                    {' · '}
                    <span className="text-red-700">{fails} failing</span>
                  </p>
                </div>
                {/* PDF export per framework — Feature_Coverage_Plan.md §3 steps
                    10-12. Backend endpoint /export/compliance?framework=<id>
                    not yet implemented; surfaced disabled to telegraph roadmap. */}
                <button
                  disabled
                  title={`Export ${fw.name} report (PDF) — available after backend ships; see Documents/Feature_Coverage_Plan.md step 11`}
                  className="btn-ghost flex items-center gap-1 text-[10px] px-2 py-1 opacity-50 cursor-not-allowed flex-shrink-0"
                >
                  <Download size={11} /> PDF
                </button>
              </div>
              <div className="space-y-0">
                {evals.map((e, i) => {
                  const clickable = !!e.uc
                  const showCR = e.status === 'FAIL'
                  return (
                    <div key={e.ctrl.id}
                         onClick={clickable ? () => navigate(`/findings/${e.uc}`) : undefined}
                         className={`flex items-start gap-2.5 py-2 transition-colors ${i < evals.length - 1 ? 'border-b border-slate-100' : ''} ${clickable ? 'cursor-pointer hover:bg-slate-50 rounded-md -mx-1.5 px-1.5' : ''}`}>
                      <StatusIcon status={e.status} />
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="text-xs font-mono text-slate-400">{e.ctrl.id}</span>
                          <span className="text-xs text-slate-800">{e.ctrl.name}</span>
                          {e.linked?.severity && (
                            <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded ${
                              e.linked.severity === 'CRITICAL' ? 'bg-red-50 text-red-700 border border-red-200' :
                              e.linked.severity === 'HIGH'     ? 'bg-orange-50 text-orange-700 border border-orange-200' :
                              e.linked.severity === 'MEDIUM'   ? 'bg-amber-50 text-amber-700 border border-amber-200' :
                                                                 'bg-emerald-50 text-emerald-700 border border-emerald-200'
                            }`}>{e.linked.severity}</span>
                          )}
                          {e.linked && e.linked.status !== 'OPEN' && (
                            <span className="text-[10px] text-emerald-700 bg-emerald-50 border border-emerald-200 px-1.5 py-0.5 rounded">
                              {e.linked.status}
                            </span>
                          )}
                        </div>
                        {e.ctrl.note && <p className="text-xs text-slate-500 mt-0.5">{e.ctrl.note}</p>}
                      </div>
                      <div className="flex items-center gap-2 flex-shrink-0">
                        {showCR && (
                          <button
                            onClick={(ev) => { ev.stopPropagation(); openCRForControl(e) }}
                            className="btn-primary flex items-center gap-1 text-[10px] px-2 py-1"
                            title={e.linked ? `Open CR for ${e.uc}` : 'Create a manual remediation CR'}
                          >
                            <Zap size={10} /> Open CR
                          </button>
                        )}
                        {clickable && (
                          <ChevronRight size={13} className="text-slate-300" />
                        )}
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>
          )
        })}
      </div>

      {actionTarget && (
        <ActionRequestModal
          conflict={actionTarget}
          onClose={result => {
            setActionTarget(null)
            if (result) loadFindings()
          }}
          onCreate={createAction}
        />
      )}
    </div>
  )
}
