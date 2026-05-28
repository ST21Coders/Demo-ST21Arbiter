import { useEffect } from 'react'
import { useFindings } from '../hooks/useApi'
import { CheckCircle, XCircle, AlertCircle } from 'lucide-react'

const FRAMEWORKS = [
  {
    id: 'pci-dss',
    name: 'PCI-DSS v4.0',
    baseScore: 91,
    controls: [
      { id: '1.3.2', name: 'Network Access Controls', status: 'FAIL', note: 'ARBITER-UC04: dev→prod VPC peering violates segmentation' },
      { id: '7.2.1', name: 'Access Control Systems',  status: 'PASS', note: 'Access review cycles enforced' },
      { id: '10.2.1',name: 'Audit Logs',              status: 'PASS', note: 'Full audit trail operational' },
      { id: '3.5.1', name: 'Data Protection',         status: 'PASS', note: 'Encryption at rest enabled' },
    ],
  },
  {
    id: 'naic',
    name: 'NAIC MDL-668',
    baseScore: 82,
    controls: [
      { id: 'MDL-668 §4', name: 'Data Residency',      status: 'FAIL', note: 'ARBITER-UC10: claims data replicating to eu-west-1' },
      { id: 'MDL-668 §5', name: 'Incident Disclosure', status: 'WARN', note: 'Legal not notified of active data residency violation' },
      { id: 'MDL-668 §7', name: 'Third-Party Risk',    status: 'PASS', note: 'Vendor risk assessments current' },
    ],
  },
  {
    id: 'soc2',
    name: 'SOC 2 Type II',
    baseScore: 87,
    controls: [
      { id: 'CC6.1', name: 'Logical Access',    status: 'WARN', note: 'ARBITER-UC07: access review period ambiguous across policy versions' },
      { id: 'CC6.6', name: 'Network Controls',  status: 'FAIL', note: 'ARBITER-UC04: production isolation violated' },
      { id: 'CC7.2', name: 'System Monitoring', status: 'PASS', note: 'ARBITER detection operational' },
      { id: 'CC8.1', name: 'Change Management', status: 'PASS', note: 'CR workflow in place' },
    ],
  },
  {
    id: 'iso27001',
    name: 'ISO 27001:2022',
    baseScore: 89,
    controls: [
      { id: 'A.8.20', name: 'Network Security',    status: 'FAIL', note: 'ARBITER-UC04: production segmentation failure' },
      { id: 'A.5.10', name: 'Use of Information',  status: 'WARN', note: 'ARBITER-UC01: Zscaler blocking approved tool' },
      { id: 'A.8.10', name: 'Information Deletion',status: 'PASS', note: 'Retention policies active' },
      { id: 'A.5.36', name: 'Compliance',          status: 'PASS', note: 'Policy framework updated' },
    ],
  },
]

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

export default function Governance() {
  const { findings, load } = useFindings()
  useEffect(() => { load() }, [load])

  const openCritical = findings.filter(f => f.severity === 'CRITICAL' && f.status === 'OPEN').length

  return (
    <div className="p-6 space-y-6 max-w-6xl">
      <div>
        <h1 className="text-lg font-bold text-slate-900 tracking-tight">Governance & Compliance</h1>
        <p className="text-xs text-slate-500 mt-0.5">Framework posture based on ARBITER live conflict detection</p>
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
          const fails = fw.controls.filter(c => c.status === 'FAIL').length
          const warns = fw.controls.filter(c => c.status === 'WARN').length
          const score = Math.max(0, fw.baseScore - fails * 8 - warns * 2)
          const scoreColor = score >= 90 ? 'rgba(16,185,129,0.05)' : score >= 80 ? 'rgba(245,158,11,0.05)' : 'rgba(239,68,68,0.05)'
          const scoreBorder = score >= 90 ? 'rgba(16,185,129,0.3)' : score >= 80 ? 'rgba(245,158,11,0.3)' : 'rgba(239,68,68,0.3)'
          return (
            <div key={fw.id} className="rounded-xl p-4 bg-white"
                 style={{ border: `1px solid ${scoreBorder}`, boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }}>
              <div className="flex items-center gap-4 mb-4">
                <div className="relative rounded-full p-1" style={{ background: scoreColor }}>
                  <ScoreRing score={score} />
                </div>
                <div>
                  <p className="font-semibold text-slate-900">{fw.name}</p>
                  <p className="text-xs text-slate-500 mt-0.5">
                    <span className="text-emerald-700">{fw.controls.filter(c => c.status === 'PASS').length} passing</span>
                    {' · '}
                    <span className="text-amber-700">{warns} warning{warns !== 1 ? 's' : ''}</span>
                    {' · '}
                    <span className="text-red-700">{fails} failing</span>
                  </p>
                </div>
              </div>
              <div className="space-y-0">
                {fw.controls.map((ctrl, i) => (
                  <div key={ctrl.id}
                       className={`flex items-start gap-2.5 py-2 ${i < fw.controls.length - 1 ? 'border-b border-slate-100' : ''}`}>
                    <StatusIcon status={ctrl.status} />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-xs font-mono text-slate-400">{ctrl.id}</span>
                        <span className="text-xs text-slate-800">{ctrl.name}</span>
                      </div>
                      {ctrl.note && <p className="text-xs text-slate-500 mt-0.5">{ctrl.note}</p>}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
