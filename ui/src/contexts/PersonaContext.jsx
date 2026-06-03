import { createContext, useContext, useMemo } from 'react'
import { getGroups, getEmail } from '../hooks/useAuth'
import { getPreferences } from '../hooks/usePreferences'

export const PERSONAS = {
  employee: {
    id: 'employee',
    name: 'Sarah Chen',
    title: 'Business Analyst',
    email: 'emp_sarah@meridianinsurance.com',
    initials: 'SC',
    role: 'Employee',
    color: '#0ea5e9',
    gradient: 'linear-gradient(135deg, #0ea5e9, #0284c7)',
    access: ['analyst'],
    badge: null,
    description: 'End-user — asks why tools are blocked, gets policy-backed answers via chatbot without needing dashboard access.',
  },
  grc: {
    id: 'grc',
    name: 'Priya Nair',
    title: 'GRC Analyst',
    email: 'grc_priya@meridianinsurance.com',
    initials: 'PN',
    role: 'GRC Analyst',
    color: '#6366f1',
    gradient: 'linear-gradient(135deg, #6366f1, #4338ca)',
    access: ['dashboard', 'findings', 'heatmap', 'governance', 'audit', 'analyst'],
    badge: 'GRC',
    description: 'Runs AI scans, reviews all findings, tracks compliance posture across PCI-DSS/NAIC/SOC2, and uses chatbot for drill-down queries.',
  },
  soc: {
    id: 'soc',
    name: 'Marcus Webb',
    title: 'SOC Analyst',
    email: 'soc_marcus@meridianinsurance.com',
    initials: 'MW',
    role: 'SOC Analyst',
    color: '#f472b6',
    gradient: 'linear-gradient(135deg, #f472b6, #db2777)',
    access: ['dashboard', 'findings', 'heatmap', 'actions', 'audit', 'analyst'],
    badge: 'SOC',
    description: 'Alert-driven — sees new detections on the dashboard, investigates broader exposure via chatbot, initiates remediation actions.',
  },
  ciso: {
    id: 'ciso',
    name: 'Diana Osei',
    title: 'Chief Information Security Officer',
    email: 'ciso_diana@meridianinsurance.com',
    initials: 'DO',
    role: 'CISO',
    color: '#f59e0b',
    gradient: 'linear-gradient(135deg, #f59e0b, #d97706)',
    access: ['dashboard', 'findings', 'heatmap', 'actions', 'governance', 'audit', 'analyst', 'llm-control', 'pipeline', 'mcp-chat'],
    badge: 'CISO',
    description: 'Executive approver — reviews critical findings, approves or rejects change requests requiring CISO sign-off, monitors compliance posture.',
  },
}

// Route path → access key (single source of truth for RBAC)
export const ROUTE_ACCESS = {
  '/':            'dashboard',
  '/findings':    'findings',
  '/heatmap':     'heatmap',
  '/actions':     'actions',
  '/governance':  'governance',
  '/audit':       'audit',
  '/analyst':     'analyst',
  '/llm-control': 'llm-control',
  '/pipeline':    'pipeline',
  '/mcp-chat':    'mcp-chat',
  // '/personas' and '/settings' have no entry — always accessible to any
  // authenticated persona (demo page / personal preferences respectively).
}

const PersonaContext = createContext(null)

// Persona is derived from the Cognito IdToken's `cognito:groups` claim.
// Group names match persona ids: 'employee' | 'grc' | 'soc' | 'ciso'.
// If a user belongs to multiple groups, the most-privileged one wins.
const GROUP_PRIORITY = ['ciso', 'soc', 'grc', 'employee']

function personaIdFromGroups(groups) {
  for (const id of GROUP_PRIORITY) {
    if (groups.includes(id)) return id
  }
  return null
}

export function PersonaProvider({ children }) {
  // Recompute when the component remounts (post-login). No setPersonaId is
  // exposed — the persona is fixed by the JWT for the duration of the session.
  const { personaId, persona, email } = useMemo(() => {
    const id = personaIdFromGroups(getGroups())
    return { personaId: id, persona: id ? PERSONAS[id] : null, email: getEmail() }
  }, [])

  function hasAccess(path) {
    const key = ROUTE_ACCESS[path]
    if (key === undefined) return true   // unguarded route (e.g. /personas)
    if (!persona) return false
    return persona.access.includes(key)
  }

  function firstAccessiblePath() {
    // A user-chosen default landing page (Settings -> Appearance) wins, but only
    // if the persona can still reach it; a stale path falls back to the role
    // default (cleaned up in PersonaRouteSync, keeping this read render-safe).
    const landingPath = getPreferences().appearance.landingPath
    if (landingPath && hasAccess(landingPath)) return landingPath
    const personaPaths = ['/', '/analyst', '/findings', '/actions', '/governance']
    for (const p of personaPaths) {
      if (hasAccess(p)) return p
    }
    return '/personas'
  }

  return (
    <PersonaContext.Provider value={{ persona, personaId, email, PERSONAS, hasAccess, firstAccessiblePath }}>
      {children}
    </PersonaContext.Provider>
  )
}

export function usePersona() {
  return useContext(PersonaContext)
}
