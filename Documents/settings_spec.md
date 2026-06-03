# Settings Page — Feature Specification

## Goal

Give every authenticated ARBITER user a single page at `/settings` that answers: "Who am I in this system, what can I change about how the app behaves for me, and where is this app pointed?" Today the Sidebar footer renders a Settings link to `/settings` ([Sidebar.jsx:208](../ui/src/components/Sidebar.jsx#L208)), but no route or page exists, so the link falls through to the catch-all `<Route path="*" element={<NotFound />} />` in [App.jsx](../ui/src/App.jsx) and the user sees a **404**. This spec replaces that dead link with a real, persona-aware settings surface.

## Why this matters (problem statement)

The Settings affordance is the one piece of chrome present on every page (it lives in the persistent Sidebar footer, below the nav and above the persona badge). A 404 behind a globally-visible link reads as a broken product, not a missing feature. Beyond fixing the link, the app currently has **no surface at all** for several things users expect:

- **No account view.** A user can't confirm which persona/role they're signed in as, which Cognito groups they hold, or which email the IdToken carries — all of which silently govern what they can see ([PersonaContext.jsx](../ui/src/contexts/PersonaContext.jsx)). When access is denied, they have nowhere to verify their own identity.
- **No personalization.** Every persona is dropped on a hardcoded landing page (`firstAccessiblePath()` in [PersonaContext.jsx](../ui/src/contexts/PersonaContext.jsx#L171)) with fixed density and no preference memory. There is no way to say "land me on Findings, not Dashboard."
- **No notification control.** The TopBar renders a `Bell` icon ([TopBar.jsx](../ui/src/components/TopBar.jsx)) with no preferences behind it.
- **No environment transparency.** Whether the app is in Mock Mode or Live (`USE_MOCK` in [config.js](../ui/src/config.js#L14)), which API endpoint it targets, and which build is running (`v1.0.0-poc`, hardcoded in the Sidebar footer) are invisible to users and only semi-visible to operators.
- **No session hygiene.** Sign-out exists only in the TopBar; there's no place that shows token expiry, refresh status, or a "sign out" with context.

This page consolidates all of the above into one predictable location.

## Primary persona

**All authenticated personas.** Unlike most pages in this app, Settings is **not** role-gated at the route level — every signed-in user (`employee`, `grc`, `soc`, `ciso`, `it`) needs account info and personal preferences. This mirrors how `/personas` is handled: it has **no entry** in `ROUTE_ACCESS`, and `hasAccess()` returns `true` for any path without an entry ([PersonaContext.jsx:157-162](../ui/src/contexts/PersonaContext.jsx#L157-L162)).

Within the page, individual **sections** are gated by persona/capability (see [Access model](#access-model)). Personal preferences (appearance, notifications) are universal; operator-facing detail (full API endpoints, advanced diagnostics) is gated to `it` via the existing `can()` capability checks.

## Scope

### In scope (v1, POC)

- New page `ui/src/pages/Settings.jsx`, wired into the Shell router and reachable from the existing Sidebar link with no 404.
- A persisted **preferences store** (`localStorage`) with a versioned schema, sensible defaults, and graceful fallback when storage is unavailable.
- Five sections: **Account & Identity**, **Appearance**, **Notifications**, **Session & Security**, **Workspace & Environment**, plus an `it`-only **Advanced** section.
- Preferences that take **real effect** in v1: default landing page, UI density, reduced motion.
- Functional **dark mode** (light/dark/system theme) — *shipped* in a follow-up after the original v1 (palette tokenized via CSS variables; see [Production hooks](#production-hooks) note, now done).
- "Reset to defaults" and local-data controls.

### Out of scope (v1)

- **Server-side persistence** of preferences. v1 is `localStorage`-only, per-browser, matching the app's existing client-only state convention (no Redux, in-memory state — see CLAUDE.md). A `GET/PUT /preferences` API is sketched under [Production hooks](#production-hooks) for later.
- ~~**Functional dark mode.**~~ *(Shipped in a follow-up — the slate/white palette was tokenized via CSS variables and the `theme` preference now flips it live.)*
- **Changing your persona/role.** Personas are derived from Cognito groups and are immutable in-session by design (CLAUDE.md: "The Personas page is read-only — no in-session switching"). Settings shows the persona; it never edits it.
- Editing guardrails/models — that already lives in [LLMControl.jsx](../ui/src/pages/LLMControl.jsx) (`/llm-control`); Settings links to it for `it`, it does not duplicate it.

## Information architecture

A two-column layout: a left **section rail** (sticky on desktop, collapses to a horizontal scroller / accordion on narrow widths) and a right **content panel** of stacked cards. This matches the visual language of [LLMControl.jsx](../ui/src/pages/LLMControl.jsx) (rounded-xl cards, `p-6` content, uppercase section labels, lucide icons) but adds a rail because Settings has more distinct sections than a single scroll wants.

```
Settings
├── Account & Identity        (all personas, read-only)
├── Appearance                (all personas)
├── Notifications             (all personas)
├── Session & Security        (all personas)
├── Workspace & Environment   (all personas; endpoint detail gated to it)
└── Advanced                  (it only)
```

Section selection is held in component state (default: `account`) and reflected in the URL hash (`/settings#appearance`) so links and refreshes are stable. No new route entries — the hash is handled inside the page.

## Section detail

### 1. Account & Identity (read-only)

Source of truth: `usePersona()` + `getEmail()`/`getGroups()` from [useAuth.js](../ui/src/hooks/useAuth.js).

| Field | Source |
|---|---|
| Display name | `persona.name` (e.g. "Priya Nair") |
| Title | `persona.title` |
| Role | `persona.role` + `persona.badge` chip, tinted with `persona.color` |
| Email | `email` (from IdToken `email` claim) |
| Cognito groups | `getGroups()` — rendered as chips; empty → "No group assigned" |
| Persona description | `persona.description` |
| Accessible pages | derived from `persona.access` mapped through `ROUTE_ACCESS` → human labels |

- Render the persona avatar/initials using `persona.initials` and `persona.gradient`, consistent with the existing persona badge.
- A muted note: *"Your role and access are assigned by your administrator via Cognito groups and cannot be changed here. See the [Personas](/personas) page for what each role can do."*
- **Edge:** when `persona` is `null` (authenticated but in no group), show the email, "Unassigned" role, and a callout matching the `AccessDenied` copy in [App.jsx](../ui/src/App.jsx) — preferences below still work.
- **Dev:** when `DEV_AUTH` is true, show a yellow "Local dev session" banner (consistent with the TopBar dev-persona styling) and surface that identity is from `sessionStorage`, not Cognito.

### 2. Appearance

All controls write to the preferences store ([Preferences model](#preferences-model)).

- **Default landing page** — segmented control / select of the personas's accessible routes (built from `persona.access` + `ROUTE_ACCESS`, plus the always-available `/personas`). Overrides `firstAccessiblePath()`. The Home shortcut in TopBar and post-callback redirect should consult this preference first (see [Wiring](#wiring--file-changes)).
  - **Validation:** if the stored landing path is no longer accessible (persona changed), silently fall back to `firstAccessiblePath()` and clear the stale value.
- **UI density** — `comfortable` (default) | `compact`. Applied via a `data-density` attribute on the app root that a small set of utility classes keys off (e.g. tighter `py` on list rows). v1 may scope this to obvious high-density surfaces (Findings table, Audit Logs) rather than every page.
- **Reduce motion** — boolean. Sets `data-reduce-motion` on the root; transitions/animations respect it. Default seeds from `prefers-reduced-motion`.
- **Theme** — `system` | `light` | `dark`. **Functional.** Applied to `<html data-theme>` by `ThemeManager` (App root); `system` follows the OS via `matchMedia` and live-updates. Implemented by tokenizing Tailwind's `slate` scale + `white` surfaces through CSS variables (see [index.css](../ui/src/index.css) / [tailwind.config.js](../ui/tailwind.config.js)); light values equal Tailwind defaults exactly (zero light regression). Default `system`.

### 3. Notifications

The Bell in [TopBar.jsx](../ui/src/components/TopBar.jsx) is currently inert. v1 stores per-category in-app notification preferences (no backend yet); the badge/count and any future toast wiring read these.

Categories (toggles, reuse the `Toggle` pattern from [LLMControl.jsx:30-43](../ui/src/pages/LLMControl.jsx#L30-L43)):

| Category | Default | Relevant personas |
|---|---|---|
| New critical findings | on | grc, soc, ciso |
| Change requests awaiting **my** approval | on | ciso, soc |
| Scan completion | on | grc, soc |
| Pipeline / KB sync status | off | it |
| Product / system announcements | on | all |

- Only show categories meaningful to the current persona (gate each row by `can()` / `persona.access`), so an `employee` sees just "announcements."
- Master toggle: "Pause all in-app notifications."
- Note that delivery is in-app only in the POC; email/Teams delivery is a [production hook](#production-hooks). (The repo already has `testing/teams_poster.py` — a future bridge, explicitly not wired here.)

### 4. Session & Security

Source: [useAuth.js](../ui/src/hooks/useAuth.js).

- **Signed in as** — email + role.
- **Session expiry** — read `expires_at` from the `arbiter.tokens` payload; show a live countdown ("expires in 42 min") and "auto-refresh enabled" (the app refreshes via `refresh()`). When `DEV_AUTH` is true, show "Local dev session — no token expiry."
- **Sign out** — calls existing `signOut()` ([useAuth.js:76](../ui/src/hooks/useAuth.js#L76)). Primary, clearly labeled button (today this only exists in the TopBar).
- **Switch user / change role** — there is **no** in-app role switch. Roles are immutable in-session (derived from Cognito groups). To act as a different role you **sign out and sign back in as a different user**. The Session section makes this explicit with a short note next to Sign out: *"To use a different role, sign out and sign in as another user — roles can't be changed in-session."* No dropdown, no role picker, nothing that mutates the persona.
- **Sign out everywhere** — *staged.* Disabled with a "coming soon" tooltip in v1; wires to Cognito `GlobalSignOut` later.
- This section is purely informational + the existing sign-out; it introduces no new auth surface and **no role-mutation surface**.

### 5. Workspace & Environment

- **Mode badge** — Mock vs Live, mirroring the TopBar badge driven by `USE_MOCK` ([config.js:14](../ui/src/config.js#L14)).
- **App version** — single source: promote `v1.0.0-poc` from the hardcoded Sidebar string into a constant (e.g. `APP_VERSION` in [config.js](../ui/src/config.js)) and read it in both places.
- **Region** — `COGNITO.region` (display only).
- **API endpoint / Chat endpoint** — `API_URL` / `CHAT_URL`. **Gated:** full URLs shown only to `it` (`can('viewLLMControl')` or a new `viewEnvironmentDetail` capability); other personas see only "Connected" / "Mock" status, not raw endpoints.
- A "Copy diagnostics" button (`it` only) copies a small JSON blob (mode, version, region, endpoints, persona, build time) for support — no secrets, no tokens.

### 6. Advanced (`it` only)

Gated by an existing IT capability (e.g. `can('viewLLMControl')`). Hidden entirely otherwise — never rendered as "Access restricted," because the rest of the page is universal.

- Quick links to the operator pages this persona owns: [LLM Control](/llm-control), [Data Pipeline](/pipeline), [MCP Admin](/mcp-chat).
- **Clear local app data** — wipes the preferences key and any cached UI state from `localStorage` (does **not** touch `arbiter.tokens` — that's Session's job). Confirm via inline two-step (no native `confirm()`), matching the app's in-UI affordance style.
- When `DEV_AUTH` is true, a note pointing at the TopBar dev-persona switcher (the canonical dev control already exists; Settings does not duplicate it).

## Preferences model

A single versioned object persisted under one key.

```
localStorage key: "arbiter.preferences"

{
  "version": 1,
  "appearance": {
    "landingPath": "/findings" | null,   // null = use firstAccessiblePath()
    "density": "comfortable" | "compact",
    "reduceMotion": boolean,
    "theme": "system" | "light" | "dark"
  },
  "notifications": {
    "paused": boolean,
    "criticalFindings": boolean,
    "crAwaitingMe": boolean,
    "scanComplete": boolean,
    "pipelineSync": boolean,
    "announcements": boolean
  }
}
```

Defaults (used when key is absent, malformed, or a field is missing):

```
landingPath: null, density: "comfortable",
reduceMotion: window.matchMedia('(prefers-reduced-motion: reduce)').matches,
theme: "system",
notifications: { paused:false, criticalFindings:true, crAwaitingMe:true,
                 scanComplete:true, pipelineSync:false, announcements:true }
```

Rules:
- **Versioned + forward-safe.** Reads merge stored values over defaults, so adding a field later never breaks an old saved blob. A `version` bump triggers a `migrate(old)` function.
- **Validated.** `landingPath` is re-checked against `hasAccess()` on read; invalid → `null`.
- **Resilient.** All reads/writes wrapped in `try/catch` (private-mode / disabled storage), falling back to an in-memory object — mirroring the `sessionStorage` guard pattern already used in [PersonaContext.jsx:182-185](../ui/src/contexts/PersonaContext.jsx#L182-L185) and [useAuth.js:19-23](../ui/src/hooks/useAuth.js#L19-L23). Settings must never crash the app.

## Access model

No new `ROUTE_ACCESS` entry — `/settings` is intentionally universal (like `/personas`). Add a one-line comment in [PersonaContext.jsx](../ui/src/contexts/PersonaContext.jsx#L85) next to the existing `/personas` note documenting this.

Within the page, gate sections with the existing `can()` mechanism. Two options for the `it`-only surfaces:

1. **Reuse** `can('viewLLMControl')` (already IT-only) as the "operator" gate. Lowest-friction; no map changes. **Recommended for v1.**
2. **Add** a dedicated `viewEnvironmentDetail` capability to `CAPABILITIES.it` in [PersonaContext.jsx](../ui/src/contexts/PersonaContext.jsx#L93) (and to the PERMISSION MODEL table in [persona_rbac_spec.md](persona_rbac_spec.md), per the in-lockstep comment). Cleaner semantically; defer unless option 1 proves too coarse.

Personal preference sections (Account, Appearance, Notifications, Session) require **no** capability — every authenticated persona manages their own.

## Wiring / file changes

Exact edits, smallest footprint first:

1. **[App.jsx](../ui/src/App.jsx)** — `import Settings from './pages/Settings'`; add a route inside `Shell`'s `<Routes>`, **before** the catch-all and **not** wrapped in `<Guarded>` (universal access, like `/personas`):
   ```jsx
   <Route path="/settings" element={<Settings />} />
   ```
2. **[TopBar.jsx](../ui/src/components/TopBar.jsx)** — add to `ROUTE_META`:
   ```js
   '/settings': { title: 'Settings', section: null },
   ```
   so the breadcrumb renders correctly.
3. **[Sidebar.jsx](../ui/src/components/Sidebar.jsx#L207-L213)** — the link already targets `/settings`. Upgrade it to use `NavLink` active styling consistent with the other nav items (it currently lacks the active border/treatment), so the footer link highlights when on the page. No new link needed.
4. **New: `ui/src/hooks/usePreferences.js`** — the store: load/merge/validate/migrate, `setPreference(path, value)`, `resetPreferences()`, and a subscribe mechanism (`useSyncExternalStore` over a module-level store, so multiple components stay in sync without a Provider — matching the no-Redux, lightweight-state ethos).
5. **New: `ui/src/pages/Settings.jsx`** — the page (rail + sections). Sections may be split into `ui/src/components/settings/*` if the file grows past ~250 lines.
6. **Consume preferences where they take effect:**
   - `landingPath` → in `firstAccessiblePath()` ([PersonaContext.jsx:171](../ui/src/contexts/PersonaContext.jsx#L171)) or at its call sites (TopBar Home shortcut, `/callback` redirect in [App.jsx](../ui/src/App.jsx), `PersonaRouteSync`). Preference wins **only if** still accessible; else fall back to current logic.
   - `density` / `reduceMotion` → `data-*` attributes set on the app root in `Shell` (or `main.jsx`) from the store.
7. **[config.js](../ui/src/config.js)** — add `export const APP_VERSION = '1.0.0-poc'`; reference it in both the Sidebar footer and the Settings Environment section (single source of truth).

## Component & styling conventions

- Reuse the look of [LLMControl.jsx](../ui/src/pages/LLMControl.jsx): outer `div className="p-6 space-y-5 max-w-5xl"` (rail makes it `max-w-6xl`), `cardStyle = { background:'#fff', border:'1px solid #e2e8f0', boxShadow:'0 1px 2px rgba(15,23,42,0.04)' }`, `rounded-xl p-4` cards, `text-[10px] uppercase tracking-wider` section labels, lucide icons.
- **Extract the `Toggle`** component from [LLMControl.jsx:30-43](../ui/src/pages/LLMControl.jsx#L30-L43) into `ui/src/components/Toggle.jsx` and import it in both places (don't copy-paste). Add a `SegmentedControl` and a small `SettingRow` (label + description + control) for consistent rows.
- Icons: `Settings`, `User`, `Palette`/`Sun`/`Moon`, `Bell`, `Shield`/`LogOut`, `Server`/`Globe`, `Wrench` — all already in the lucide set used across the app.
- Persona tinting uses `persona.color` / `persona.gradient`, consistent with Personas and the badge.
- Honor `reduceMotion` on this page's own transitions.

## Edge cases

- **No persona group** → Account shows "Unassigned"; Appearance landing-page select offers only `/personas`; everything else still renders.
- **`localStorage` unavailable** → in-memory fallback; a one-line muted note "Preferences won't persist in this browser session" in Appearance.
- **Stale `landingPath`** (persona lost access) → auto-cleared on read; no error.
- **`DEV_AUTH` mode** → Account/Session reflect the dev persona and show the local-dev banner; Session hides token countdown.
- **Mock mode** → Environment shows "Mock"; endpoint detail collapses to "Not connected (mock data)".
- **Malformed stored blob** → caught, replaced with defaults, optionally logged once.
- **Hash deep-link to a hidden section** (e.g. non-`it` hits `/settings#advanced`) → fall back to `account`.

## Production hooks (post-POC)

Documented so v1 doesn't paint into a corner:

- **Server-persisted preferences.** A 5th DDB table or a `preferences` map on the existing user record, fronted by `GET/PUT /preferences` in [api_handler.py](../Infra/functions/api_handler/api_handler.py) keyed on `_caller_user_id`. The client store gains a "sync on load / debounced write-through" layer; `localStorage` becomes the cache, not the source of truth.
- **Notification delivery.** Wire the stored categories to a real fan-out (in-app feed + optional email/Teams via the existing `testing/teams_poster.py` bridge).
- **Sign out everywhere.** Cognito `GlobalSignOut` on the access token, invalidating refresh tokens across devices.
- ~~**Functional dark mode.**~~ **Done** — slate scale + white surfaces tokenized as CSS variables driven by Tailwind config; `[data-theme="dark"]` on `<html>` flips them, applied by `ThemeManager`.

## Testing (Vitest, `ui/src/__tests__/`)

- `usePreferences`: returns defaults when key absent; merges partial blobs; rejects/clears invalid `landingPath`; survives `localStorage` throwing; `reset` restores defaults; version migration runs.
- `Settings` render: renders all universal sections for `grc`; shows **Advanced** only for `it`; renders gracefully with `persona = null`; deep-link hash selects the right section and rejects hidden ones.
- Landing-page integration: when a valid `landingPath` is set, `firstAccessiblePath()` (or its consumers) returns it; when inaccessible, falls back.
- Run: `cd ui && npx vitest run src/__tests__/settings.test.js`.

## Acceptance criteria

- [ ] Clicking **Settings** in the Sidebar shows the Settings page, never a 404.
- [ ] All five universal sections render for every persona; **Advanced** appears only for `it`.
- [ ] Account info matches the signed-in Cognito identity (name, role, email, groups) and is read-only.
- [ ] Changing **default landing page** changes where the Home shortcut / post-login redirect goes (when still accessible); changing **density** and **reduce motion** has a visible effect; all three survive a page reload.
- [ ] **Theme** selection persists across reload (visual change deferred, clearly labeled).
- [ ] Notification toggles persist and show only persona-relevant categories.
- [ ] **Sign out** works from the Session section.
- [ ] Environment shows Mock/Live correctly; raw endpoints visible only to `it`.
- [ ] No crash when `localStorage` is unavailable, the blob is malformed, or the user has no persona.
- [ ] `/settings` has no `ROUTE_ACCESS` entry and is reachable by all authenticated personas; the catch-all 404 is unaffected for genuinely unknown routes.
- [ ] No checked-in secrets; "Copy diagnostics" excludes tokens.

## Open questions

1. **Landing-page override location** — put the preference read inside `firstAccessiblePath()` (one place, affects all callers) or at each call site (more explicit, avoids surprising the function's other users)? *Recommendation: inside `firstAccessiblePath()`, with the accessibility re-check, since all current callers want the same behavior.*
2. **Density scope for v1** — every page, or just the dense ones (Findings, Audit Logs)? *Recommendation: start with the dense surfaces; expand later.*
3. **Advanced gate** — reuse `can('viewLLMControl')` or add `viewEnvironmentDetail`? *Recommendation: reuse for v1.*
4. **Ship theme control at all in v1**, or hide it until dark mode is real? *Recommendation: ship it labeled "Preview" so the follow-up is purely visual.*
