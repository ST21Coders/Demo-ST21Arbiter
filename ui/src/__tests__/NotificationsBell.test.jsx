import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import { PersonaProvider } from '../contexts/PersonaContext'
import NotificationsBell, {
  pickTopNotifications,
  timeAgo,
  MAX_NOTIFICATIONS,
} from '../components/NotificationsBell'

// Drive the active persona via the same module-mock idiom as
// tokenTracking.test.jsx — PersonaProvider derives from getGroups() at mount.
const mocks = vi.hoisted(() => ({
  groups: ['ciso'],
  email:  'ciso_diana@meridianinsurance.com',
}))
vi.mock('../hooks/useAuth', () => ({
  getGroups:        () => mocks.groups,
  getEmail:         () => mocks.email,
  getSessionExpiry: () => null,
  isAuthenticated:  () => true,
  signOut:          vi.fn(),
  signIn:           vi.fn(),
  authHeaders:      () => ({}),
  refresh:          () => Promise.resolve(''),
  getIdToken:       () => '',
  isDevAuth:        () => false,
  getDevPersonaId:  () => null,
  setDevPersona:    vi.fn(),
}))

beforeEach(() => {
  mocks.groups = ['ciso']
})

const HOUR = 3600_000
function finding(id, { hoursAgo = 1, severity = 'HIGH', status = 'OPEN', title } = {}) {
  return {
    conflict_id: id,
    severity,
    status,
    title: title || `Finding ${id}`,
    detected_at: new Date(Date.now() - hoursAgo * HOUR).toISOString(),
  }
}

function LocationProbe() {
  const location = useLocation()
  return <div data-testid="loc">{location.pathname}</div>
}

function renderBell(openFindings) {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <PersonaProvider>
        <NotificationsBell openFindings={openFindings} />
        <Routes>
          <Route path="*" element={<LocationProbe />} />
        </Routes>
      </PersonaProvider>
    </MemoryRouter>,
  )
}

// ── pickTopNotifications (pure) ───────────────────────────────────────────────
describe('pickTopNotifications', () => {
  it('orders newest-first by detected_at regardless of severity', () => {
    const input = [
      finding('OLD-CRIT',  { hoursAgo: 48, severity: 'CRITICAL' }),
      finding('NEW-LOW',   { hoursAgo: 1,  severity: 'LOW' }),
      finding('MID-HIGH',  { hoursAgo: 24, severity: 'HIGH' }),
    ]
    const ids = pickTopNotifications(input).map(f => f.conflict_id)
    expect(ids).toEqual(['NEW-LOW', 'MID-HIGH', 'OLD-CRIT'])
  })

  it('includes only OPEN findings', () => {
    const input = [
      finding('A', { hoursAgo: 1 }),
      finding('B', { hoursAgo: 2, status: 'RESOLVED' }),
      finding('C', { hoursAgo: 3 }),
    ]
    expect(pickTopNotifications(input).map(f => f.conflict_id)).toEqual(['A', 'C'])
  })

  it(`caps the list at ${MAX_NOTIFICATIONS}`, () => {
    const input = Array.from({ length: 10 }, (_, i) => finding(`F${i}`, { hoursAgo: i + 1 }))
    const out = pickTopNotifications(input)
    expect(out).toHaveLength(MAX_NOTIFICATIONS)
    expect(out[0].conflict_id).toBe('F0') // newest survives the cap
  })

  it('sinks rows with missing/unparseable detected_at below dated rows', () => {
    const input = [
      { conflict_id: 'NO-DATE', severity: 'CRITICAL', status: 'OPEN', title: 'x' },
      finding('DATED', { hoursAgo: 100 }),
      { conflict_id: 'BAD-DATE', severity: 'HIGH', status: 'OPEN', title: 'y', detected_at: 'not-a-date' },
    ]
    const ids = pickTopNotifications(input).map(f => f.conflict_id)
    expect(ids[0]).toBe('DATED')
    expect(ids.slice(1)).toEqual(expect.arrayContaining(['NO-DATE', 'BAD-DATE']))
  })

  it('does not mutate the input array', () => {
    const input = [finding('A', { hoursAgo: 2 }), finding('B', { hoursAgo: 1 })]
    const snapshot = input.map(f => f.conflict_id)
    pickTopNotifications(input)
    expect(input.map(f => f.conflict_id)).toEqual(snapshot)
  })

  it('tolerates non-array and empty input', () => {
    expect(pickTopNotifications(undefined)).toEqual([])
    expect(pickTopNotifications(null)).toEqual([])
    expect(pickTopNotifications([])).toEqual([])
  })
})

