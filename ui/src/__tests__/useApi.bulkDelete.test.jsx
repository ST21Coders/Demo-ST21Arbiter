import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'

// Live-mode tests run against a mocked config module with USE_MOCK=false so
// bulkDeleteSessions takes the apiFetch -> fetch branch. The mock-mode test
// re-imports useApi with USE_MOCK=true to exercise the in-memory splice path.
// authHeaders() is stubbed so request headers are deterministic.

vi.mock('../config', () => ({
  API_URL:  'https://api.example.com',
  CHAT_URL: 'https://chat.example.com/',
  USE_MOCK: false,
}))
vi.mock('../hooks/useAuth', () => ({
  authHeaders: () => ({ Authorization: 'Bearer test-token' }),
  refresh:     vi.fn(),
  signIn:      vi.fn(),
}))

let useConversations
beforeEach(async () => {
  vi.resetModules()
  vi.doMock('../config', () => ({
    API_URL:  'https://api.example.com',
    CHAT_URL: 'https://chat.example.com/',
    USE_MOCK: false,
  }))
  vi.doMock('../hooks/useAuth', () => ({
    authHeaders: () => ({ Authorization: 'Bearer test-token' }),
    refresh:     vi.fn(),
    signIn:      vi.fn(),
  }))
  ;({ useConversations } = await import('../hooks/useApi'))

  // Default success response; individual tests override via mockResolvedValueOnce.
  globalThis.fetch = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => ({
      deleted: ['harness-abc', 'harness-def'],
      failed:  [],
    }),
  })
})

afterEach(() => {
  vi.restoreAllMocks()
})

function parseFetchBody(call) {
  return JSON.parse(call[1].body)
}

describe('useConversations.bulkDeleteSessions — live mode (USE_MOCK=false)', () => {
  it('POSTs to /conversations/bulk-delete with {session_ids: [...]} JSON body', async () => {
    const { result } = renderHook(() => useConversations())
    const ids = ['harness-abc', 'harness-def']
    await act(async () => { await result.current.bulkDeleteSessions(ids) })

    expect(globalThis.fetch).toHaveBeenCalledTimes(1)
    const [url, init] = globalThis.fetch.mock.calls[0]
    expect(url).toBe('https://api.example.com/conversations/bulk-delete')
    expect(init.method).toBe('POST')
    expect(init.headers).toMatchObject({
      'Content-Type': 'application/json',
      Authorization: 'Bearer test-token',
    })
    const body = parseFetchBody(globalThis.fetch.mock.calls[0])
    expect(body).toEqual({ session_ids: ids })
  })

  it('returns the parsed {deleted, failed} response shape', async () => {
    globalThis.fetch = vi.fn().mockResolvedValueOnce({
      ok: true, status: 200, statusText: 'OK',
      json: async () => ({
        deleted: ['s1'],
        failed:  [{ session_id: 's2', reason: 'not_found' }],
      }),
    })
    const { result } = renderHook(() => useConversations())
    let res
    await act(async () => { res = await result.current.bulkDeleteSessions(['s1', 's2']) })
    expect(res).toEqual({
      deleted: ['s1'],
      failed:  [{ session_id: 's2', reason: 'not_found' }],
    })
  })

  it('throws on a non-2xx response (mirrors deleteSession error behavior)', async () => {
    globalThis.fetch = vi.fn().mockResolvedValueOnce({
      ok: false, status: 400, statusText: 'Bad Request',
      json: async () => ({ error: 'too many ids' }),
    })
    const { result } = renderHook(() => useConversations())
    await expect(
      act(async () => { await result.current.bulkDeleteSessions(Array(101).fill('x')) })
    ).rejects.toThrow(/400 Bad Request/)
  })

  it('does NOT optimistically prune sessions before the response (caller calls list() after)', async () => {
    // Seed sessions via the mocked list() endpoint, then verify that calling
    // bulkDeleteSessions leaves the local sessions array untouched — the
    // reconciliation happens via a follow-up list() in the parent component.
    globalThis.fetch = vi.fn()
      // first call: list()
      .mockResolvedValueOnce({
        ok: true, status: 200, statusText: 'OK',
        json: async () => ({ sessions: [
          { session_id: 'a' }, { session_id: 'b' }, { session_id: 'c' },
        ]}),
      })
      // second call: bulkDeleteSessions
      .mockResolvedValueOnce({
        ok: true, status: 200, statusText: 'OK',
        json: async () => ({ deleted: ['a', 'b'], failed: [] }),
      })

    const { result } = renderHook(() => useConversations())
    await act(async () => { await result.current.list() })
    expect(result.current.sessions).toHaveLength(3)

    await act(async () => { await result.current.bulkDeleteSessions(['a', 'b']) })
    // Sidebar unchanged — caller is responsible for the follow-up list().
    expect(result.current.sessions).toHaveLength(3)
  })
})

describe('useConversations.bulkDeleteSessions — mock mode (USE_MOCK=true)', () => {
  let useConversationsMock
  beforeEach(async () => {
    vi.resetModules()
    vi.doMock('../config', () => ({
      API_URL:  '',
      CHAT_URL: '',
      USE_MOCK: true,
    }))
    vi.doMock('../hooks/useAuth', () => ({
      authHeaders: () => ({}),
      refresh:     vi.fn(),
      signIn:      vi.fn(),
    }))
    ;({ useConversations: useConversationsMock } = await import('../hooks/useApi'))
    globalThis.fetch = vi.fn() // assert it's never called
  })

  it('splices the ids out of the in-memory MOCK_SESSIONS and returns {deleted: ids, failed: []}', async () => {
    const { result } = renderHook(() => useConversationsMock())

    // Prime sessions from the in-memory mock array.
    await act(async () => { await result.current.list() })
    const beforeIds = result.current.sessions.map(s => s.session_id)
    expect(beforeIds).toContain('mock-sess-1')
    expect(beforeIds).toContain('mock-sess-2')

    let res
    await act(async () => {
      res = await result.current.bulkDeleteSessions(['mock-sess-1', 'mock-sess-2'])
    })
    expect(res).toEqual({
      deleted: ['mock-sess-1', 'mock-sess-2'],
      failed:  [],
    })
    expect(globalThis.fetch).not.toHaveBeenCalled()

    // The next list() call should reflect the splice — the ids are gone.
    await act(async () => { await result.current.list() })
    const afterIds = result.current.sessions.map(s => s.session_id)
    expect(afterIds).not.toContain('mock-sess-1')
    expect(afterIds).not.toContain('mock-sess-2')
  })

  it('tolerates ids that are not in MOCK_SESSIONS (no-op for those, still reports deleted)', async () => {
    const { result } = renderHook(() => useConversationsMock())
    let res
    await act(async () => {
      res = await result.current.bulkDeleteSessions(['does-not-exist-1', 'does-not-exist-2'])
    })
    // Mock branch is intentionally permissive — it just reports the request
    // ids as deleted without server-side reconciliation.
    expect(res).toEqual({
      deleted: ['does-not-exist-1', 'does-not-exist-2'],
      failed:  [],
    })
    expect(globalThis.fetch).not.toHaveBeenCalled()
  })

  it('handles an empty id list without throwing', async () => {
    const { result } = renderHook(() => useConversationsMock())
    let res
    await act(async () => { res = await result.current.bulkDeleteSessions([]) })
    expect(res).toEqual({ deleted: [], failed: [] })
    expect(globalThis.fetch).not.toHaveBeenCalled()
  })
})
