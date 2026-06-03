import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { PersonaProvider, usePersona } from '../contexts/PersonaContext'
import Settings from '../pages/Settings'
import { __reloadPreferences, setPreference } from '../hooks/usePreferences'

// Control the Cognito-derived identity. PersonaProvider resolves the persona
// from getGroups() (DEV_AUTH is false in the test env), so swapping the mocked
// groups before render is enough to drive every section's gating.
const mocks = vi.hoisted(() => ({ groups: ['grc'], email: 'grc_priya@meridianinsurance.com', expiry: null }))
vi.mock('../hooks/useAuth', () => ({
  getGroups: () => mocks.groups,
  getEmail: () => mocks.email,
  getSessionExpiry: () => mocks.expiry,
  signOut: vi.fn(),
}))

function renderSettings(entry = '/settings') {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <PersonaProvider>
        <Settings />
      </PersonaProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  mocks.groups = ['grc']
  mocks.email = 'grc_priya@meridianinsurance.com'
  mocks.expiry = null
  localStorage.clear()
  __reloadPreferences()
})

describe('Settings — section gating', () => {
  it('renders all universal sections for a non-admin persona, but NOT Advanced', () => {
    renderSettings()
    for (const label of ['Account & Identity', 'Appearance', 'Notifications', 'Session & Security', 'Workspace & Environment']) {
      expect(screen.getByRole('button', { name: new RegExp(label, 'i') })).toBeInTheDocument()
    }
    expect(screen.queryByRole('button', { name: /Advanced/i })).toBeNull()
  })

  it('shows the Advanced section for the CISO (admin) persona', () => {
    mocks.groups = ['ciso']
    renderSettings()
    expect(screen.getByRole('button', { name: /Advanced/i })).toBeInTheDocument()
  })

  it('renders gracefully when the user has no persona group', () => {
    mocks.groups = []
    mocks.email = 'nobody@meridianinsurance.com'
    renderSettings()
    // Account section degrades to the "Unassigned" state.
    expect(screen.getByText(/Unassigned/i)).toBeInTheDocument()
    expect(screen.getByText(/nobody@meridianinsurance.com/)).toBeInTheDocument()
  })
})

describe('Settings — Account is read-only (no role mutation)', () => {
  it('exposes no role-mutating control and shows the read-only note', () => {
    renderSettings() // default active section is Account
    // The Account view must contain no <select>/combobox (the only selects in
    // Settings live in Appearance) and no persona <option>s.
    expect(screen.queryByRole('combobox')).toBeNull()
    expect(screen.queryAllByRole('option')).toHaveLength(0)
    expect(screen.getByText(/cannot be changed here/i)).toBeInTheDocument()
    expect(screen.getByText(/sign out and sign in as another user/i)).toBeInTheDocument()
  })
})

describe('Settings — hash deep-linking', () => {
  it('selects the section named in the URL hash', () => {
    renderSettings('/settings#appearance')
    // Appearance renders its landing-page picker (a combobox).
    expect(screen.getByText(/Default landing page/i)).toBeInTheDocument()
    expect(screen.getByRole('combobox')).toBeInTheDocument()
  })

  it('falls back to the first section when the hash targets a hidden section', () => {
    // grc cannot see Advanced; #advanced must fall back to Account.
    renderSettings('/settings#advanced')
    expect(screen.getByText(/cannot be changed here/i)).toBeInTheDocument()     // Account content
    expect(screen.queryByText(/Platform operations/i)).toBeNull()              // not Advanced content
  })

  it('honors #advanced for the CISO persona', () => {
    mocks.groups = ['ciso']
    renderSettings('/settings#advanced')
    expect(screen.getByText(/Platform operations/i)).toBeInTheDocument()
  })
})

// Tiny consumer to read the provider's firstAccessiblePath() closure, which now
// consults the landingPath preference.
function HomeProbe() {
  const { firstAccessiblePath } = usePersona()
  return <div data-testid="home">{firstAccessiblePath()}</div>
}

function renderProbe() {
  return render(
    <MemoryRouter>
      <PersonaProvider>
        <HomeProbe />
      </PersonaProvider>
    </MemoryRouter>,
  )
}

describe('firstAccessiblePath — landing-page preference', () => {
  it('returns the pinned landing page when the persona can reach it', () => {
    setPreference('appearance.landingPath', '/findings') // grc has findings access
    renderProbe()
    expect(screen.getByTestId('home')).toHaveTextContent('/findings')
  })

  it('ignores an inaccessible pinned page and uses the role default', () => {
    setPreference('appearance.landingPath', '/pipeline') // grc has NO pipeline access
    renderProbe()
    expect(screen.getByTestId('home')).toHaveTextContent('/') // grc role default = dashboard
  })

  it('uses the role default when no landing page is pinned', () => {
    renderProbe()
    expect(screen.getByTestId('home')).toHaveTextContent('/')
  })
})
