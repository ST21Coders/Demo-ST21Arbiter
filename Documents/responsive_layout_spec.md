# Spec — Flexible / responsive content width across screen sizes

**Status:** Draft (spec only — no implementation yet)
**Owner:** UI (`ui/`)
**Version target:** bump `APP_VERSION` in [`ui/src/config.js`](../ui/src/config.js) (currently `1.2.0-poc`) on ship, per project convention.

---

## 1. Problem

ARBITER's app shell stretches correctly to the full viewport, but page **content** is capped at a fixed width, so on wide monitors the usable area stops around half the screen and the right side is dead space. On laptop-width screens the cap never engages, so it looks fine — which is why the bug only shows up on large displays.

### Root cause

The shell is full-width and healthy:

- [`ui/src/App.jsx:142-176`](../ui/src/App.jsx#L142-L176) — `Shell` is `flex h-screen`, sidebar + `flex-1` main column, `main` is `flex-1 overflow-y-auto`. No cap here.

The cap lives in **each page's outermost wrapper**, hardcoded per page:

| Page | File | Current wrapper |
|---|---|---|
| Dashboard | [`ui/src/pages/Dashboard.jsx:311`](../ui/src/pages/Dashboard.jsx#L311) | `p-6 space-y-6 max-w-6xl` |
| Findings | [`ui/src/pages/Findings.jsx:80`](../ui/src/pages/Findings.jsx#L80) | `p-6 space-y-5 max-w-6xl` |
| Finding detail | [`ui/src/pages/FindingDetail.jsx:42,70`](../ui/src/pages/FindingDetail.jsx#L70) | `p-6 ... max-w-5xl` / `max-w-6xl` |
| Action Center | [`ui/src/pages/ActionCenter.jsx:273`](../ui/src/pages/ActionCenter.jsx#L273) | `p-6 space-y-5 max-w-6xl` |
| Governance | [`ui/src/pages/Governance.jsx:169`](../ui/src/pages/Governance.jsx#L169) | `p-6 space-y-6 max-w-6xl` |
| Data Pipeline | [`ui/src/pages/DataPipeline.jsx:309`](../ui/src/pages/DataPipeline.jsx#L309) | `p-6 space-y-6 max-w-5xl` |
| Audit Logs | [`ui/src/pages/AuditLogs.jsx:116`](../ui/src/pages/AuditLogs.jsx#L116) | `p-6 space-y-5 max-w-6xl` |
| LLM Control | [`ui/src/pages/LLMControl.jsx:55`](../ui/src/pages/LLMControl.jsx#L55) | `p-6 space-y-5 max-w-5xl` |
| Token Usage | [`ui/src/pages/TokenUsage.jsx:141`](../ui/src/pages/TokenUsage.jsx#L141) | `p-6 space-y-6 max-w-6xl` |
| Personas | [`ui/src/pages/Personas.jsx:59`](../ui/src/pages/Personas.jsx#L59) | `p-6 space-y-6 max-w-6xl` |
| Settings | [`ui/src/pages/Settings.jsx:47`](../ui/src/pages/Settings.jsx#L47) | `p-6 max-w-6xl` |

`max-w-6xl` = 1152px, `max-w-5xl` = 1024px (Tailwind defaults). The Tailwind config ([`ui/tailwind.config.js`](../ui/tailwind.config.js)) adds no screens above the defaults, so nothing grows past `2xl` (1536px) anyway.

**Out of scope / leave alone** — these `max-w-*` are intentional and correct (cards, modals, chat bubbles, truncation widths), and must NOT be touched:
- Chat/message bubbles: `AnalystView.jsx`, `MCPChat.jsx` (`max-w-[75%]`, `max-w-[80%]`, `max-w-sm`).
- Modals/drawers: `ActionRequestModal.jsx`, `AnalystView.jsx` (`max-w-2xl`, `max-w-lg`), `TokenUsage.jsx:391` drawer (`max-w-md`).
- Auth card: `SignIn.jsx:12` (`max-w-md`).
- Inline `truncate max-w-[NNpx]` on table cells / labels everywhere.

Only the **page-level outer wrapper** caps are in scope.

---

## 2. Goal

Page content adapts to the viewport: on wide monitors it uses the available width (with a sane upper bound so 4K monitors don't produce unreadable 3000px-wide tables), and on laptop/tablet widths it behaves exactly as it does today (zero regression). The sidebar, top bar, and existing per-page paddings are unchanged.

### Non-goals
- No change to the sidebar, `TopBar`, routing, or auth.
- No mobile/hamburger nav redesign (the sidebar already shrinks; that's a separate effort).
- No change to chat bubbles, modals, or cell-truncation widths.
- No new layout library, no CSS framework swap — stay on Tailwind utilities + the existing `index.css` `@layer` pattern.

---

## 3. Approach

A single shared page-container utility replaces the ad-hoc `max-w-Nxl` on each page's outer wrapper. This centralizes the width decision in one place instead of 11 scattered literals.

### 3.1 Recommended: responsive container utility (default behavior, no user setting)

Add one component class in [`ui/src/index.css`](../ui/src/index.css) under the existing `@layer components` block, alongside `.card` etc.:

```css
/* ── Page content container — fluid up to a readable ceiling ──
   Replaces ad-hoc max-w-Nxl on page wrappers. Fills small/medium
   screens exactly as before; on wide monitors it widens in steps
   instead of stopping dead at 1152px. */
.page-container {
  @apply w-full mx-auto;
  max-width: 1280px;                 /* ≈ old 6xl+, baseline */
}
@media (min-width: 1536px) { .page-container { max-width: 1440px; } }
@media (min-width: 1920px) { .page-container { max-width: 1680px; } }
@media (min-width: 2560px) { .page-container { max-width: 2040px; } }
```

Then in each in-scope page, replace the wrapper, e.g.:

```diff
- <div className="p-6 space-y-6 max-w-6xl">
+ <div className="p-6 space-y-6 page-container">
```

Notes:
- Keep each page's existing `p-6` / `space-y-*` exactly — only the `max-w-Nxl` token is replaced.
- `mx-auto` centers the column so wide screens don't left-align content against the sidebar.
- The ceiling steps (1440 → 1680 → 2040) keep tables/text from stretching to unreadable line lengths on 4K while still using far more of the screen than today.
- These exact px values are a starting point — tune during implementation review against a real wide monitor.

**Pros:** one CSS change + 11 one-token edits; zero new state; no settings UI; behaves identically below 1536px (1280 vs 1152 is a minor, deliberate widening — confirm acceptable, or set baseline to `1152px` for pixel-identical small-screen behavior).

### 3.2 Optional add-on: user-selectable content width (Settings → Appearance)

If you want users to choose **Comfortable** (capped, current feel) vs **Full width** (fluid to viewport), layer this on top of 3.1. It fits the existing preferences architecture cleanly:

1. **Preference field** — add to `makeDefaults().appearance` in [`ui/src/hooks/usePreferences.js`](../ui/src/hooks/usePreferences.js):
   ```js
   contentWidth: 'comfortable',   // 'comfortable' | 'full'
   ```
   The store already merges-over-defaults, so old saved blobs stay valid with no version bump needed (`PREFS_VERSION` stays 1).

2. **Apply it** — `Shell` in [`ui/src/App.jsx:142`](../ui/src/App.jsx#L142) already spreads appearance prefs onto root `data-*` attributes (`data-density`, `data-reduce-motion`). Add `data-content-width={appearance.contentWidth}` the same way, then key the container off it in `index.css`:
   ```css
   [data-content-width="full"] .page-container { max-width: none; }
   ```
   `comfortable` = the stepped ceilings from 3.1; `full` = edge-to-edge (still inside `p-6`).

3. **Settings control** — add one `SettingRow` + `SegmentedControl` to [`ui/src/components/settings/AppearanceSection.jsx`](../ui/src/components/settings/AppearanceSection.jsx), mirroring the existing "UI density" row:
   ```jsx
   <SettingRow icon={Maximize2} label="Content width"
               desc="Comfortable caps content for readability; Full uses the whole screen on wide monitors.">
     <SegmentedControl
       value={contentWidth}
       onChange={v => setPreference('appearance.contentWidth', v)}
       options={[{ value: 'comfortable', label: 'Comfortable' }, { value: 'full', label: 'Full width' }]}
     />
   </SettingRow>
   ```

**Pros:** user control, consistent with theme/density/motion prefs already there, persists per-browser.
**Cons:** more surface area (preference + settings UI + a test). Only worth it if "let the user choose" is a real requirement; otherwise 3.1 alone solves the reported problem.

---

## 4. Files touched

**3.1 (recommended baseline):**
- [`ui/src/index.css`](../ui/src/index.css) — add `.page-container` class (+ breakpoints).
- 11 page files (table in §1) — swap `max-w-Nxl` → `page-container` on the outer wrapper only.
- [`ui/src/config.js`](../ui/src/config.js) — bump `APP_VERSION`.

**3.2 (optional add-on), additionally:**
- [`ui/src/hooks/usePreferences.js`](../ui/src/hooks/usePreferences.js) — add `contentWidth` default.
- [`ui/src/App.jsx`](../ui/src/App.jsx) — add `data-content-width` on the `Shell` root.
- [`ui/src/components/settings/AppearanceSection.jsx`](../ui/src/components/settings/AppearanceSection.jsx) — add the Content width row.

---

## 5. Test & verification plan

- **Manual (the actual bug):** `cd ui && npm run dev` → open the app on the wide monitor; confirm Dashboard/Findings/Audit Logs/etc. now use the screen at ≥1920px and look unchanged at ~1280px and ~1440px. Resize the window across breakpoints to confirm smooth stepping, no horizontal scroll, content stays centered.
- **Existing unit tests** ([`ui/src/__tests__/`](../ui/src/__tests__/)): run `npm test` — these cover helpers/mock data, not layout, so they should pass untouched. If 3.2 is built, add a small test asserting `contentWidth` defaults to `'comfortable'` and survives `mergeOverDefaults` of an old blob, matching the existing preferences test style.
- **Dark mode + density:** spot-check one dense page (Audit Logs) in dark theme + compact density to confirm the container change doesn't interact badly with `[data-theme="dark"]` / `[data-density="compact"]` rules in `index.css`.
- **No build regressions:** `npm run build` succeeds.

---

## 6. Open questions

1. **3.1 only, or 3.1 + the Settings toggle (3.2)?** The reported problem is fully solved by 3.1. 3.2 is only worth it if you want users to pick.
2. **Small-screen baseline:** OK to widen the baseline cap slightly (1152 → 1280) for a uniform fluid feel, or keep it pixel-identical at 1152px below 1536px?
3. **Width ceilings:** are the proposed steps (1440/1680/2040) right for your monitor, or do you want it to go fully edge-to-edge with no ceiling on very wide displays?
