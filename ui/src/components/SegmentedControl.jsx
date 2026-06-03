// Small segmented button group for mutually-exclusive choices (e.g. density,
// theme). Styled to the slate/rounded-lg language used across the app.
//
// Props:
//   value     — currently selected option value
//   options   — [{ value, label, tag? }]   (tag renders a small chip, e.g. "Preview")
//   onChange  — (value) => void
//   disabled  — disables the whole control
export default function SegmentedControl({ value, options, onChange, disabled }) {
  return (
    <div
      role="radiogroup"
      className={`inline-flex items-center rounded-lg border border-slate-200 bg-slate-50 p-0.5 ${disabled ? 'opacity-40' : ''}`}
    >
      {options.map(opt => {
        const active = opt.value === value
        return (
          <button
            key={opt.value}
            role="radio"
            aria-checked={active}
            disabled={disabled}
            onClick={() => !disabled && !active && onChange(opt.value)}
            className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium transition-colors ${
              disabled ? 'cursor-not-allowed' : 'cursor-pointer'
            } ${
              active
                ? 'bg-white text-slate-900 shadow-sm border border-slate-200'
                : 'text-slate-500 hover:text-slate-800'
            }`}
          >
            {opt.label}
            {opt.tag && (
              <span className="text-[9px] font-bold uppercase tracking-wider px-1 py-0.5 rounded bg-indigo-50 text-indigo-600 border border-indigo-200">
                {opt.tag}
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}
