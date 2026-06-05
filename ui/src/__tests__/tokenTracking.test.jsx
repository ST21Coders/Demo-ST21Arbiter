import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { PersonaProvider, usePersona } from '../contexts/PersonaContext'
import Sidebar from '../components/Sidebar'
import TokenTracking from '../pages/TokenTracking'

// Drive the active persona via the same module-mock idiom as settings.test.jsx.
// Setting mocks.groups before each render is enough — PersonaProvider derives
// the persona from getGroups() at mount time.
const mocks = vi.hoisted(() => ({
  groups: ['ciso'],
  email:  'ciso_diana@meridianinsurance.com',
  expiry: null,
}))
vi.mock('../hooks/useAuth', () => ({
  getGroups:        () => mocks.groups,
  getEmail:         () => mocks.email,
  getSessionExpiry: () => mocks.expiry,
  isAuthenticated:  () => true,
  signOut:          vi.fn(),
  signIn:           vi.fn(),
  authHeaders:      () => ({}),
  refresh:          () => Promise.resolve(''),
  getIdToken:       () => '',
  // Dev-persona switcher exports: present but inert in tests — we drive
  // persona via mocks.groups directly, not via sessionStorage.
  isDevAuth:        () => false,
  getDevPersonaId:  () => null,
  setDevPersona:    vi.fn(),
}))

// Recharts measures its parent via getBoundingClientRect; jsdom returns
// zeros, so charts collapse to nothing and queries get flaky. Stub the
// surface we use with thin pass-through components so the page renders
// deterministically and we can still assert chart sections mount.
vi.mock('recharts', () => {
  const Stub = ({ children }) => <div data-testid="recharts-stub">{children}</div>
  const Null = () => null
  return {
    ResponsiveContainer: Stub,
    AreaChart: Stub,
    BarChart: Stub,
    Area: Null, Bar: Null, Cell: Null,
    XAxis: Null, YAxis: Null,
    Tooltip: Null, CartesianGrid: Null, Legend: Null,
  }
})

beforeEach(() => {
  mocks.groups = ['ciso']
  mocks.email  = 'ciso_diana@meridianinsurance.com'
})

// ── Helpers ───────────────────────────────────────────────────────────────────

function renderSidebar() {
  return render(
    <MemoryRouter>
      <PersonaProvider>
        <Sidebar />
      </PersonaProvider>
    </MemoryRouter>,
  )
}

// Mirrors App.jsx::Guarded so we exercise the same hasAccess() decision the
// router does at runtime, without having to mount the whole <Shell>. Renders
// "Access restricted" text on denial — matches the user-visible AccessDenied
// panel in App.jsx so assertions stay realistic.
function GuardedRender({ path, children }) {
  const { hasAccess } = usePersona()
  return hasAccess(path) ? children : <div>Access restricted</div>
}

function renderTokenTrackingGuarded() {
  return render(
    <MemoryRouter>
      <PersonaProvider>
        <GuardedRender path="/token-usage">
          <TokenTracking />
        </GuardedRender>
      </PersonaProvider>
    </MemoryRouter>,
  )
}

// ── 1. Sidebar gating ────────────────────────────────────────────────────────
describe('TokenTracking — sidebar gating', () => {
  it.each([
    { groups: ['soc'],      label: 'SOC',      shouldSee: false },
    { groups: ['grc'],      label: 'GRC',      shouldSee: false },
    { groups: ['employee'], label: 'Employee', shouldSee: false },
    { groups: ['ciso'],     label: 'CISO',     shouldSee: true  },
  ])('persona $label → sidebar item present: $shouldSee', ({ groups, shouldSee }) => {
    mocks.groups = groups
    renderSidebar()
    const link = screen.queryByRole('link', { name: /Token Tracking/i })
    if (shouldSee) expect(link).toBeInTheDocument()
    else           expect(link).toBeNull()
  })
})

// ── 2. Route gating ──────────────────────────────────────────────────────────
describe('TokenTracking — route gating via <Guarded>', () => {
  it.each([
    { groups: ['soc'],      label: 'SOC' },
    { groups: ['grc'],      label: 'GRC' },
    { groups: ['employee'], label: 'Employee' },
  ])('persona $label → AccessDenied at /token-usage', ({ groups }) => {
    mocks.groups = groups
    renderTokenTrackingGuarded()
    expect(screen.getByText(/Access restricted/i)).toBeInTheDocument()
    // And the page header must NOT have rendered
    expect(screen.queryByRole('heading', { name: /Token Tracking/i })).toBeNull()
  })

  it('persona CISO → page renders', async () => {
    mocks.groups = ['ciso']
    renderTokenTrackingGuarded()
    expect(screen.queryByText(/Access restricted/i)).toBeNull()
    // Page header is an <h1> — disambiguates from the sidebar link if it were here
    expect(screen.getByRole('heading', { name: /Token Tracking/i })).toBeInTheDocument()
  })
})

