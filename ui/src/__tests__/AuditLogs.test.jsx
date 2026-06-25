import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

// AuditLogs pulls its row set from useAudit() in ../hooks/useApi. Mock that
// hook to return a small, deterministic fixture so the test can drive the
// page's sort UI without standing up an API. We expose a `setLogs` escape
// hatch through a module-level holder in case a test wants to vary the
// fixture; today every test uses the same FIXTURE.
const state = vi.hoisted(() => ({ logs: [] }))
vi.mock('../hooks/useApi', () => ({
  useAudit: () => ({
    logs: state.logs,
    loading: false,
    // load() is called from a useEffect on mount; make it a no-op since the
    // mock already provides `logs` synchronously.
    load: vi.fn(),
  }),
}))

// Fixture chosen to exercise every acceptance criterion in one set:
//   - rows arrive in non-timestamp-desc order on the wire
//   - two rows share `action_type` ("CR_CREATED") to test stability
//   - one row has an empty `user` to test empty-sink-to-bottom (both directions)
//   - distinct timestamps, resources, statuses so each sort is unambiguous
//   - each row has an event_id so the row-expand state is keyed stably
const FIXTURE = [
  {
    event_id: 'e-a',
    timestamp: '2026-01-01T10:00:00Z',
    action_type: 'SCAN_TRIGGERED',
    resource: 'r-a',
    user: 'alice',
    status: 'OK',
    details: '{"note":"a"}',
  },
  {
    event_id: 'e-b',
    timestamp: '2026-01-03T10:00:00Z',
    action_type: 'CR_CREATED',
    resource: 'r-b',
    user: 'Bob',
    status: 'PENDING_APPROVAL',
    details: '{"note":"b"}',
  },
  {
    event_id: 'e-c',
    timestamp: '2026-01-02T10:00:00Z',
    action_type: 'CR_CREATED', // tie with e-b on action_type
    resource: 'r-c',
    user: 'carol',
    status: 'OK',
    details: '{"note":"c"}',
  },
  {
    event_id: 'e-d',
    timestamp: '2026-01-04T10:00:00Z',
    action_type: 'JIRA_LINKED',
    resource: 'r-d',
    user: '', // empty -> must sink to bottom regardless of sort direction
    status: 'OK',
    details: '{"note":"d"}',
  },
]

beforeEach(() => {
  state.logs = FIXTURE.map(r => ({ ...r }))
})

// Lazy-import the page so vi.mock() applies before module evaluation.
async function renderPage() {
  const { default: AuditLogs } = await import('../pages/AuditLogs')
  return render(<AuditLogs />)
}

// Return the data rows (skip header row, skip expanded-detail rows). Expanded
// detail rows have a single <td colSpan={7}>, so they have only one cell;
// real data rows have seven cells.
function dataRows() {
  return screen.getAllByRole('row').filter(r => {
    const cells = within(r).queryAllByRole('cell')
    return cells.length === 7
  })
}

// Read the event_id for a data row from its resource cell (each fixture row
// has a unique `r-x` resource string, so this gives us a stable identity).
function rowResources() {
  return dataRows().map(r => {
    const cells = within(r).getAllByRole('cell')
    // cell order: chevron, timestamp, action, resource, user, status, details
    return cells[3].textContent.trim()
  })
}

function rowUsers() {
  return dataRows().map(r => within(r).getAllByRole('cell')[4].textContent.trim())
}

function rowActions() {
  return dataRows().map(r => within(r).getAllByRole('cell')[2].textContent.trim())
}

// Get a sortable column header (<th>) by its visible label.
function headerByLabel(label) {
  // Each sortable header renders a <button> with the label; the parent <th>
  // is the element that carries aria-sort.
  const btn = screen.getByRole('button', { name: new RegExp(`^${label}$`) })
  return btn.closest('th')
}

