import { ToggleLeft, ToggleRight } from 'lucide-react'

// Shared on/off toggle. Extracted verbatim from LLMControl.jsx so the
// LLM Control panel and the Settings page render an identical control.
export default function Toggle({ on, onChange, disabled }) {
  return (
    <button
      onClick={() => !disabled && onChange(!on)}
      disabled={disabled}
      className={`transition-colors ${disabled ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer'}`}
    >
      {on
        ? <ToggleRight size={22} className="text-indigo-600" />
        : <ToggleLeft  size={22} className="text-slate-300" />
      }
    </button>
  )
}
