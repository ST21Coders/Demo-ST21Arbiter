export function SeverityBadge({ severity }) {
  const map = {
    CRITICAL: 'badge-critical',
    HIGH:     'badge-high',
    MEDIUM:   'badge-medium',
    LOW:      'badge-low',
  }
  return <span className={map[severity] || 'badge-low'}>{severity}</span>
}

export function StatusBadge({ status }) {
  const map = {
    OPEN:             'badge-open',
    RESOLVED:         'badge-resolved',
    IN_REVIEW:        'badge-review',
    PENDING_APPROVAL: 'bg-amber-50 text-amber-700 border border-amber-200 text-xs px-2 py-0.5 rounded-full',
    APPROVED:         'bg-emerald-50 text-emerald-700 border border-emerald-200 text-xs px-2 py-0.5 rounded-full',
    REJECTED:         'bg-red-50 text-red-700 border border-red-200 text-xs px-2 py-0.5 rounded-full',
    EXECUTING:        'bg-indigo-50 text-indigo-700 border border-indigo-200 text-xs px-2 py-0.5 rounded-full',
    COMPLETED:        'badge-resolved',
    ESCALATED:        'bg-amber-50 text-amber-700 border border-amber-200 text-xs px-2 py-0.5 rounded-full',
    AUTO_APPROVED:    'bg-teal-50 text-teal-700 border border-teal-200 text-xs px-2 py-0.5 rounded-full',
  }
  return <span className={map[status] || 'badge-open'}>{status?.replace(/_/g, ' ')}</span>
}

export function TypeBadge({ type }) {
  return (
    <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${
      type === 'CROSS_DOMAIN'
        ? 'bg-slate-100 text-slate-700 border border-slate-200'
        : 'bg-slate-50 text-slate-500 border border-slate-200'
    }`}>
      {type === 'CROSS_DOMAIN' ? 'Cross-Domain' : 'Intra-Doc'}
    </span>
  )
}
