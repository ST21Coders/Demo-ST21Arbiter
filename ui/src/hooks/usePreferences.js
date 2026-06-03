// Per-browser user preferences store.
//
// Design (see Documents/settings_impl_plan.md, Phase 0.2):
//   • No React Context / Provider — a module-level store plus
//     useSyncExternalStore, matching the app's lightweight, no-Redux state
//     ethos. Any component can read/write and they all stay in sync.
//   • Persisted to localStorage under one versioned key. All reads merge
//     stored values over DEFAULTS so adding a field later never breaks an
//     old saved blob. A version bump runs migrate().
//   • Fully fault-tolerant: every storage access is wrapped in try/catch
//     (private-mode / disabled storage), falling back to an in-memory copy —
//     mirroring the guards in useAuth.js and PersonaContext.jsx. Preferences
//     must never crash the app.
//
// NOTE: this module deliberately imports NOTHING from PersonaContext, to avoid
// an import cycle. The landingPath access re-check lives in
// firstAccessiblePath() (which already has hasAccess), not here.

import { useSyncExternalStore } from 'react'

const STORAGE_KEY = 'arbiter.preferences'
const PREFS_VERSION = 1

// jsdom (test env) has no matchMedia; guard so DEFAULTS never throws.
function prefersReducedMotion() {
  try {
    return typeof window !== 'undefined' &&
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches
  } catch {
    return false
  }
}

function makeDefaults() {
  return {
    version: PREFS_VERSION,
    appearance: {
      landingPath: null,          // null = use firstAccessiblePath()
      density: 'comfortable',     // 'comfortable' | 'compact'
      reduceMotion: prefersReducedMotion(),
      theme: 'system',            // 'system' | 'light' | 'dark' (staged — persisted, not yet visual)
    },
    notifications: {
      paused: false,
      criticalFindings: true,
      crAwaitingMe: true,
      scanComplete: true,
      pipelineSync: false,
      announcements: true,
    },
  }
}

// Deep-ish merge of a stored blob over fresh defaults. One level of nesting is
// all the schema has, so an explicit two-level merge keeps it readable and
// guarantees every key exists with a sane type.
function mergeOverDefaults(stored) {
  const d = makeDefaults()
  if (!stored || typeof stored !== 'object') return d
  return {
    version: PREFS_VERSION,
    appearance: { ...d.appearance, ...(stored.appearance && typeof stored.appearance === 'object' ? stored.appearance : {}) },
    notifications: { ...d.notifications, ...(stored.notifications && typeof stored.notifications === 'object' ? stored.notifications : {}) },
  }
}

// Forward-migration hook. v1 is identity; future bumps add cases here.
function migrate(stored) {
  if (!stored || typeof stored !== 'object') return stored
  // switch (stored.version) { case 0: ...; }
  return stored
}

function readStorage() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return makeDefaults()
    const parsed = migrate(JSON.parse(raw))
    return mergeOverDefaults(parsed)
  } catch {
    return makeDefaults()
  }
}

// ── Module-level store ────────────────────────────────────────
// `state` is replaced wholesale on every write so useSyncExternalStore sees a
// new reference (and an unchanged one between writes — required, or it loops).
let state = readStorage()
const listeners = new Set()

function emit() {
  for (const l of listeners) l()
}

function persist() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state))
  } catch {
    /* storage unavailable — keep the in-memory copy */
  }
}

function subscribe(listener) {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

// ── Public API ────────────────────────────────────────────────

export function getPreferences() {
  return state
}

// Whether persistence actually works in this browser/session. Drives the
// "won't persist this session" notice in the Appearance section.
export function storageAvailable() {
  try {
    const k = '__arbiter_prefs_probe__'
    localStorage.setItem(k, '1')
    localStorage.removeItem(k)
    return true
  } catch {
    return false
  }
}

// Set one preference by dotted path, e.g. setPreference('appearance.density', 'compact').
// Clones the affected branch so the snapshot reference changes.
export function setPreference(path, value) {
  const [section, key] = path.split('.')
  if (!section || !key || !(section in state)) return
  state = { ...state, [section]: { ...state[section], [key]: value } }
  persist()
  emit()
}

export function resetPreferences() {
  state = makeDefaults()
  persist()
  emit()
}

// Test-only: force a re-read from storage (e.g. after a test seeds localStorage
// directly). Not used by the app.
export function __reloadPreferences() {
  state = readStorage()
  emit()
}

// Resolve the stored theme preference ('system' | 'light' | 'dark') to a
// concrete 'light' | 'dark' given the OS preference. Pure — unit-tested.
export function resolveTheme(theme, systemPrefersDark) {
  if (theme === 'dark' || theme === 'light') return theme
  return systemPrefersDark ? 'dark' : 'light'
}

export function usePreferences() {
  return useSyncExternalStore(subscribe, getPreferences, getPreferences)
}

// Convenience selector for a single dotted path.
export function usePreference(path) {
  const prefs = usePreferences()
  const [section, key] = path.split('.')
  return prefs?.[section]?.[key]
}
