# Settings Page — Implementation Plan

Companion to [settings_spec.md](settings_spec.md). This is the build order, the exact files touched, and what each change contains. The spec is approved with one locked-in constraint:

> **Roles are read-only and immutable in-session.** Settings displays the persona but never edits it. The only way to act as a different role is **Sign out → sign in as a different user**. No role picker, no dropdown, nothing that mutates the persona anywhere on this page.

Decisions taken from the spec's open questions (defaults chosen): landing-page override lives **inside `firstAccessiblePath()`**; density v1 targets the **dense surfaces only**; the Advanced gate **reuses `can('viewLLMControl')`**; the theme control **ships labeled "Preview."**

---

## Phase 0 — Foundations (no UI yet)

These land first because everything else imports them. Mergeable on their own.

### 0.1 `APP_VERSION` constant — [ui/src/config.js](../ui/src/config.js)
- Add `export const APP_VERSION = '1.0.0-poc'`.
- Update [Sidebar.jsx:219](../ui/src/components/Sidebar.jsx#L219) footer (`v1.0.0-poc · us-east-1`) to read `APP_VERSION` instead of the literal. Single source of truth for the Environment section.

### 0.2 Preferences store — new file `ui/src/hooks/usePreferences.js`
The whole persistence layer. No Provider — a module-level store + `useSyncExternalStore`, matching the app's lightweight, no-Redux state ethos.

```
Module shape:
  const STORAGE_KEY = 'arbiter.preferences'
  const PREFS_VERSION = 1
  const DEFAULTS = { version, appearance:{...}, notifications:{...} }   // per spec

  let state = load()            // try localStorage → merge over DEFAULTS → validate → migrate; catch → in-memory DEFAULTS
  const listeners = new Set()
  function emit(){ listeners.forEach(l => l()) }
  function persist(){ try { localStorage.setItem(KEY, JSON.stringify(state)) } catch {} }   // mirrors useAuth.js/PersonaContext try/catch

  export function getPreferences()                       // snapshot (stable ref between writes — required by useSyncExternalStore)
  export function setPreference(path, value)             // e.g. ('appearance.density','compact'); clones, sets, persist(), emit()
  export function resetPreferences()                     // state = clone(DEFAULTS); persist(); emit()
  export function storageAvailable()                     // boolean, for the "won't persist" note

  export function usePreferences()                       // useSyncExternalStore(subscribe, getPreferences)
  export function usePreference(path)                    // convenience selector
```

Rules baked in:
- **Merge-over-defaults** on read so future fields never break an old blob.
- **Validate `landingPath`** is deferred to the consumer that has `hasAccess` (the store can't import PersonaContext without a cycle) — store keeps it as a plain string|null; `firstAccessiblePath()` does the access re-check (see 3.1).
- **`migrate(old)`** switch on `old.version`; v1 is identity.
- **Stable snapshots**: `getPreferences()` must return the same reference until a write happens, or `useSyncExternalStore` will loop. Replace `state` wholesale on each write.

### 0.3 Shared `Toggle` — new file `ui/src/components/Toggle.jsx`
- Lift the `Toggle` currently inlined in [LLMControl.jsx:30-43](../ui/src/pages/LLMControl.jsx#L30-L43) verbatim into its own file; export default.
- Update LLMControl to import it and delete the local copy (no behavior change — pure extraction).
- Add a sibling `SegmentedControl.jsx` (button group, `value`/`options`/`onChange`/`disabled`) and a tiny `SettingRow.jsx` (label + description on the left, control slot on the right) for consistent rows. Both styled to the slate/`rounded-lg` language.

**Phase 0 tests:** `ui/src/__tests__/preferences.test.js` — defaults when key absent; partial-blob merge; reset; `localStorage.setItem` throwing doesn't crash; migration runs. (Mirrors the existing `persona.test.js` style.)

---

## Phase 1 — Route & navigation wiring (page can be blank)

Goal: clicking Settings reaches a real page, breadcrumb/active-state correct, 404 gone. Verifiable before any section content exists.

### 1.1 [ui/src/App.jsx](../ui/src/App.jsx)
- `import Settings from './pages/Settings'`.
- Inside `Shell`'s `<Routes>`, **before** `<Route path="*" .../>`, add — **not** wrapped in `<Guarded>` (universal, like `/personas`):
  ```jsx
  <Route path="/settings" element={<Settings />} />
  ```

### 1.2 [ui/src/components/TopBar.jsx](../ui/src/components/TopBar.jsx)
- Add to `ROUTE_META`: `'/settings': { title: 'Settings', section: null },` so the breadcrumb renders "Settings" instead of the "ARBITER" fallback.

### 1.3 [ui/src/components/Sidebar.jsx](../ui/src/components/Sidebar.jsx#L205-L214)
- The footer link already targets `/settings`. Convert its `className` to the **function form** used by every other `NavLink` so it gets the active treatment (indigo text + `bg-indigo-50` + left border), matching the main-nav pattern at [Sidebar.jsx:147-165](../ui/src/components/Sidebar.jsx#L147-L165). No new link, no route-gating (footer link shows for everyone — correct, since `/settings` is universal).
- Optionally add `'/settings': 'Settings'` to the exported `PAGE_TITLES` map for consistency (cosmetic).

### 1.4 Stub page — new file `ui/src/pages/Settings.jsx`
- Minimal default export rendering the page header (`p-6`, title "Settings", subtitle) so Phase 1 is independently verifiable. Real content arrives in Phase 2.

**Phase 1 check:** `cd ui && npm run dev`, sign in, click Settings → page renders, breadcrumb says Settings, sidebar link highlights, unknown routes still 404.

---

## Phase 2 — Page shell & sections

Build out [Settings.jsx](../ui/src/pages/Settings.jsx) and section components. Split sections into `ui/src/components/settings/*.jsx` (one file per section) to keep `Settings.jsx` a thin layout host.

### 2.0 Layout host — `Settings.jsx`
- Two-column: sticky left **section rail** + right content panel. Responsive: rail collapses to a horizontal scroller on `< lg`.
- Active section in `useState('account')`, synced to `location.hash` (`/settings#appearance`). On mount, read the hash; if it points to a section hidden for this persona (e.g. non-`it` → `#advanced`), fall back to `account`.
- Build the section list dynamically, filtering Advanced by `can('viewLLMControl')`.
- Styling per [LLMControl.jsx](../ui/src/pages/LLMControl.jsx): `cardStyle`, `rounded-xl p-4`, `text-[10px] uppercase tracking-wider` labels, lucide icons. Outer `max-w-6xl`.

### 2.1 `AccountSection.jsx` — read-only ⚠️ no role mutation
- Pull from `usePersona()` (`persona`, `email`) + `getGroups()`.
- Avatar (`persona.initials` + `persona.gradient`), name, title, role + `persona.badge` chip (tinted `persona.color`), email, Cognito group chips, description, accessible-pages list (derive from `persona.access` × `ROUTE_ACCESS`).
- **Locked-in note** (verbatim intent): *"Your role and access are assigned by your administrator via Cognito groups and cannot be changed here. To use a different role, sign out and sign in as another user."* Link to [Personas](/personas).
- **No** select, dropdown, or button that writes the persona. The only action present is a link to Personas (read-only view).
- `persona === null` → "Unassigned" state mirroring `PersonaBadge`'s degraded branch ([Sidebar.jsx:65-78](../ui/src/components/Sidebar.jsx#L65-L78)) and the `AccessDenied` copy in App.jsx.
- `DEV_AUTH` → yellow "Local dev session" banner (TopBar dev styling).

### 2.2 `AppearanceSection.jsx`
- **Default landing page** — `SegmentedControl`/`<select>` of accessible routes (`persona.access` × `ROUTE_ACCESS` + `/personas`), value = `appearance.landingPath ?? '(auto)'`. Writes via `setPreference('appearance.landingPath', v)` (store `null` for auto).
- **Density** — SegmentedControl `comfortable | compact` → `setPreference('appearance.density', v)`.
- **Reduce motion** — `Toggle` → `appearance.reduceMotion`.
- **Theme** — SegmentedControl `system | light | dark`, with a "Preview" tag + tooltip that it's saved but not yet visual. → `appearance.theme`.
- If `!storageAvailable()`, render the muted "won't persist this session" line.

### 2.3 `NotificationsSection.jsx`
- Master "Pause all" `Toggle` → `notifications.paused` (disables the rest visually when on).
- Per-category toggles, each **rendered only if relevant to persona** (gate rows by `can()` / `persona.access` per the spec table). `employee` sees only "announcements."
- Footer note: in-app only in POC; email/Teams is a future hook.

### 2.4 `SessionSection.jsx`
- "Signed in as" (email + role).
- **Token expiry**: read `arbiter.tokens.expires_at` (add a tiny `getSessionExpiry()` helper to [useAuth.js](../ui/src/hooks/useAuth.js) rather than reaching into `sessionStorage` from the page) → live countdown via a 1s interval (respect `reduceMotion` by updating text only). `DEV_AUTH` → "Local dev session — no token expiry."
- **Sign out** — primary button → `signOut()`.
- **Switch-user note** (locked-in): *"To use a different role, sign out and sign in as another user — roles can't be changed in-session."*
- **Sign out everywhere** — disabled, "coming soon" tooltip.

### 2.5 `EnvironmentSection.jsx`
- Mock/Live badge (`USE_MOCK`), `APP_VERSION`, `COGNITO.region` — all personas.
- API/Chat endpoints (`API_URL`/`CHAT_URL`) **only if `can('viewLLMControl')`**; others see "Connected" / "Not connected (mock)".
- `it` only: "Copy diagnostics" → clipboard JSON `{mode, version, region, endpoints, persona, buildTime}` — **no tokens, no secrets**.

### 2.6 `AdvancedSection.jsx` — `it` only (hidden otherwise, never "Access restricted")
- Quick links: [LLM Control](/llm-control), [Data Pipeline](/pipeline), [MCP Admin](/mcp-chat).
- **Clear local app data** — two-step inline confirm (no native `confirm()`); calls `resetPreferences()` + removes any other UI cache keys. **Does not touch `arbiter.tokens`** (that's Sign out's job).
- `DEV_AUTH` → note pointing at the TopBar dev-persona switcher (don't duplicate it).

---

## Phase 3 — Make preferences take effect

Wire the three real-effect preferences into the rest of the app.

### 3.1 Landing-page override — [PersonaContext.jsx](../ui/src/contexts/PersonaContext.jsx#L171)
- In `firstAccessiblePath()`: read `getPreferences().appearance.landingPath`; if set **and** `hasAccess(landingPath)`, return it; else fall through to the existing loop. If it was set but inaccessible, `setPreference('appearance.landingPath', null)` to self-heal the stale value.
- This automatically covers all consumers: TopBar Home shortcut, `/callback` redirect, and `PersonaRouteSync` in [App.jsx](../ui/src/App.jsx).
- Add the one-line comment near [PersonaContext.jsx:85](../ui/src/contexts/PersonaContext.jsx#L85) documenting that `/settings`, like `/personas`, is intentionally absent from `ROUTE_ACCESS` (universal).

### 3.2 Density + reduce-motion attributes — root element
- In `Shell` ([App.jsx](../ui/src/App.jsx)) read `usePreferences()` and set `data-density` / `data-reduce-motion` on the outer `div` (or on `document.documentElement` via an effect).
- Add the keying CSS in [ui/src/index.css](../ui/src/index.css): `[data-density="compact"]` tightens row padding on the dense surfaces (Findings table, Audit Logs) only for v1; `[data-reduce-motion="true"] * { transition:none!important; animation:none!important; }` (scoped sensibly).

### 3.3 Theme — shipped (follow-up after original v1)
- Functional dark mode. Tailwind's `slate` scale is exposed as channel-triplet CSS variables ([tailwind.config.js](../ui/tailwind.config.js)); `:root` light values equal Tailwind defaults exactly, `[data-theme="dark"]` remaps them ([index.css](../ui/src/index.css)). `white` stays literal (so `text-white` survives); dark card surfaces come from a `.bg-white → var(--surface)` override plus explicit handling for solid `bg-slate-900` buttons, `/40` overlays, and accent `-50/-100/-200` tints. `ThemeManager` (App root) applies `data-theme` to `<html>` from the `theme` preference, following the OS for `system` via `matchMedia`. Pure `resolveTheme()` is unit-tested ([theme.test.js](../ui/src/__tests__/theme.test.js)).

---

## Phase 4 — Tests & verification

- `ui/src/__tests__/preferences.test.js` (from Phase 0) — store behavior.
- `ui/src/__tests__/settings.test.js`:
  - renders all universal sections for `grc`; **Advanced** only for `it`; graceful with `persona=null`.
  - **no role-mutating control exists** — assert there is no `<select>`/button in AccountSection that writes persona (guards the locked-in constraint).
  - hash deep-link selects the right section; `#advanced` as non-`it` falls back to `account`.
- Extend `persona.test.js` (or a new case): `firstAccessiblePath()` honors a valid `landingPath`, ignores+clears an inaccessible one.
- Manual: `npm run dev` walk-through of the [acceptance criteria](settings_spec.md#acceptance-criteria); `npm test`; `npm run build` to confirm no import cycles.

---

## File-change summary

| File | Change |
|---|---|
| [ui/src/config.js](../ui/src/config.js) | + `APP_VERSION` |
| `ui/src/hooks/usePreferences.js` | **new** — store + hooks |
| `ui/src/components/Toggle.jsx` | **new** — extracted from LLMControl |
| `ui/src/components/SegmentedControl.jsx`, `SettingRow.jsx` | **new** — shared controls |
| [ui/src/pages/LLMControl.jsx](../ui/src/pages/LLMControl.jsx) | import shared `Toggle`, delete local copy |
| [ui/src/App.jsx](../ui/src/App.jsx) | + `/settings` route (un-guarded); root `data-*` attrs in Shell |
| [ui/src/components/TopBar.jsx](../ui/src/components/TopBar.jsx) | + `ROUTE_META['/settings']` |
| [ui/src/components/Sidebar.jsx](../ui/src/components/Sidebar.jsx) | footer link → active `NavLink`; `APP_VERSION`; PAGE_TITLES entry |
| `ui/src/pages/Settings.jsx` | **new** — layout host |
| `ui/src/components/settings/*.jsx` | **new** — 6 section components |
| [ui/src/contexts/PersonaContext.jsx](../ui/src/contexts/PersonaContext.jsx) | `firstAccessiblePath()` honors `landingPath`; doc comment |
| [ui/src/hooks/useAuth.js](../ui/src/hooks/useAuth.js) | + `getSessionExpiry()` helper |
| [ui/src/index.css](../ui/src/index.css) | density / reduce-motion rules |
| `ui/src/__tests__/preferences.test.js`, `settings.test.js` | **new** tests |

## Build order (each step independently mergeable/verifiable)

1. **Phase 0** — config const, preferences store, shared controls (+ store tests).
2. **Phase 1** — route + nav wiring + stub page → 404 gone, link works.
3. **Phase 2** — sections, top to bottom (Account first; it's the read-only core).
4. **Phase 3** — wire landing-page / density / reduce-motion effects.
5. **Phase 4** — tests + manual acceptance pass.

## Risks / watch-items

- **`useSyncExternalStore` snapshot stability** — return a cached snapshot ref; rebuild only on write, or React warns/loops. Cover in preferences.test.js.
- **Import cycle** — keep `usePreferences.js` free of any PersonaContext import; the access re-check lives in `firstAccessiblePath()`, which already has `hasAccess`.
- **StrictMode double-effect** (dev) — the expiry-countdown interval must clean up in its effect's teardown; the store subscribe/unsubscribe must be symmetric (CLAUDE.local.md flags StrictMode double-fire).
- **Density scope creep** — v1 deliberately limits `compact` to dense tables; resist styling every page now.