// ── timeAgo (pure) ────────────────────────────────────────────────────────────
describe('timeAgo', () => {
  const now = Date.parse('2026-07-07T12:00:00Z')
  it('formats seconds, minutes, hours and days', () => {
    expect(timeAgo('2026-07-07T11:59:30Z', now)).toBe('just now')
    expect(timeAgo('2026-07-07T11:15:00Z', now)).toBe('45m ago')
    expect(timeAgo('2026-07-07T07:00:00Z', now)).toBe('5h ago')
    expect(timeAgo('2026-07-04T12:00:00Z', now)).toBe('3d ago')
  })
  it('returns empty string on missing/unparseable input', () => {
    expect(timeAgo(undefined, now)).toBe('')
    expect(timeAgo('garbage', now)).toBe('')
  })
})

// ── RBAC gating ───────────────────────────────────────────────────────────────
describe('NotificationsBell — RBAC gating', () => {
  it.each(['ciso', 'soc', 'grc'])('renders the bell for the %s persona', (id) => {
    mocks.groups = [id]
    renderBell([finding('A')])
    expect(screen.getByTitle('Notifications')).toBeInTheDocument()
  })

  it('is absent for the employee persona (no findings/actions access)', () => {
    mocks.groups = ['employee']
    renderBell([finding('A')])
    expect(screen.queryByTitle('Notifications')).not.toBeInTheDocument()
  })

  it('is absent when the user has no persona group at all', () => {
    mocks.groups = []
    renderBell([finding('A')])
    expect(screen.queryByTitle('Notifications')).not.toBeInTheDocument()
  })
})

// ── Badge dot ─────────────────────────────────────────────────────────────────
describe('NotificationsBell — badge dot', () => {
  it('shows the red dot only when open findings exist', () => {
    renderBell([finding('A')])
    expect(screen.getByTestId('notifications-dot')).toBeInTheDocument()
  })
  it('hides the red dot when there are no open findings', () => {
    renderBell([])
    expect(screen.queryByTestId('notifications-dot')).not.toBeInTheDocument()
  })
})

// ── Panel behavior ────────────────────────────────────────────────────────────
describe('NotificationsBell — panel', () => {
  it('opens on click, lists rows newest-first with severity badges, capped at 6', () => {
    const input = [
      finding('OLDEST', { hoursAgo: 70, severity: 'CRITICAL' }),
      ...Array.from({ length: 6 }, (_, i) => finding(`F${i}`, { hoursAgo: i + 1 })),
    ]
    renderBell(input)
    fireEvent.click(screen.getByTitle('Notifications'))

    const panel = screen.getByTestId('notifications-panel')
    const rows = within(panel).getAllByRole('listitem')
    expect(rows).toHaveLength(6)
    expect(rows[0].textContent).toContain('Finding F0')
    // The 70h-old CRITICAL is pushed out by six fresher findings.
    expect(within(panel).queryByText('Finding OLDEST')).not.toBeInTheDocument()
    // Header reflects the full open count, not the visible slice.
    expect(within(panel).getByText('7 open findings')).toBeInTheDocument()
  })

  it('shows an empty state when there are no open findings', () => {
    renderBell([])
    fireEvent.click(screen.getByTitle('Notifications'))
    expect(screen.getByText('No open findings.')).toBeInTheDocument()
  })

  it('closes on Escape', () => {
    renderBell([finding('A')])
    fireEvent.click(screen.getByTitle('Notifications'))
    expect(screen.getByTestId('notifications-panel')).toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByTestId('notifications-panel')).not.toBeInTheDocument()
  })

  it('closes on mouse-down outside the component', () => {
    renderBell([finding('A')])
    fireEvent.click(screen.getByTitle('Notifications'))
    fireEvent.mouseDown(document.body)
    expect(screen.queryByTestId('notifications-panel')).not.toBeInTheDocument()
  })

  it('navigates to the finding detail page on row click and closes the panel', () => {
    renderBell([finding('ARBITER-UC99')])
    fireEvent.click(screen.getByTitle('Notifications'))
    fireEvent.click(screen.getByText('Finding ARBITER-UC99'))
    expect(screen.getByTestId('loc').textContent).toBe('/findings/ARBITER-UC99')
    expect(screen.queryByTestId('notifications-panel')).not.toBeInTheDocument()
  })

  it('navigates to /findings via the view-all footer', () => {
    renderBell([finding('A')])
    fireEvent.click(screen.getByTitle('Notifications'))
    fireEvent.click(screen.getByText('View all findings →'))
    expect(screen.getByTestId('loc').textContent).toBe('/findings')
  })
})