describe('AuditLogs — default render', () => {
  it('sorts by Timestamp descending on first mount (aria-sort + row order)', async () => {
    await renderPage()
    // aria-sort on Timestamp is "descending"; the other sortable headers are "none".
    expect(headerByLabel('Timestamp')).toHaveAttribute('aria-sort', 'descending')
    for (const label of ['Action', 'Resource', 'User', 'Status']) {
      expect(headerByLabel(label)).toHaveAttribute('aria-sort', 'none')
    }
    // Newest first: d (01-04), b (01-03), c (01-02), a (01-01).
    expect(rowResources()).toEqual(['r-d', 'r-b', 'r-c', 'r-a'])
  })

  it('non-sortable headers (expand-toggle + Details) carry no aria-sort attribute', async () => {
    await renderPage()
    const allHeaders = screen.getAllByRole('columnheader')
    // 7 columns. The two non-sortable headers (chevron col + Details col)
    // must not have aria-sort.
    const nonSortableNoAriaSort = allHeaders.filter(
      h => !h.querySelector('button'),
    )
    expect(nonSortableNoAriaSort.length).toBe(2)
    for (const h of nonSortableNoAriaSort) {
      expect(h.getAttribute('aria-sort')).toBeNull()
    }
  })
})

describe('AuditLogs — search input removal', () => {
  it('renders no text-entry control on the page', async () => {
    await renderPage()
    // No <input type=text> / role=textbox and no role=searchbox.
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(screen.queryByRole('searchbox')).toBeNull()
  })
})

describe('AuditLogs — clicking the active column flips direction', () => {
  it('clicking Timestamp flips to ascending and reorders oldest -> newest', async () => {
    const user = userEvent.setup()
    await renderPage()
    await user.click(screen.getByRole('button', { name: /^Timestamp$/ }))
    expect(headerByLabel('Timestamp')).toHaveAttribute('aria-sort', 'ascending')
    // Oldest first: a (01-01), c (01-02), b (01-03), d (01-04).
    expect(rowResources()).toEqual(['r-a', 'r-c', 'r-b', 'r-d'])
  })

  it('clicking Timestamp twice returns to descending', async () => {
    const user = userEvent.setup()
    await renderPage()
    await user.click(screen.getByRole('button', { name: /^Timestamp$/ }))
    await user.click(screen.getByRole('button', { name: /^Timestamp$/ }))
    expect(headerByLabel('Timestamp')).toHaveAttribute('aria-sort', 'descending')
    expect(rowResources()).toEqual(['r-d', 'r-b', 'r-c', 'r-a'])
  })
})

describe('AuditLogs — switching to a different sortable column', () => {
  it('clicking User activates ascending sort; Timestamp reverts to aria-sort="none"', async () => {
    const user = userEvent.setup()
    await renderPage()
    await user.click(screen.getByRole('button', { name: /^User$/ }))
    expect(headerByLabel('User')).toHaveAttribute('aria-sort', 'ascending')
    expect(headerByLabel('Timestamp')).toHaveAttribute('aria-sort', 'none')
    // Case-insensitive asc: alice, Bob, carol, then empty user (r-d) at bottom.
    expect(rowUsers()).toEqual(['alice', 'Bob', 'carol', ''])
  })
})

describe('AuditLogs — empty value sinks to bottom regardless of direction', () => {
  it('puts the empty-user row last on User asc', async () => {
    const user = userEvent.setup()
    await renderPage()
    await user.click(screen.getByRole('button', { name: /^User$/ }))
    expect(rowUsers()).toEqual(['alice', 'Bob', 'carol', ''])
  })

  it('still puts the empty-user row last on User desc', async () => {
    const user = userEvent.setup()
    await renderPage()
    // First click -> asc; second click on same column flips to desc.
    await user.click(screen.getByRole('button', { name: /^User$/ }))
    await user.click(screen.getByRole('button', { name: /^User$/ }))
    expect(headerByLabel('User')).toHaveAttribute('aria-sort', 'descending')
    // Desc by user; empty user still last.
    expect(rowUsers()).toEqual(['carol', 'Bob', 'alice', ''])
  })
})

