import { useEffect, useState } from 'react'
import { BrowserRouter, Routes, Route, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { PersonaProvider, usePersona } from './contexts/PersonaContext'
import { handleCallback, isAuthenticated } from './hooks/useAuth'
import { usePreferences, getPreferences, setPreference, resolveTheme } from './hooks/usePreferences'
import Sidebar from './components/Sidebar'
import TopBar from './components/TopBar'
import Dashboard from './pages/Dashboard'
import Findings from './pages/Findings'
import FindingDetail from './pages/FindingDetail'
import HeatMap from './pages/HeatMap'
import ActionCenter from './pages/ActionCenter'
import Governance from './pages/Governance'
import Reports from './pages/Reports'
import DataPipeline from './pages/DataPipeline'
import DataGrouping from './pages/DataGrouping'
import ConfigDrift from './pages/ConfigDrift'
import AuditLogs from './pages/AuditLogs'
import AnalystView from './pages/AnalystView'
import WhatIf from './pages/WhatIf'
import LLMControl from './pages/LLMControl'
import MCPChat from './pages/MCPChat'
import Integrations from './pages/Integrations'
import ImpactAnalysis from './pages/ImpactAnalysis'
import CmdbDrift from './pages/CmdbDrift'
import TokenTracking from './pages/TokenTracking'
import Personas from './pages/Personas'
import Settings from './pages/Settings'
import SignIn from './pages/SignIn'
import { Lock } from 'lucide-react'

// Cognito Hosted UI redirects here with ?code=... — exchange it for tokens,
// stash them in sessionStorage, then bounce to the home page. Without this
// route, the auth flow never closes and every API call sees a 401.
function Callback() {
  const navigate = useNavigate()
  const [status, setStatus] = useState('exchanging…')

  useEffect(() => {
    handleCallback()
      .then(() => navigate(getPreferences().appearance.landingPath || '/', { replace: true }))
      .catch(err => setStatus(`Sign-in failed: ${err.message || err}`))
  }, [navigate])

  return (
    <div className="min-h-screen flex items-center justify-center bg-slate-50">
      <p className="text-sm text-slate-500">{status}</p>
    </div>
  )
}

// Applies the user's theme preference to <html data-theme> for the whole app
// (including the public SignIn/Callback screens). 'system' follows the OS and
// live-updates. matchMedia is guarded for non-browser envs.
function ThemeManager() {
  const { appearance } = usePreferences()
  useEffect(() => {
    const mq = typeof window !== 'undefined' && window.matchMedia
      ? window.matchMedia('(prefers-color-scheme: dark)')
      : null
    const apply = () => {
      document.documentElement.dataset.theme = resolveTheme(appearance.theme, mq ? mq.matches : false)
    }
    apply()
    if (appearance.theme === 'system' && mq?.addEventListener) {
      mq.addEventListener('change', apply)
      return () => mq.removeEventListener('change', apply)
    }
  }, [appearance.theme])
  return null
}

// Wraps the authenticated app shell. Unauthenticated visitors bounce to
// /signin (the public landing page). Authenticated users see the full shell.
function RequireAuth({ children }) {
  if (!isAuthenticated()) return <Navigate to="/signin" replace />
  return children
}

function NotFound() {
  return (
    <div className="flex-1 flex items-center justify-center h-full">
      <div className="text-center">
        <p className="text-5xl font-bold text-slate-300 mb-3">404</p>
        <p className="text-slate-500 text-sm">Page not found</p>
      </div>
    </div>
  )
}

function AccessDenied() {
  const { persona, firstAccessiblePath } = usePersona()
  return (
    <div className="flex-1 flex items-center justify-center h-full p-6">
      <div className="max-w-md text-center bg-white border border-slate-200 rounded-xl p-8 shadow-sm">
        <div className="w-12 h-12 rounded-full bg-amber-50 border border-amber-200 flex items-center justify-center mx-auto mb-4">
          <Lock size={20} className="text-amber-700" />
        </div>
        <p className="text-lg font-bold text-slate-900 mb-1">Access restricted</p>
        <p className="text-sm text-slate-600 mb-1">
          {persona
            ? <>Your role <span className="font-semibold text-slate-900">{persona.role}</span> does not have access to this page.</>
            : <>Your account is not assigned to a persona group, so most pages are unavailable.</>}
        </p>
        <p className="text-xs text-slate-500 mb-4">
          Return to your home page or sign in as a different user.
        </p>
        <a href={firstAccessiblePath()} className="btn-primary inline-flex items-center gap-1.5 text-xs">
          Go to my home
        </a>
      </div>
    </div>
  )
}

function Guarded({ path, children }) {
  const { hasAccess } = usePersona()
  return hasAccess(path) ? children : <AccessDenied />
}

function PersonaRouteSync() {
  const { hasAccess, firstAccessiblePath, personaId } = usePersona()
  const location = useLocation()
  const navigate = useNavigate()

  useEffect(() => {
    // Self-heal a stale default-landing-page preference: if the persona can no
    // longer reach the pinned page, clear it (effect = render-safe).
    const landingPath = getPreferences().appearance.landingPath
    if (landingPath && !hasAccess(landingPath)) setPreference('appearance.landingPath', null)

    // When the active persona changes, if current page is no longer accessible,
    // bounce them to their primary landing page.
    if (location.pathname === '/personas') return
    if (!hasAccess(location.pathname)) {
      navigate(firstAccessiblePath(), { replace: true })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [personaId])

  return null
}

function Shell() {
  // Personal appearance preferences drive root-level data attributes that CSS
  // keys off (see index.css): compact density on dense tables, reduced motion.
  // (Theme is applied to <html> by ThemeManager at the App root.)
  const { appearance } = usePreferences()
  return (
    <div
      data-density={appearance.density}
      data-reduce-motion={appearance.reduceMotion ? 'true' : 'false'}
      className="flex h-screen overflow-hidden bg-slate-50 text-slate-900"
    >
      <PersonaRouteSync />
      <Sidebar />
      <div className="flex-1 flex flex-col overflow-hidden">
        <TopBar />
        <main className="flex-1 overflow-y-auto">
          <Routes>
            <Route path="/"            element={<Guarded path="/"><Dashboard /></Guarded>} />
            <Route path="/findings"    element={<Guarded path="/findings"><Findings /></Guarded>} />
            <Route path="/findings/:id" element={<Guarded path="/findings"><FindingDetail /></Guarded>} />
            <Route path="/heatmap"     element={<Guarded path="/heatmap"><HeatMap /></Guarded>} />
            <Route path="/actions"     element={<Guarded path="/actions"><ActionCenter /></Guarded>} />
            <Route path="/governance"  element={<Guarded path="/governance"><Governance /></Guarded>} />
            <Route path="/reports"     element={<Guarded path="/reports"><Reports /></Guarded>} />
            <Route path="/pipeline"    element={<Guarded path="/pipeline"><DataPipeline /></Guarded>} />
            <Route path="/data-grouping" element={<Guarded path="/data-grouping"><DataGrouping /></Guarded>} />
            <Route path="/config-drift" element={<Guarded path="/config-drift"><ConfigDrift /></Guarded>} />
            <Route path="/audit"       element={<Guarded path="/audit"><AuditLogs /></Guarded>} />
            <Route path="/analyst"     element={<Guarded path="/analyst"><AnalystView /></Guarded>} />
            <Route path="/whatif"      element={<Guarded path="/whatif"><WhatIf /></Guarded>} />
            <Route path="/llm-control" element={<Guarded path="/llm-control"><LLMControl /></Guarded>} />
            <Route path="/mcp-chat"    element={<Guarded path="/mcp-chat"><MCPChat /></Guarded>} />
            <Route path="/integrations" element={<Guarded path="/integrations"><Integrations /></Guarded>} />
            <Route path="/impact-analysis" element={<Guarded path="/impact-analysis"><ImpactAnalysis /></Guarded>} />
            <Route path="/cmdb-drift"  element={<Guarded path="/cmdb-drift"><CmdbDrift /></Guarded>} />
            <Route path="/token-usage" element={<Guarded path="/token-usage"><TokenTracking /></Guarded>} />
            <Route path="/personas"    element={<Personas />} />
            {/* Universal — no <Guarded> wrapper (like /personas). Every
                authenticated persona can reach Settings; sections inside
                self-gate by route access. */}
            <Route path="/settings"    element={<Settings />} />
            <Route path="*"            element={<NotFound />} />
          </Routes>
        </main>
      </div>
    </div>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <ThemeManager />
      <Routes>
        {/* Public — these MUST be reachable while unauthenticated, so they
            sit outside <RequireAuth> and outside the Shell chrome. */}
        <Route path="/signin"   element={<SignIn />} />
        <Route path="/callback" element={<Callback />} />
        {/* Everything else lives under the authenticated Shell. PersonaProvider
            reads cognito:groups from the IdToken, so it must mount AFTER
            RequireAuth confirms a token exists. */}
        <Route path="/*" element={
          <RequireAuth>
            <PersonaProvider>
              <Shell />
            </PersonaProvider>
          </RequireAuth>
        } />
      </Routes>
    </BrowserRouter>
  )
}
