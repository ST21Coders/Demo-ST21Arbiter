import { describe, it, expect } from 'vitest'
import { resolveTheme } from '../hooks/usePreferences'

describe('resolveTheme', () => {
  it('returns an explicit light/dark choice unchanged', () => {
    expect(resolveTheme('light', true)).toBe('light')
    expect(resolveTheme('light', false)).toBe('light')
    expect(resolveTheme('dark', false)).toBe('dark')
    expect(resolveTheme('dark', true)).toBe('dark')
  })

  it('follows the OS preference when set to system', () => {
    expect(resolveTheme('system', true)).toBe('dark')
    expect(resolveTheme('system', false)).toBe('light')
  })

  it('defaults to light for unknown/undefined values without system dark', () => {
    expect(resolveTheme(undefined, false)).toBe('light')
    expect(resolveTheme('bogus', false)).toBe('light')
    expect(resolveTheme(undefined, true)).toBe('dark')
  })
})
