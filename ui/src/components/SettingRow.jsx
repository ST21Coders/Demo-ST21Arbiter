// One labelled settings row: title + optional description on the left, a
// control slot on the right. Keeps every row in the Settings sections aligned
// and consistently spaced. Pass the control as children.
//
// Props:
//   label    — row title
//   desc     — optional helper text
//   icon     — optional lucide icon component
//   last     — when false (default) draws a bottom divider, matching the
//              row dividers used in LLMControl.jsx
export default function SettingRow({ label, desc, icon: Icon, last = false, children }) {
  return (
    <div className={`flex items-center justify-between gap-4 py-3 ${last ? '' : 'border-b border-slate-100'}`}>
      <div className="flex items-start gap-2.5 min-w-0">
        {Icon && <Icon size={14} className="text-slate-400 mt-0.5 flex-shrink-0" />}
        <div className="min-w-0">
          <p className="text-sm text-slate-900 font-medium">{label}</p>
          {desc && <p className="text-xs text-slate-500 mt-0.5">{desc}</p>}
        </div>
      </div>
      <div className="flex-shrink-0">{children}</div>
    </div>
  )
}
