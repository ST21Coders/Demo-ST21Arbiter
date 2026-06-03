import '@testing-library/jest-dom'

// jsdom in this config ships only a stub `localStorage` ({} with no methods),
// so any code touching Web Storage (e.g. hooks/usePreferences.js) needs a real
// implementation under test. Provide a minimal Map-backed Storage on both the
// global and window scopes. Harmless for tests that don't use it.
class MemoryStorage {
  constructor() { this.store = new Map() }
  get length() { return this.store.size }
  key(i) { return Array.from(this.store.keys())[i] ?? null }
  getItem(k) { return this.store.has(k) ? this.store.get(k) : null }
  setItem(k, v) { this.store.set(String(k), String(v)) }
  removeItem(k) { this.store.delete(k) }
  clear() { this.store.clear() }
}

const localStorageMock = new MemoryStorage()
Object.defineProperty(globalThis, 'localStorage', { value: localStorageMock, configurable: true })
if (typeof window !== 'undefined') {
  Object.defineProperty(window, 'localStorage', { value: localStorageMock, configurable: true })
}