describe('AuditLogs — sort is stable on ties', () => {
  it('preserves input order for rows that share action_type', async () => {
    const user = userEvent.setup()
    await renderPage()
    // Sort by Action asc. e-b ("CR_CREATED") arrives before e-c ("CR_CREATED")
    // in the fixture, so after a stable sort the CR_CREATED tier must show
    // r-b before r-c. (The Action column reformats "CR_CREATED" -> "CR CREATED".)
    await user.click(screen.getByRole('button', { name: /^Action$/ }))
    expect(headerByLabel('Action')).toHaveAttribute('aria-sort', 'ascending')
    expect(rowActions()).toEqual([
      'CR CREATED',
      'CR CREATED',
      'JIRA LINKED',
      'SCAN TRIGGERED',
    ])
    // And specifically: r-b appears before r-c (tie-preservation).
    expect(rowResources()).toEqual(['r-b', 'r-c', 'r-d', 'r-a'])
  })
})

describe('AuditLogs — keyboard activation', () => {
  it('Tab to Timestamp header and press Space to flip; Enter to flip back', async () => {
    const user = userEvent.setup()
    await renderPage()
    const tsButton = screen.getByRole('button', { name: /^Timestamp$/ })
    tsButton.focus()
    expect(tsButton).toHaveFocus()
    // Default is descending. Space -> ascending.
    await user.keyboard(' ')
    expect(headerByLabel('Timestamp')).toHaveAttribute('aria-sort', 'ascending')
    // Enter -> back to descending.
    await user.keyboard('{Enter}')
    expect(headerByLabel('Timestamp')).toHaveAttribute('aria-sort', 'descending')
  })
})

describe('AuditLogs — row expansion still works after a sort change', () => {
  it('clicking a row body after sorting expands its detail panel', async () => {
    const user = userEvent.setup()
    await renderPage()
    // Switch to User asc so the top row is r-a (alice).
    await user.click(screen.getByRole('button', { name: /^User$/ }))
    expect(rowUsers()[0]).toBe('alice')

    // Before click: no expanded detail panel for any row.
    expect(screen.queryByText(/Event ID/i)).toBeNull()

    // Click the body of the first data row (r-a) — toggles expansion.
    const firstRow = dataRows()[0]
    await user.click(firstRow)

    // ExpandedDetail renders the "Event ID" label inside the expanded row.
    expect(screen.getByText(/Event ID/i)).toBeInTheDocument()
    // And the expanded row carries the real event_id of the clicked row.
    expect(screen.getByText('e-a')).toBeInTheDocument()
  })
})

describe('AuditLogs — sort state resets on remount', () => {
  it('a fresh mount returns to Timestamp descending', async () => {
    const user = userEvent.setup()
    const { unmount } = await renderPage()
    // Move the active sort off Timestamp.
    await user.click(screen.getByRole('button', { name: /^User$/ }))
    expect(headerByLabel('User')).toHaveAttribute('aria-sort', 'ascending')
    unmount()

    // Remount the page — default sort must reset to Timestamp descending.
    await renderPage()
    expect(headerByLabel('Timestamp')).toHaveAttribute('aria-sort', 'descending')
    expect(headerByLabel('User')).toHaveAttribute('aria-sort', 'none')
    expect(rowResources()).toEqual(['r-d', 'r-b', 'r-c', 'r-a'])
  })
})

// Regression: the audit DDB table has a composite PK (event_id + timestamp),
// so the API legitimately returns rows that share an event_id across different
// timestamps. The row key must combine both so React reconciliation works after
// a sort reorders them. Without this, duplicate keys silently break the UI:
// rows appear not to reorder even though state updates correctly.
describe('AuditLogs — duplicate event_id across timestamps', () => {
  it('renders every row and reorders correctly when toggling sort', async () => {
    const user = userEvent.setup()
    state.logs = [
      { event_id: '1', timestamp: '2026-01-01T10:00:00Z', action_type: 'A', resource: 'r-1a', user: 'u1', status: 'OK', details: '{}' },
      { event_id: '1', timestamp: '2026-01-02T10:00:00Z', action_type: 'A', resource: 'r-1b', user: 'u2', status: 'OK', details: '{}' },
      { event_id: '1', timestamp: '2026-01-03T10:00:00Z', action_type: 'A', resource: 'r-1c', user: 'u3', status: 'OK', details: '{}' },
    ]
    await renderPage()
    expect(rowResources()).toEqual(['r-1c', 'r-1b', 'r-1a'])
    await user.click(screen.getByRole('button', { name: /^Timestamp$/ }))
    expect(rowResources()).toEqual(['r-1a', 'r-1b', 'r-1c'])
  })
})
