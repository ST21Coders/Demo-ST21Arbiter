import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { PersonaProvider, usePersona } from '../contexts/PersonaContext'
import Sidebar from '../components/Sidebar'
import TokenTracking, { byPersonaWithCost } from '../pages/TokenTracking'

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

// ── 6. user_email distinct from user_id ──────────────────────────────────────
// Live-mode acceptance ("the two fields are distinct") is covered by manual
// backend smoke. Mock records do not carry a `user_id` field, so this test
// asserts only the email-shaped value flows through the User column.
describe('TokenTracking — user_email distinct from user_id', () => {
  it('User column cells contain email-shaped values (mock CISO)', async () => {
    mocks.groups = ['ciso']
    renderTokenTrackingGuarded()

    // Wait for the data load to populate the records table.
    const rows = await screen.findAllByRole('row', {}, { timeout: 2000 })
    expect(rows.length).toBeGreaterThan(1)

    // At least one cell anywhere on the page must be email-shaped — this
    // covers both the records-table User column and the per-user breakdown
    // card. The inverse of the bug we are fixing (empty email column).
    const emailCells = screen.getAllByText(/@/)
    expect(emailCells.length).toBeGreaterThan(0)
  })
})

// ── 7. Per-user breakdown card ───────────────────────────────────────────────
describe('TokenTracking — per-user breakdown card', () => {
  it('renders a sorted user table with email rows', async () => {
    mocks.groups = ['ciso']
    renderTokenTrackingGuarded()

    // Wait for records to populate so the card is rendered (gated on !loading).
    await screen.findAllByRole('row', {}, { timeout: 2000 })

    // Find the per-user card by its heading text (exact text from TokenTracking.jsx).
    const heading = await screen.findByText('Token usage by user')
    // The heading lives in the card's header; walk up to the rounded-xl container.
    const card = heading.closest('div.rounded-xl')
    expect(card).not.toBeNull()

    // Header row + at least 4 data rows (mock data carries 4 distinct users).
    const cardRows = within(card).getAllByRole('row')
    expect(cardRows.length).toBeGreaterThanOrEqual(5)

    // Helper: parse the "Tokens" column's formatted string (e.g. "1.2M",
    // "350.5K", or a raw integer) back to a comparable number.
    function parseTokens(text) {
      const t = (text || '').trim()
      if (t.endsWith('M')) return parseFloat(t) * 1_000_000
      if (t.endsWith('K')) return parseFloat(t) * 1_000
      return parseFloat(t) || 0
    }

    // Tokens column is the 4th column (User · Persona · Chats · Tokens · Cost).
    // Pull data rows (skip header) and verify descending order on tokens.
    const dataRows = cardRows.slice(1)
    const firstCells  = within(dataRows[0]).getAllByRole('cell')
    const secondCells = within(dataRows[1]).getAllByRole('cell')
    expect(firstCells.length).toBe(5)
    const firstTokens  = parseTokens(firstCells[3].textContent)
    const secondTokens = parseTokens(secondCells[3].textContent)
    expect(firstTokens).toBeGreaterThanOrEqual(secondTokens)

    // Every visible user cell (first column of each data row) is email-shaped.
    for (const row of dataRows) {
      const cells = within(row).getAllByRole('cell')
      expect(cells[0].textContent).toMatch(/@/)
    }
  })
})

// ── 8. KPI subtitle reflects persona filter ──────────────────────────────────
describe('TokenTracking — KPI subtitle', () => {
  it('defaults to "Across all personas" when persona filter is all', async () => {
    mocks.groups = ['ciso']
    renderTokenTrackingGuarded()
    // Wait for data so the page has fully rendered.
    await screen.findAllByRole('row', {}, { timeout: 2000 })
    expect(screen.getByText(/Across all personas/i)).toBeInTheDocument()
  })

  it('switches to "CISO · Nova 2 Lite list pricing" when persona filter is ciso', async () => {
    mocks.groups = ['ciso']
    renderTokenTrackingGuarded()
    await screen.findAllByRole('row', {}, { timeout: 2000 })

    // Second <select> is the persona filter (agent is first).
    const selects = screen.getAllByRole('combobox')
    expect(selects.length).toBeGreaterThanOrEqual(2)
    const personaSelect = selects[1]
    fireEvent.change(personaSelect, { target: { value: 'ciso' } })

    // Wait for re-render — subtitle copy flips.
    const subtitle = await screen.findByText(/CISO · Nova 2 Lite/i, {}, { timeout: 2000 })
    expect(subtitle).toBeInTheDocument()
  })
})

// ── 9. byPersonaWithCost reducer ─────────────────────────────────────────────
// Pure-function smoke test for the exported reducer. The Recharts <Tooltip>
// stub returns null in tests so the cost UI is unobservable via DOM — testing
// the reducer directly is how cost correctness is verified.
describe('TokenTracking — byPersonaWithCost reducer', () => {
  it('returns four entries in canonical order with summed tokens and costs', () => {
    const fixture = [
      // ciso: 2 rows
      { persona: 'ciso',     total_tokens: 1000, estimated_cost: 0.10 },
      { persona: 'ciso',     total_tokens:  500, estimated_cost: 0.05 },
      // soc: 1 row
      { persona: 'soc',      total_tokens: 2000, estimated_cost: 0.20 },
      // grc: 2 rows
      { persona: 'grc',      total_tokens:  300, estimated_cost: 0.03 },
      { persona: 'grc',      total_tokens:  700, estimated_cost: 0.07 },
      // employee: 1 row
      { persona: 'employee', total_tokens:  400, estimated_cost: 0.04 },
    ]

    const result = byPersonaWithCost(fixture)

    // (i) Four entries in canonical persona order.
    expect(result).toHaveLength(4)
    expect(result.map(r => r.persona)).toEqual(['ciso', 'soc', 'grc', 'employee'])

    // (ii) Sum of all four costs equals sum of fixture costs (within 1e-6).
    const totalCost = result.reduce((s, r) => s + r.cost, 0)
    const fixtureCost = fixture.reduce((s, r) => s + r.estimated_cost, 0)
    expect(Math.abs(totalCost - fixtureCost)).toBeLessThan(1e-6)

    // (iii) Per-persona `total` equals sum of total_tokens for that persona.
    const byPersona = {
      ciso:     1500,
      soc:      2000,
      grc:      1000,
      employee:  400,
    }
    for (const entry of result) {
      expect(entry.total).toBe(byPersona[entry.persona])
    }
  })
})
