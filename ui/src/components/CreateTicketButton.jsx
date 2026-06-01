import { useNavigate } from 'react-router-dom'
import { Ticket } from 'lucide-react'

// Navigates to the Action Center and opens ActionRequestModal pre-filled with
// the conversation-extracted fields. The user gets to review/edit (title,
// severity, environment, justification) before clicking Submit Change Request
// in the modal — so the chat does not autonomously create tickets.
//
// The prefill payload is passed via React Router's location.state. ActionCenter
// reads it on mount and renders the modal, then clears the state.
export default function CreateTicketButton({ detected, onNavigate }) {
  const navigate = useNavigate()

  function handleClick() {
    onNavigate?.(detected)
    // Match the shape ActionRequestModal expects in its `prefill` prop. The
    // `request` field seeds the natural-language description; the rest map
    // straight onto the guided-form inputs.
    const prefill = {
      request: detected.title,
      action_type: detected.action_type,
      target_resource: detected.target_resource,
      target_environment: detected.target_environment,
      severity: detected.severity,
      justification: detected.description,
      requesting_team: '',
      chat_session_id: detected.session_id || null,
      source: 'CHAT_AUTO_TICKET',
    }
    navigate('/actions', { state: { prefill } })
  }

  return (
    <div className="mt-2 flex flex-col gap-1">
      <button
        onClick={handleClick}
        className="self-start inline-flex items-center gap-2 bg-indigo-600 hover:bg-indigo-500 text-white px-3 py-1.5 rounded-lg text-xs font-semibold transition-colors"
      >
        <Ticket size={13} /> Create Ticket
      </button>
      <p className="text-[10px] text-slate-500 ml-0.5">
        Opens Action Center pre-filled with: <span className="text-slate-700">{detected.title}</span>
      </p>
    </div>
  )
}
