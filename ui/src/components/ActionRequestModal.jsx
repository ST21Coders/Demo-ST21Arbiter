import { useState } from 'react'
import { X, Wand2, AlertTriangle, Info } from 'lucide-react'

const ENVIRONMENTS = ['DEV', 'STAGING', 'PRE_PROD', 'PROD']
const ACTION_TYPES = ['SECURITY_FIX', 'POLICY_UPDATE', 'CONFIGURATION_CHANGE', 'ACCESS_CHANGE', 'RULE_UPDATE', 'DOCUMENT_ARCHIVE']
const SEVERITIES = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']

const ENV_MATRIX = {
  DEV:      { label: 'Auto-approved', color: 'text-emerald-700', approvers: [] },
  STAGING:  { label: 'Team Lead approval', color: 'text-indigo-700', approvers: ['Team Lead'] },
  PRE_PROD: { label: 'Manager + Owning Team Lead', color: 'text-amber-700', approvers: ['Manager', 'Owning Team Lead'] },
  PROD:     { label: 'CISO + VP Security + Legal notification', color: 'text-red-700', approvers: ['CISO', 'VP Security', 'Legal (notified)'] },
}

export default function ActionRequestModal({ conflict, onClose, onCreate, prefill }) {
  const [mode, setMode] = useState('guided') // 'guided' | 'natural'
  const [loading, setLoading] = useState(false)
  const [form, setForm] = useState({
    request: conflict
      ? `Remediate conflict ${conflict.conflict_id}: ${conflict.title}`
      : '',
    action_type: prefill?.action_type || (conflict?.source_technical ? 'SECURITY_FIX' : 'POLICY_UPDATE'),
    target_resource: prefill?.target_resource || conflict?.source_technical || conflict?.source_policy || '',
    target_environment: prefill?.target_environment || 'PROD',
    severity: prefill?.severity || conflict?.severity || 'HIGH',
    justification: prefill?.justification || '',
    requesting_team: prefill?.requesting_team || '',
    requested_by: 'sec.analyst@meridianinsurance.com',
  })

  const envInfo = ENV_MATRIX[form.target_environment]

  async function handleSubmit(e) {
    e.preventDefault()
    setLoading(true)
    try {
      const payload = {
        ...form,
        conflict_id: conflict?.conflict_id,
      }
      const result = await onCreate(payload)
      onClose(result)
    } catch (err) {
      alert('Error creating action: ' + err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-slate-900/40 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-white border border-slate-200 rounded-2xl w-full max-w-2xl max-h-[90vh] overflow-y-auto slide-in shadow-xl">
        {/* Header */}
        <div className="flex items-center justify-between p-5 border-b border-slate-200">
          <div>
            <h2 className="text-lg font-bold text-slate-900">Initiate Remediation Action</h2>
            {conflict && (
              <p className="text-xs text-slate-500 mt-0.5">
                Conflict: <span className="text-indigo-600">{conflict.conflict_id}</span> · {conflict.title}
              </p>
            )}
            {prefill && !conflict && (
              <p className="text-xs text-indigo-600 mt-0.5">Pre-filled by ARBITER agent · review before submitting</p>
            )}
          </div>
          <button onClick={() => onClose(null)} className="text-slate-400 hover:text-slate-900">
            <X size={20} />
          </button>
        </div>

        {/* Mode toggle */}
        <div className="p-5 border-b border-slate-200">
          <div className="flex gap-2">
            <button
              onClick={() => setMode('guided')}
              className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${mode === 'guided' ? 'bg-indigo-600 text-white' : 'text-slate-600 hover:text-slate-900 hover:bg-slate-100'}`}
            >
              Guided Form
            </button>
            <button
              onClick={() => setMode('natural')}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${mode === 'natural' ? 'bg-indigo-600 text-white' : 'text-slate-600 hover:text-slate-900 hover:bg-slate-100'}`}
            >
              <Wand2 size={13} /> Natural Language
            </button>
          </div>
        </div>

        <form onSubmit={handleSubmit} className="p-5 space-y-4">
          {mode === 'natural' && (
            <div>
              <label className="block text-xs text-slate-600 mb-1.5 font-medium">Describe the action you want to take</label>
              <textarea
                value={form.request}
                onChange={e => setForm(f => ({ ...f, request: e.target.value }))}
                rows={4}
                placeholder="e.g. Remove the inbound rule from 10.50.0.0/16 on the production security group to fix the VPC peering violation"
                className="input resize-none"
              />
              <p className="text-xs text-slate-500 mt-1">Claude will parse this and determine action type, environment, and required approvers.</p>
            </div>
          )}

          {mode === 'guided' && (
            <>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-xs text-slate-600 mb-1.5 font-medium">Action Type</label>
                  <select value={form.action_type} onChange={e => setForm(f => ({ ...f, action_type: e.target.value }))} className="input">
                    {ACTION_TYPES.map(t => <option key={t} value={t}>{t.replace(/_/g, ' ')}</option>)}
                  </select>
                </div>
                <div>
                  <label className="block text-xs text-slate-600 mb-1.5 font-medium">Severity</label>
                  <select value={form.severity} onChange={e => setForm(f => ({ ...f, severity: e.target.value }))} className="input">
                    {SEVERITIES.map(s => <option key={s} value={s}>{s}</option>)}
                  </select>
                </div>
              </div>

              <div>
                <label className="block text-xs text-slate-600 mb-1.5 font-medium">Target Resource / Policy</label>
                <input value={form.target_resource} onChange={e => setForm(f => ({ ...f, target_resource: e.target.value }))} className="input" placeholder="e.g. sg-mig-prod-peer-dev-001 or MIG-POL-004-SEG01" />
              </div>
            </>
          )}

          {/* Environment selector */}
          <div>
            <label className="block text-xs text-slate-600 mb-1.5 font-medium">Target Environment</label>
            <div className="grid grid-cols-4 gap-2">
              {ENVIRONMENTS.map(env => (
                <button
                  key={env}
                  type="button"
                  onClick={() => setForm(f => ({ ...f, target_environment: env }))}
                  className={`py-2 rounded-lg text-xs font-medium border transition-colors ${
                    form.target_environment === env
                      ? 'border-indigo-500 bg-indigo-50 text-indigo-700'
                      : 'border-slate-200 text-slate-600 hover:border-slate-300 hover:text-slate-900'
                  }`}
                >
                  {env.replace('_', '-')}
                </button>
              ))}
            </div>
          </div>

          {/* Approval preview */}
          <div className={`rounded-lg p-3 border ${
            form.target_environment === 'DEV' ? 'border-emerald-200 bg-emerald-50' :
            form.target_environment === 'PROD' ? 'border-red-200 bg-red-50' :
            'border-amber-200 bg-amber-50'
          }`}>
            <div className="flex items-center gap-2 mb-1">
              <Info size={13} className={envInfo.color} />
              <span className={`text-xs font-medium ${envInfo.color}`}>Approval Required: {envInfo.label}</span>
            </div>
            {envInfo.approvers.length > 0 && (
              <div className="flex flex-wrap gap-1 mt-1">
                {envInfo.approvers.map(a => (
                  <span key={a} className="bg-white border border-slate-200 text-slate-700 text-xs px-2 py-0.5 rounded-full">{a}</span>
                ))}
              </div>
            )}
          </div>

          {/* Cross-team warning */}
          <div className="bg-amber-50 border border-amber-200 rounded-lg p-3">
            <div className="flex items-start gap-2">
              <AlertTriangle size={13} className="text-amber-600 mt-0.5 flex-shrink-0" />
              <div>
                <p className="text-xs text-amber-800 font-medium">Cross-team ownership check</p>
                <p className="text-xs text-amber-700 mt-0.5">If this policy/resource is owned by a different team, their team lead approval will be automatically added to the chain.</p>
              </div>
            </div>
          </div>

          <div>
            <label className="block text-xs text-slate-600 mb-1.5 font-medium">Your Team (optional)</label>
            <input value={form.requesting_team} onChange={e => setForm(f => ({ ...f, requesting_team: e.target.value }))} className="input" placeholder="e.g. Cloud Security, Network Ops" />
          </div>

          <div>
            <label className="block text-xs text-slate-600 mb-1.5 font-medium">Business Justification</label>
            <textarea
              value={form.justification}
              onChange={e => setForm(f => ({ ...f, justification: e.target.value }))}
              rows={2}
              placeholder="Why is this change needed? What risk does it address?"
              className="input resize-none"
              required
            />
          </div>

          <div className="flex gap-3 pt-2">
            <button type="button" onClick={() => onClose(null)} className="btn-ghost flex-1">Cancel</button>
            <button type="submit" disabled={loading || !form.justification} className="btn-primary flex-1">
              {loading ? 'Submitting...' : 'Submit Change Request'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