// ── 3. Mock data render ──────────────────────────────────────────────────────
describe('TokenTracking — mock data render', () => {
  it('renders KPI strip with non-zero values, all 3 chart cards, and table rows', async () => {
    mocks.groups = ['ciso']
    renderTokenTrackingGuarded()

    // KPI labels are always rendered; values fill in after the hook resolves.
    expect(screen.getByText(/Tokens \(range\)/i)).toBeInTheDocument()
    expect(screen.getByText(/Estimated cost/i)).toBeInTheDocument()
    expect(screen.getByText(/Avg tokens \/ chat/i)).toBeInTheDocument()
    expect(screen.getByText(/Guardrail-blocked/i)).toBeInTheDocument()

    // After the 150ms mock sleep, the table is populated.
    const rows = await screen.findAllByRole('row', {}, { timeout: 2000 })
    // header + ≥1 data rows. Spec acceptance criterion: ≥50 rows for 7d default.
    expect(rows.length).toBeGreaterThan(50)

    // All three chart sections present.
    expect(screen.getByText('Tokens over time')).toBeInTheDocument()
    expect(screen.getByText('Tokens by agent')).toBeInTheDocument()
    expect(screen.getByText('Tokens by persona')).toBeInTheDocument()

    // KPI numeric value is non-zero (tokens span 7d, must be > 0).
    // Tokens (range) card's value sits under the label — find via the card's text content.
    const tokensCard = screen.getByText(/Tokens \(range\)/i).closest('div')
    expect(tokensCard).not.toBeNull()
    // Value formats as e.g. "1.2M" or "350K" — at minimum it must not read "0".
    expect(within(tokensCard).queryByText(/^0$/)).toBeNull()
  })
})

// ── 4. Filter behavior ───────────────────────────────────────────────────────
describe('TokenTracking — filters', () => {
  it('narrows the table when agent filter is set to sharepoint', async () => {
    mocks.groups = ['ciso']
    renderTokenTrackingGuarded()
    // Wait for the first load to settle.
    await screen.findAllByRole('row', {}, { timeout: 2000 })

    // The two <select>s are the only ones on the page. First is agent, second is persona.
    const selects = screen.getAllByRole('combobox')
    expect(selects.length).toBeGreaterThanOrEqual(2)
    const agentSelect = selects[0]
    fireEvent.change(agentSelect, { target: { value: 'sharepoint' } })

    // Wait for the post-filter render — spinner clears, table repopulates.
    // findAllByText waits up to the timeout for at least one match.
    const sharepointCells = await screen.findAllByText('sharepoint', {}, { timeout: 2000 })
    expect(sharepointCells.length).toBeGreaterThan(0)

    // And no row's Agent column should read "master" after the filter.
    expect(screen.queryAllByText('master').length).toBe(0)
  })
})

// ── 5. CSV export ────────────────────────────────────────────────────────────
describe('TokenTracking — CSV export', () => {
  let originalCreate
  let originalRevoke
  let originalClick
  beforeEach(() => {
    originalCreate = global.URL.createObjectURL
    originalRevoke = global.URL.revokeObjectURL
    originalClick  = HTMLAnchorElement.prototype.click
    global.URL.createObjectURL = vi.fn(() => 'blob:mock')
    global.URL.revokeObjectURL = vi.fn()
    // Suppress the jsdom "Not implemented: navigation" warning that a real
    // <a>.click() triggers — we only care that the blob was created.
    HTMLAnchorElement.prototype.click = vi.fn()
  })
  afterEach(() => {
    global.URL.createObjectURL = originalCreate
    global.URL.revokeObjectURL = originalRevoke
    HTMLAnchorElement.prototype.click = originalClick
  })

  it('clicking Export CSV invokes URL.createObjectURL', async () => {
    mocks.groups = ['ciso']
    renderTokenTrackingGuarded()
    // Wait until the data loaded so the export has something to write.
    await screen.findAllByRole('row', {}, { timeout: 2000 })

    const exportBtn = screen.getByRole('button', { name: /Export CSV/i })
    fireEvent.click(exportBtn)
    expect(global.URL.createObjectURL).toHaveBeenCalledTimes(1)
    expect(global.URL.revokeObjectURL).toHaveBeenCalledTimes(1)
  })
})
