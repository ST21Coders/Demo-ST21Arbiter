import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import ClearChatsButton, {
  HARNESS_PREFIXES,
  isHarnessId,
  isOlderThan30Days,
  computeScopes,
} from '../components/ClearChatsButton'

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

describe('isHarnessId', () => {
  it('matches every documented harness prefix', () => {
    expect(isHarnessId('harness-abc')).toBe(true)
    expect(isHarnessId('features-bar')).toBe(true)
    expect(isHarnessId('logic-race-baz')).toBe(true)
  })

  it('returns false for analyst / mcp / empty / non-string ids', () => {
    expect(isHarnessId('analyst-x')).toBe(false)
    expect(isHarnessId('mcp-1')).toBe(false)
    expect(isHarnessId('')).toBe(false)
    expect(isHarnessId(undefined)).toBe(false)
    expect(isHarnessId(null)).toBe(false)
    expect(isHarnessId(123)).toBe(false)
  })

  it('exports the prefix list in the documented order', () => {
    expect(HARNESS_PREFIXES).toEqual(['harness-', 'features-', 'logic-race-'])
  })
})

describe('isOlderThan30Days', () => {
  const now = Date.parse('2026-06-17T12:00:00Z')

  it('returns true when created_at is 31 days before now', () => {
    const ts = new Date(now - 31 * 24 * 60 * 60 * 1000).toISOString()
    expect(isOlderThan30Days({ created_at: ts }, now)).toBe(true)
  })

  it('returns false when created_at is 29 days before now', () => {
    const ts = new Date(now - 29 * 24 * 60 * 60 * 1000).toISOString()
    expect(isOlderThan30Days({ created_at: ts }, now)).toBe(false)
  })

  it('returns false when created_at is missing', () => {
    expect(isOlderThan30Days({}, now)).toBe(false)
    expect(isOlderThan30Days({ created_at: null }, now)).toBe(false)
    expect(isOlderThan30Days({ created_at: '' }, now)).toBe(false)
  })

  it('returns false when created_at is unparseable', () => {
    expect(isOlderThan30Days({ created_at: 'not-a-date' }, now)).toBe(false)
  })

  it('returns false on a null/undefined session', () => {
    expect(isOlderThan30Days(null, now)).toBe(false)
    expect(isOlderThan30Days(undefined, now)).toBe(false)
  })
})

describe('computeScopes', () => {
  const now = Date.parse('2026-06-17T12:00:00Z')
  const oldIso = new Date(now - 45 * 24 * 60 * 60 * 1000).toISOString()
  const freshIso = new Date(now - 5 * 24 * 60 * 60 * 1000).toISOString()

  it('partitions a mixed array into all/harness/old', () => {
    const sessions = [
      { session_id: 'analyst-1', created_at: freshIso },
      { session_id: 'harness-2', created_at: freshIso },
      { session_id: 'features-3', created_at: oldIso },
      { session_id: 'logic-race-4', created_at: freshIso },
      { session_id: 'analyst-5', created_at: oldIso },
      { session_id: 'analyst-6' /* missing created_at — excluded from old */ },
    ]
    const { all, harness, old } = computeScopes(sessions, now)
    expect(all).toHaveLength(6)
    expect(harness.map(s => s.session_id)).toEqual(['harness-2', 'features-3', 'logic-race-4'])
    expect(old.map(s => s.session_id)).toEqual(['features-3', 'analyst-5'])
  })

  it('tolerates an empty array', () => {
    const { all, harness, old } = computeScopes([], now)
    expect(all).toEqual([])
    expect(harness).toEqual([])
    expect(old).toEqual([])
  })

  it('tolerates non-array input', () => {
    const { all, harness, old } = computeScopes(null, now)
    expect(all).toEqual([])
    expect(harness).toEqual([])
    expect(old).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

const NOW = 1750000000000
const OLD = new Date(NOW - 45 * 24 * 60 * 60 * 1000).toISOString()
const FRESH = new Date(NOW - 2 * 24 * 60 * 60 * 1000).toISOString()

beforeEach(() => {
  vi.spyOn(Date, 'now').mockReturnValue(NOW)
})

afterEach(() => {
  vi.restoreAllMocks()
})

function mixedSessions() {
  return [
    { session_id: 'analyst-1', created_at: FRESH },
    { session_id: 'harness-2', created_at: FRESH },
    { session_id: 'features-3', created_at: OLD },
    { session_id: 'analyst-4', created_at: OLD },
  ]
}

describe('ClearChatsButton — render', () => {
  it('disables the main button when sessions is empty', () => {
    render(<ClearChatsButton sessions={[]} onBulkDelete={vi.fn()} onAfter={vi.fn()} />)
    const btn = screen.getByRole('button', { name: /Clear/i })
    expect(btn).toBeDisabled()
  })

  it('enables the main button when there is at least one session', () => {
    render(<ClearChatsButton sessions={mixedSessions()} onBulkDelete={vi.fn()} onAfter={vi.fn()} />)
    const btn = screen.getByRole('button', { name: /Clear/i })
    expect(btn).not.toBeDisabled()
  })
})

describe('ClearChatsButton — dropdown', () => {
  it('opens on click and shows three scope items with correct counts', () => {
    render(<ClearChatsButton sessions={mixedSessions()} onBulkDelete={vi.fn()} onAfter={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: /Clear/i }))

    expect(screen.getByRole('button', { name: /All chats \(4\)/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Harness chats only \(2\)/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Older than 30 days \(2\)/ })).toBeInTheDocument()
  })

  it('disables a scope item whose count is zero', () => {
    // Only fresh analyst rows → harness=0, old=0
    const sessions = [
      { session_id: 'analyst-1', created_at: FRESH },
      { session_id: 'analyst-2', created_at: FRESH },
    ]
    render(<ClearChatsButton sessions={sessions} onBulkDelete={vi.fn()} onAfter={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: /Clear/i }))

    const all = screen.getByRole('button', { name: /All chats \(2\)/ })
    const harness = screen.getByRole('button', { name: /Harness chats only \(0\)/ })
    const old = screen.getByRole('button', { name: /Older than 30 days \(0\)/ })

    expect(all).not.toBeDisabled()
    expect(harness).toBeDisabled()
    expect(harness).toHaveAttribute('aria-disabled', 'true')
    expect(old).toBeDisabled()
  })

  it('closes the dropdown on Escape', () => {
    render(<ClearChatsButton sessions={mixedSessions()} onBulkDelete={vi.fn()} onAfter={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: /Clear/i }))
    expect(screen.getByTestId('clear-chats-menu')).toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByTestId('clear-chats-menu')).not.toBeInTheDocument()
  })
})

