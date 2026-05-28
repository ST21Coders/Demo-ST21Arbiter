import { Shield, LogIn } from 'lucide-react'
import { signIn, isAuthenticated } from '../hooks/useAuth'
import { Navigate } from 'react-router-dom'

export default function SignIn() {
  // If a stale session is still valid, send the user straight in.
  if (isAuthenticated()) return <Navigate to="/" replace />

  return (
    <div className="min-h-screen w-full flex items-center justify-center px-6"
         style={{ background: 'linear-gradient(135deg, #f8fafc 0%, #e2e8f0 100%)' }}>
      <div className="w-full max-w-md bg-white rounded-2xl shadow-lg border border-slate-200 p-8">
        <div className="flex items-center gap-3 mb-6">
          <div className="w-10 h-10 rounded-xl flex items-center justify-center"
               style={{ background: 'linear-gradient(135deg, #4f46e5, #3730a3)' }}>
            <Shield size={18} className="text-white" />
          </div>
          <div>
            <p className="text-lg font-bold text-slate-900 tracking-widest leading-none">ARBITER</p>
            <p className="text-[11px] text-slate-500 mt-1">AI Governance Engine · Meridian Insurance</p>
          </div>
        </div>

        <h1 className="text-base font-semibold text-slate-900 mb-1">Sign in to continue</h1>
        <p className="text-sm text-slate-600 leading-relaxed mb-6">
          Use your Cognito credentials. Your role and accessible pages are
          determined by the group your account belongs to.
        </p>

        <button
          onClick={signIn}
          className="w-full flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg text-sm font-medium text-white transition-colors"
          style={{ background: '#4f46e5' }}
          onMouseEnter={e => { e.currentTarget.style.background = '#4338ca' }}
          onMouseLeave={e => { e.currentTarget.style.background = '#4f46e5' }}
        >
          <LogIn size={14} />
          Sign in with Cognito
        </button>

        <p className="text-[11px] text-slate-400 mt-6 text-center">
          You will be redirected to the Cognito Hosted UI.
        </p>
      </div>
    </div>
  )
}
