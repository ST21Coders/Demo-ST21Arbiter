import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

// Smart Rabbit routes chats by sending the selected catalog agent's id as
// `target` with chat_type 'rabbit'. These tests pin the sendChat wire format
// (mirrors useApi.bulkDelete.test.jsx's live-mode convention).

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

let sendChat
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
  ;({ sendChat } = await import('../hooks/useApi'))

  globalThis.fetch = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => ({ reply: 'ok', session_id: 'sess-123' }),
  })
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('Smart Rabbit chat routing (sendChat wire format)', () => {
  it("POSTs to the Function URL with target + chat_type 'rabbit'", async () => {
    await sendChat({
      prompt: 'List all CIs of the Web Server class',
      session_id: 'sess-abc',
      chat_type: 'rabbit',
      target: 'servicenow',
    })
    expect(globalThis.fetch).toHaveBeenCalledTimes(1)
    const [url, opts] = globalThis.fetch.mock.calls[0]
    expect(url).toBe('https://chat.example.com/chat')
    const body = JSON.parse(opts.body)
    expect(body.target).toBe('servicenow')
    expect(body.chat_type).toBe('rabbit')
    expect(body.prompt).toContain('Web Server')
  })

  it('routes to a new-catalog agent (claim) the same way', async () => {
    await sendChat({
      prompt: 'What documents does an auto claim need?',
      session_id: 'sess-def',
      chat_type: 'rabbit',
      target: 'claim',
    })
    const body = JSON.parse(globalThis.fetch.mock.calls[0][1].body)
    expect(body.target).toBe('claim')
    expect(body.chat_type).toBe('rabbit')
  })
})