describe('ClearChatsButton — confirm + dispatch', () => {
  it('window.confirm includes the count and scope name', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const onBulkDelete = vi.fn()
    render(<ClearChatsButton sessions={mixedSessions()} onBulkDelete={onBulkDelete} onAfter={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: /Clear/i }))
    fireEvent.click(screen.getByRole('button', { name: /Harness chats only \(2\)/ }))

    expect(confirmSpy).toHaveBeenCalledTimes(1)
    const msg = confirmSpy.mock.calls[0][0]
    expect(msg).toMatch(/2/)
    expect(msg).toMatch(/harness/i)
    // Cancel path → no call.
    expect(onBulkDelete).not.toHaveBeenCalled()
  })

  it('on confirm, calls onBulkDelete with the ids for the chosen scope', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const onBulkDelete = vi.fn().mockResolvedValue({ deleted: ['harness-2', 'features-3'], failed: [] })
    const onAfter = vi.fn()
    render(<ClearChatsButton sessions={mixedSessions()} onBulkDelete={onBulkDelete} onAfter={onAfter} />)

    fireEvent.click(screen.getByRole('button', { name: /Clear/i }))
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Harness chats only \(2\)/ }))
    })

    expect(onBulkDelete).toHaveBeenCalledTimes(1)
    expect(onBulkDelete.mock.calls[0][0]).toEqual(['harness-2', 'features-3'])
  })

  it('calls onAfter exactly once on a happy response (failed: [])', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const onBulkDelete = vi.fn().mockResolvedValue({
      deleted: ['analyst-1', 'harness-2', 'features-3', 'analyst-4'],
      failed: [],
    })
    const onAfter = vi.fn()
    render(<ClearChatsButton sessions={mixedSessions()} onBulkDelete={onBulkDelete} onAfter={onAfter} />)

    fireEvent.click(screen.getByRole('button', { name: /Clear/i }))
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /All chats \(4\)/ }))
    })

    expect(onAfter).toHaveBeenCalledTimes(1)
    // No toast on a clean response.
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('renders a partial-failure toast when failed is non-empty', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const onBulkDelete = vi.fn().mockResolvedValue({
      deleted: ['harness-2'],
      failed: [{ session_id: 'features-3', reason: 'not_found' }],
    })
    render(<ClearChatsButton sessions={mixedSessions()} onBulkDelete={onBulkDelete} onAfter={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: /Clear/i }))
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Harness chats only \(2\)/ }))
    })

    const toast = await screen.findByRole('status')
    expect(toast.textContent).toMatch(/Deleted 1 of 2/)
    expect(toast.textContent).toMatch(/1 could not be deleted/)
  })

  it('calls onActiveDeleted when the active session is in the deleted set', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const onBulkDelete = vi.fn().mockResolvedValue({
      deleted: ['analyst-1', 'harness-2', 'features-3', 'analyst-4'],
      failed: [],
    })
    const onActiveDeleted = vi.fn()
    render(
      <ClearChatsButton
        sessions={mixedSessions()}
        onBulkDelete={onBulkDelete}
        onAfter={vi.fn()}
        activeSessionId="harness-2"
        onActiveDeleted={onActiveDeleted}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: /Clear/i }))
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /All chats \(4\)/ }))
    })

    expect(onActiveDeleted).toHaveBeenCalledTimes(1)
  })

  it('does NOT call onActiveDeleted when the active session is not in the deleted set', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const onBulkDelete = vi.fn().mockResolvedValue({
      deleted: ['harness-2', 'features-3'],
      failed: [],
    })
    const onActiveDeleted = vi.fn()
    render(
      <ClearChatsButton
        sessions={mixedSessions()}
        onBulkDelete={onBulkDelete}
        onAfter={vi.fn()}
        activeSessionId="analyst-1"
        onActiveDeleted={onActiveDeleted}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: /Clear/i }))
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Harness chats only \(2\)/ }))
    })

    expect(onActiveDeleted).not.toHaveBeenCalled()
  })

  it('on a thrown onBulkDelete, alerts the user and still re-enables the button', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {})
    const onBulkDelete = vi.fn().mockRejectedValue(new Error('boom'))
    const onAfter = vi.fn()
    render(<ClearChatsButton sessions={mixedSessions()} onBulkDelete={onBulkDelete} onAfter={onAfter} />)

    fireEvent.click(screen.getByRole('button', { name: /Clear/i }))
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /All chats \(4\)/ }))
    })

    expect(alertSpy).toHaveBeenCalledTimes(1)
    // Spec-locked alert wording: `Bulk delete failed: <detail>` where <detail>
    // is the underlying error message (here "boom").
    expect(alertSpy.mock.calls[0][0]).toBe('Bulk delete failed: boom')
    expect(onAfter).not.toHaveBeenCalled()
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Clear/i })).not.toBeDisabled(),
    )
  })
})
