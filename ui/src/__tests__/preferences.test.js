import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  getPreferences,
  setPreference,
  resetPreferences,
  storageAvailable,
  __reloadPreferences,
} from '../hooks/usePreferences'

const KEY = 'arbiter.preferences'

// The store reads localStorage once at module load, so each test seeds storage
// then forces a re-read via __reloadPreferences() to get a clean slate.
beforeEach(() => {
  localStorage.clear()
  vi.restoreAllMocks()
  __reloadPreferences()
})

afterEach(() => {
  localStorage.clear()
})

describe('usePreferences store', () => {
  it('returns defaults when no key is stored', () => {
    const p = getPreferences()
    expect(p.version).toBe(1)
    expect(p.appearance.landingPath).toBeNull()
    expect(p.appearance.density).toBe('comfortable')
    expect(p.appearance.theme).toBe('system')
    expect(p.notifications.criticalFindings).toBe(true)
    expect(p.notifications.pipelineSync).toBe(false)
  })

  it('merges a partial stored blob over defaults', () => {
    localStorage.setItem(KEY, JSON.stringify({
      version: 1,
      appearance: { density: 'compact' }, // only one field present
    }))
    __reloadPreferences()
    const p = getPreferences()
    expect(p.appearance.density).toBe('compact')        // from storage
    expect(p.appearance.theme).toBe('system')           // back-filled default
    expect(p.notifications.announcements).toBe(true)     // whole section defaulted
  })

  it('falls back to defaults on a malformed blob', () => {
    localStorage.setItem(KEY, '{not valid json')
    __reloadPreferences()
    expect(getPreferences().appearance.density).toBe('comfortable')
  })

  it('setPreference updates state and persists to localStorage', () => {
    setPreference('appearance.density', 'compact')
    expect(getPreferences().appearance.density).toBe('compact')
    const persisted = JSON.parse(localStorage.getItem(KEY))
    expect(persisted.appearance.density).toBe('compact')
  })

  it('setPreference returns a new top-level reference (snapshot stability)', () => {
    const before = getPreferences()
    setPreference('appearance.theme', 'dark')
    const after = getPreferences()
    expect(after).not.toBe(before)              // changed → new ref
    expect(getPreferences()).toBe(after)        // unchanged between writes → same ref
  })

  it('setPreference ignores unknown sections/paths', () => {
    const before = getPreferences()
    setPreference('bogus.key', 'x')
    setPreference('appearance', 'x') // no key part
    expect(getPreferences()).toBe(before)
  })

  it('resetPreferences restores defaults', () => {
    setPreference('appearance.density', 'compact')
    setPreference('notifications.paused', true)
    resetPreferences()
    const p = getPreferences()
    expect(p.appearance.density).toBe('comfortable')
    expect(p.notifications.paused).toBe(false)
  })

  it('does not throw when localStorage.setItem throws (private mode)', () => {
    const spy = vi.spyOn(localStorage, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError')
    })
    expect(() => setPreference('appearance.theme', 'light')).not.toThrow()
    // in-memory state still updates even though persistence failed
    expect(getPreferences().appearance.theme).toBe('light')
    spy.mockRestore()
  })

  it('storageAvailable reports true in jsdom', () => {
    expect(storageAvailable()).toBe(true)
  })
})
