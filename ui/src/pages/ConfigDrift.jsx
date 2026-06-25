import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle, Bell, CheckCircle2, Clock, Database, GitBranch, RefreshCw,
  RotateCcw, ShieldAlert, TimerReset, X,
} from 'lucide-react'
import {
  captureSecurityGroupBaseline,
  checkSecurityGroupDrift,
  getCurrentSecurityGroups,
  getSecurityGroupBaseline,
  revertSecurityGroupDrift,
} from '../hooks/useApi'

function formatDate(value) {
  return value ? new Date(value).toLocaleString() : 'Not captured'
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

function statusClass(driftDetected) {
  return driftDetected ? 'border-red-200 bg-red-50' : 'border-emerald-200 bg-emerald-50'
}

export default function ConfigDrift() {
  const [baseline, setBaseline] = useState(null)
  const [latest, setLatest] = useState(null)
  const [checkResult, setCheckResult] = useState(null)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [flowOpen, setFlowOpen] = useState(false)
  const autoRevertCheckIdRef = useRef('')

  const driftFindings = checkResult?.findings || []
  const driftDetected = driftFindings.length > 0
  const hasBaseline = Boolean(baseline?.resources?.length || checkResult?.baselineCapturedAt)
  const baselineAge = useMemo(() => (
    formatDate(baseline?.capturedAt || checkResult?.baselineCapturedAt)
  ), [baseline, checkResult])
  const latestCount = latest?.count ?? checkResult?.latestResourceCount ?? '—'
  const deadline = checkResult?.hitl?.deadlineAt
  const pendingReverts = checkResult?.pendingReverts || []
  const hasPendingRevert = pendingReverts.some(item => item.status === 'PENDING_HITL')
  const deadlineExpired = deadline ? new Date(deadline).getTime() <= Date.now() : false
  const primaryFinding = driftFindings[0]
  const primaryRevert = pendingReverts[0]
  const flowOptions = [
    {
      title: 'Accept the drift',
      detail: primaryFinding
        ? `Record an approved exception for ${primaryFinding.resourceName || primaryFinding.resourceId} and make this change part of the accepted baseline.`
        : 'Record an approved exception and update the accepted baseline.',
    },
    {
      title: 'Stop the drift and revert',
      detail: primaryRevert
        ? `${primaryRevert.action} on ${primaryRevert.resourceId}. ${deadline ? `Auto-revert is staged after ${formatDate(deadline)}.` : 'Revert action is staged.'}`
        : 'Reject the change and restore the resource to the saved baseline.',
    },
  ]

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      setBusy('baseline-load')
      setError('')
      try {
        const data = await getSecurityGroupBaseline()
        if (cancelled) return
        if (data?.captured) {
          setBaseline(data)
          setLatest({
            count: data.resources?.length || data.resourceCount || 0,
            resources: data.resources || [],
            observedAt: data.capturedAt,
          })
          setNotice(`Restored saved baseline with ${data.resources?.length || data.resourceCount || 0} security group${(data.resources?.length || data.resourceCount) === 1 ? '' : 's'}.`)
        }
      } catch (err) {
        if (!cancelled) setError(err.message || String(err))
      } finally {
        if (!cancelled) setBusy('')
      }
    })()
    return () => { cancelled = true }
  }, [])

  async function loadCurrent() {
    setBusy('current')
    setError('')
    try {
      const data = await getCurrentSecurityGroups()
      setLatest(data)
      setNotice(`Loaded ${data.count || 0} live security group${data.count === 1 ? '' : 's'} from AWS.`)
    } catch (err) {
      setError(err.message || String(err))
    } finally {
      setBusy('')
    }
  }

  async function captureBaseline() {
    setBusy('baseline')
    setError('')
    try {
      const data = await captureSecurityGroupBaseline()
      setBaseline(data)
      setCheckResult(null)
      setLatest({ count: data.resources?.length || 0, resources: data.resources || [], observedAt: data.capturedAt })
      setNotice(`Baseline captured from ${data.resources?.length || 0} live security group${data.resources?.length === 1 ? '' : 's'}.`)
    } catch (err) {
      setError(err.message || String(err))
    } finally {
      setBusy('')
    }
  }

  async function runDriftCheck() {
    setBusy('check')
    setError('')
    try {
      const data = await checkSecurityGroupDrift({ hitlTimeoutMinutes: 10 })
      setCheckResult(data)
      setLatest({ count: data.latestResourceCount, resources: data.latest || [], observedAt: data.checkedAt })
      setNotice(data.findings?.length
        ? `Drift detected. HITL deadline opened for ${data.hitl?.timeoutMinutes || 10} minutes.`
        : 'No drift detected against the saved baseline.')
    } catch (err) {
      setError(err.message || String(err))
    } finally {
      setBusy('')
    }
  }

  const executeExpiredRevert = useCallback(async (checkId = checkResult?.checkId, { automatic = false } = {}) => {
    if (!checkId) return
    setBusy('revert')
    setError('')
    try {
      const data = await revertSecurityGroupDrift({ checkId })
      if (data.check) {
        setCheckResult(data.check)
        setLatest({
          count: data.check.latestResourceCount,
          resources: data.check.latest || [],
          observedAt: data.check.checkedAt,
        })
      }
      const appliedCount = data.applied?.length || 0
      setNotice(appliedCount
        ? `${automatic ? 'Auto-revert' : 'Revert'} completed for ${appliedCount} expired security group change${appliedCount === 1 ? '' : 's'}.`
        : `${automatic ? 'Auto-revert' : 'Revert'} ran, but no allowed pending action was applied.`)
    } catch (err) {
      setError(err.message || String(err))
    } finally {
      setBusy('')
    }
  }, [checkResult?.checkId])

  useEffect(() => {
    if (!checkResult?.checkId || !deadline || !hasPendingRevert) return undefined
    if (autoRevertCheckIdRef.current === checkResult.checkId) return undefined

    const delay = Math.max(1000, new Date(deadline).getTime() - Date.now() + 1000)
    const timer = window.setTimeout(() => {
      autoRevertCheckIdRef.current = checkResult.checkId
      executeExpiredRevert(checkResult.checkId, { automatic: true })
    }, delay)
    return () => window.clearTimeout(timer)
  }, [checkResult?.checkId, deadline, executeExpiredRevert, hasPendingRevert])

  return (
    <div className="p-6 space-y-6 page-container">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="flex items-center gap-2 text-lg font-bold tracking-tight text-slate-900">
            <ShieldAlert size={19} className="text-indigo-600" /> Config Drift
          </h1>
          <p className="mt-1 max-w-3xl text-xs text-slate-500">
            Live AWS security group drift workflow. Capture a known-good baseline, compare current AWS state against it, and stage HITL remediation decisions.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={loadCurrent}
            disabled={Boolean(busy)}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-60"
          >
            <RefreshCw size={13} className={busy === 'current' ? 'animate-spin' : ''} /> Current AWS
          </button>
          <button
            type="button"
            onClick={captureBaseline}
            disabled={Boolean(busy)}
            className="inline-flex items-center gap-1.5 rounded-lg bg-slate-900 px-3 py-2 text-xs font-semibold text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            <Database size={13} /> Baseline
          </button>
          <button
            type="button"
            onClick={runDriftCheck}
            disabled={Boolean(busy)}
            className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-2 text-xs font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-slate-200 disabled:text-slate-500"
          >
            <RotateCcw size={13} /> Drift Check
          </button>
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-4">
        <div className="rounded-lg border border-slate-200 bg-white p-4">
          <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">Baseline</p>
          <p className="mt-2 text-sm font-bold text-slate-900">{hasBaseline ? 'Captured' : 'Not captured'}</p>
          <p className="mt-1 text-xs text-slate-500">{baselineAge}</p>
        </div>
        <div className="rounded-lg border border-slate-200 bg-white p-4">
          <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">Current AWS</p>
          <p className="mt-2 text-sm font-bold text-slate-900">{latestCount} security group{latestCount === 1 ? '' : 's'}</p>
          <p className="mt-1 text-xs text-slate-500">{latest?.observedAt ? `Observed ${formatDate(latest.observedAt)}` : 'Use Current AWS or Drift Check.'}</p>
        </div>
        <div className={`rounded-lg border p-4 ${statusClass(driftDetected)}`}>
          <p className={`text-[10px] font-semibold uppercase tracking-wider ${driftDetected ? 'text-red-500' : 'text-emerald-600'}`}>Drift Status</p>
          <p className={`mt-2 flex items-center gap-1.5 text-sm font-bold ${driftDetected ? 'text-red-800' : 'text-emerald-800'}`}>
            {driftDetected ? <AlertTriangle size={14} /> : <CheckCircle2 size={14} />}
            {driftDetected ? `${driftFindings.length} change detected` : 'No active finding'}
          </p>
          <p className={`mt-1 text-xs ${driftDetected ? 'text-red-700' : 'text-emerald-700'}`}>
            {checkResult?.checkedAt ? `Last checked ${formatDate(checkResult.checkedAt)}` : 'Run Drift Check after baseline capture.'}
          </p>
        </div>
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-4">
          <p className="text-[10px] font-semibold uppercase tracking-wider text-amber-600">HITL Window</p>
          <p className="mt-2 flex items-center gap-1.5 text-sm font-bold text-amber-900">
            <TimerReset size={14} /> {deadline ? '10 minutes' : 'Waiting'}
          </p>
          <p className="mt-1 text-xs text-amber-700">{deadline ? `Deadline ${formatDate(deadline)}` : 'No pending HITL item.'}</p>
        </div>
      </div>

      {(notice || error) && (
        <div className={`rounded-lg border p-3 text-xs ${error ? 'border-red-200 bg-red-50 text-red-800' : 'border-slate-200 bg-white text-slate-600'}`}>
          {error || notice}
        </div>
      )}

      {driftDetected && (
        <div className="rounded-xl border border-red-200 bg-white shadow-sm">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-red-100 p-4">
            <div>
              <p className="text-sm font-bold text-slate-900">Detected Drift</p>
              <p className="mt-1 text-xs text-slate-500">
                HITL is pending. If no one addresses it before the deadline, Arbiter will revert the allowlisted security group change.
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={() => setFlowOpen(true)}
                className="inline-flex items-center gap-1.5 rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-2 text-xs font-semibold text-indigo-700 hover:bg-indigo-100"
              >
                <GitBranch size={13} /> Flow
              </button>
              <span className="inline-flex items-center gap-1.5 rounded-full border border-amber-200 bg-amber-50 px-2.5 py-1 text-[10px] font-bold text-amber-700">
                <Bell size={11} /> {hasPendingRevert ? 'HITL PENDING' : checkResult?.revertStatus || 'HITL COMPLETE'}
              </span>
              {hasPendingRevert && (
                <button
                  type="button"
                  onClick={() => executeExpiredRevert()}
                  disabled={Boolean(busy) || !deadlineExpired}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-red-600 px-3 py-2 text-xs font-semibold text-white hover:bg-red-700 disabled:cursor-not-allowed disabled:bg-slate-200 disabled:text-slate-500"
                  title={deadlineExpired ? 'Execute the expired staged revert' : 'Available after the HITL deadline'}
                >
                  <RotateCcw size={13} className={busy === 'revert' ? 'animate-spin' : ''} /> Revert expired drift
                </button>
              )}
            </div>
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
                    <td className="max-w-[280px] px-4 py-3 text-slate-600">{finding.recommendation}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {pendingReverts.length > 0 && (
            <div className="border-t border-red-100 bg-red-50 p-4">
              <p className="text-xs font-bold text-red-900">Staged Revert Actions</p>
              <div className="mt-2 grid gap-2 md:grid-cols-2">
                {pendingReverts.map((item, index) => (
                  <div key={`${item.resourceId}-${item.action}-${index}`} className="rounded-lg border border-red-200 bg-white p-3">
                    <p className="font-mono text-[11px] font-semibold text-red-800">{item.action}</p>
                    <p className="mt-1 font-mono text-[10px] text-slate-500">{item.resourceId}</p>
                    <p className="mt-1 text-xs text-slate-600">Status: {item.status}</p>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {flowOpen && primaryFinding && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/45 p-4">
          <div className="w-full max-w-4xl overflow-hidden rounded-xl border border-slate-200 bg-white shadow-2xl">
            <div className="flex items-start justify-between gap-4 border-b border-slate-200 p-5">
              <div>
                <p className="text-[10px] font-bold uppercase tracking-wider text-indigo-500">Detected Drift Flow</p>
                <h2 className="mt-1 text-base font-bold text-slate-900">{primaryFinding.resourceName || primaryFinding.resourceId}</h2>
                <p className="mt-1 text-xs text-slate-500">
                  This flow was opened from the current Detected Drift record and reflects the decision point Arbiter is holding.
                </p>
              </div>
              <button
                type="button"
                onClick={() => setFlowOpen(false)}
                className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-slate-200 text-slate-500 hover:bg-slate-50"
                aria-label="Close flow"
              >
                <X size={15} />
              </button>
            </div>

            <div className="grid gap-3 p-5 md:grid-cols-[1fr_auto_1fr_auto_1.25fr]">
              <div className="rounded-lg border border-red-200 bg-red-50 p-4">
                <p className="text-[10px] font-bold uppercase tracking-wider text-red-500">1. Reason</p>
                <p className="mt-2 text-sm font-bold text-red-900">{primaryFinding.driftType}</p>
                <p className="mt-2 text-xs text-red-800">{primaryFinding.after}</p>
              </div>
              <div className="hidden items-center text-slate-300 md:flex">
                <GitBranch size={18} />
              </div>
              <div className="rounded-lg border border-amber-200 bg-amber-50 p-4">
                <p className="text-[10px] font-bold uppercase tracking-wider text-amber-600">2. Now</p>
                <p className="mt-2 text-sm font-bold text-amber-900">{hasPendingRevert ? 'HITL pending' : checkResult?.revertStatus || 'Decision recorded'}</p>
                <p className="mt-2 text-xs text-amber-800">
                  {deadline ? `Deadline ${formatDate(deadline)}.` : 'No deadline is currently active.'} Severity is {primaryFinding.severity || 'unrated'}.
                </p>
              </div>
              <div className="hidden items-center text-slate-300 md:flex">
                <GitBranch size={18} />
              </div>
              <div className="rounded-lg border border-slate-200 bg-slate-50 p-4">
                <p className="text-[10px] font-bold uppercase tracking-wider text-slate-500">3. Options</p>
                <div className="mt-3 grid gap-2">
                  {flowOptions.map(option => (
                    <div key={option.title} className="rounded-lg border border-slate-200 bg-white p-3">
                      <p className="text-xs font-bold text-slate-900">{option.title}</p>
                      <p className="mt-1 text-xs text-slate-600">{option.detail}</p>
                    </div>
                  ))}
                </div>
              </div>
            </div>

            <div className="border-t border-slate-200 bg-white p-5">
              <p className="text-xs font-bold text-slate-900">Detected Drift context</p>
              <div className="mt-3 grid gap-3 md:grid-cols-3">
                <div className="rounded-lg border border-slate-200 p-3">
                  <p className="text-[10px] font-bold uppercase tracking-wider text-slate-400">Before</p>
                  <p className="mt-1 text-xs text-slate-700">{primaryFinding.before}</p>
                </div>
                <div className="rounded-lg border border-slate-200 p-3">
                  <p className="text-[10px] font-bold uppercase tracking-wider text-slate-400">After</p>
                  <p className="mt-1 text-xs text-slate-700">{primaryFinding.after}</p>
                </div>
                <div className="rounded-lg border border-slate-200 p-3">
                  <p className="text-[10px] font-bold uppercase tracking-wider text-slate-400">Recommendation</p>
                  <p className="mt-1 text-xs text-slate-700">{primaryFinding.recommendation}</p>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {!driftDetected && (
        <div className="rounded-xl border border-slate-200 bg-white p-6 text-center">
          <Clock size={22} className="mx-auto text-slate-300" />
          <p className="mt-2 text-sm font-semibold text-slate-800">Ready for live drift checks</p>
          <p className="mx-auto mt-1 max-w-lg text-xs text-slate-500">
            Capture the baseline from live AWS once, then run Drift Check whenever you want Arbiter to compare current security groups back to that approved state.
          </p>
        </div>
      )}
    </div>
  )
}
