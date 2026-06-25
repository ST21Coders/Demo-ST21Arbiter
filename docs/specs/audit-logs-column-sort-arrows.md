# Spec: column-sort arrows on the Audit Logs page

## Summary

The Audit Logs page (`/audit`) renders a 7-column table whose headers are
inert. Today the only ordering control is "newest timestamp first," hardcoded
client-side, and the page also carries a free-text search input that is being
removed as part of this change. Auditors who want to scan by Action, Status,
Resource, or User have no way to reorder the table. This feature turns the
column headers into clickable sort controls with visible up/down arrows on
five of the six data columns (Timestamp, Action, Resource, User, Status),
wired as a single-column, two-state asc/desc toggle. After this change the
column-sort arrows are the **entire** filtering and ordering UI on `/audit`:
there is no search box, no dropdown filter, no other refinement control. The
Details column stays inert because its visible value is a client-side
heuristic and alphabetic sort over derived text would mislead.

## Goals

- Let a viewer of `/audit` reorder the table by any of Timestamp, Action,
  Resource, User, or Status with a single click on the column header.
- Make sortability discoverable: every sortable header carries a visible icon
  affordance even when it is not the active sort column.
- Preserve the current default ordering (Timestamp descending) on first load
  and on every fresh visit to the page.
- Remove the existing free-text search input from the page so the sort arrows
  are the sole ordering and refinement UI.
- Meet standard sortable-table a11y semantics: keyboard-reachable headers,
  Enter/Space activation, correct `aria-sort` on each `<th>`.

## Non-goals

- No server-side sort. The endpoint and its 200-row scan limit stay as they
  are; sort runs client-side over the rows already in memory.
- No persistence of the user's sort choice — not in URL params, not in
  `localStorage`, not in React Router state. Sort resets to the default every
  time the user lands on `/audit`.
- No multi-column sort. No shift-click to add a secondary key. No hidden
  tiebreakers beyond what the underlying array order provides.
- No third "unsorted/default" toggle state. The active column is always sorted
  in one of two directions.
- No sort on the Details column. The header for Details stays inert text, with
  no icon and no `aria-sort`.
- No custom canonical ordering for Status or Action (e.g. severity-ordered
  Status, lifecycle-ordered Action). Both sort alphabetically.
- No changes to row-expand behaviour, to `<StatusBadge>` rendering, to the
  CSV-export path, or to any backend route.
- No reapplication of this pattern to Findings, Change Requests, or Action
  Center in this pass. Those pages keep their current header behaviour.
- No new dependency. Icons come from the already-installed `lucide-react`.

## Out of scope

- Bringing the free-text search back in any other form is explicitly not part
  of this change. No typeahead, no per-column filter dropdowns, no
  multi-select chips, no advanced-search modal. The sort arrows are the only
  refinement UI on `/audit` after this change ships.

## User stories

- As a SOC analyst, I want to sort the audit table by Action so that I can
  scan all `SCAN_TRIGGERED` events together when reviewing an incident.
- As a CISO, I want to sort by Status so that I can pull `PENDING_APPROVAL`
  rows to the top when reviewing what is waiting on me.
- As a GRC reviewer, I want to sort by User so that I can group every action
  taken by a single operator while preparing an evidence pack.
- As any auditor, I want to flip Timestamp between newest-first and
  oldest-first with a single click so that I can walk a timeline forward or
  backward without scrolling to the end of the table.
- As a keyboard-only or screen-reader user, I want the column headers to be
  focusable, activatable with Enter or Space, and to announce their sort
  state, so that the reordering control is usable without a mouse.

## UX details

### Sortable columns and their initial click direction

| Column      | Sortable | Default direction on first click | Comparator |
|-------------|----------|----------------------------------|------------|
| (expand)    | no       | n/a                              | n/a        |
| Timestamp   | yes      | descending (newest first)        | ISO-8601 string compare |
| Action      | yes      | ascending                        | case-insensitive string compare on `action_type` |
| Resource    | yes      | ascending                        | case-insensitive string compare on `resource` |
| User        | yes      | ascending                        | case-insensitive string compare on `user` |
| Status      | yes      | ascending                        | case-insensitive string compare on `status` |
| Details     | no       | n/a                              | n/a        |

Rationale on the per-column default direction: Timestamp defaults to
descending because the page's existing default (and the one auditors expect)
is newest-first. The four categorical columns default to ascending because A→Z
is the conventional first click for text columns and matches user expectations
from other list UIs.

### Page-load default

On first render of `/audit`, and on every fresh navigation to `/audit`, the
active sort is **Timestamp descending**. The Timestamp header shows the
active-descending indicator; the other four sortable headers show the
inactive-but-sortable indicator. Sort applies directly to the full result set
returned by `GET /audit` — there is no upstream filter step.

### Visual indicators

All three indicators sit immediately to the right of the column label inside
the header cell, vertically centered with the label text. Use icons from
`lucide-react` (already a dependency):

| Header state           | Icon          | Notes |
|------------------------|---------------|-------|
| Active, ascending      | `ArrowUp`     | Full opacity, same colour as the header text. |
| Active, descending     | `ArrowDown`   | Full opacity, same colour as the header text. |
| Inactive but sortable  | `ArrowUpDown` | Reduced opacity (e.g. ~50%) so it reads as an affordance hint, not as a current state. |
| Not sortable (Details) | none          | No icon. Header is plain text, identical to today. |

Icon size should match the existing header text size (small,
visually-balanced). Spacing between label and icon should match other
icon-plus-label patterns already used in the codebase (architect picks the
exact Tailwind classes).

### Interaction model

- Single-column sort, two-state toggle. Exactly one column is the active sort
  column at any time. That column is sorted either ascending or descending —
  there is no third "unsorted" state.
- Clicking the active column's header **toggles** its direction: asc → desc,
  desc → asc.
- Clicking an inactive sortable column's header makes that column active and
  sets its direction to its column-specific default from the table above
  (Timestamp → desc, every other sortable column → asc). The previously
  active column reverts to the inactive-but-sortable indicator.
- Clicking the Details header does nothing. Clicking the expand-chevron header
  cell does nothing.
- Clicking anywhere inside a body row continues to toggle that row's expansion
  exactly as today. Header clicks and body-row clicks are independent.
- There is no free-text search input on the page. The pipeline is
  **fetch → sort → render**: rows returned by `GET /audit` are sorted
  client-side by the active column's comparator and rendered.

### Accessibility

- Each sortable `<th>` carries `aria-sort` with one of:
  - `"ascending"` when this column is active and asc
  - `"descending"` when this column is active and desc
  - `"none"` when this column is sortable but not currently active
- The non-sortable headers (the expand-chevron column and Details) do **not**
  carry `aria-sort`.
- Only one `<th>` carries a non-`"none"` `aria-sort` value at any time.
- Each sortable header's interactive surface is a real `<button>` (or an
  element with `role="button"` and `tabIndex={0}`) wrapping the label and the
  icon. It must be reachable via Tab in source order and activatable with
  Enter and with Space. Activating it has the same effect as a mouse click on
  that header.
- The button's accessible name is the column label (e.g. "Timestamp"). The
  current sort state is conveyed by `aria-sort` on the `<th>`, not duplicated
  into the button's label.
- Focus styling on the header button must use the same focus-ring treatment
  the rest of the page uses for keyboard focus (architect picks exact
  classes).

### Comparator details

- **Timestamp**: sort by the raw `timestamp` field as a string using
  lexicographic compare. ISO-8601 strings sort correctly lexicographically.
  Asc = oldest first; desc = newest first. Matches today's default-sort
  comparator at `AuditLogs.jsx:99`.
- **Action**: sort by `action_type` (the underlying field, not the
  underscore-stripped display string), case-insensitive.
- **Resource**: sort by `resource`, case-insensitive.
- **User**: sort by `user`, case-insensitive.
- **Status**: sort by `status` (the underlying enum string, e.g.
  `PENDING_APPROVAL`), case-insensitive. No custom canonical order — `A` comes
  before `C` comes before `P`.

### Stability and missing-value handling

- The sort must be **stable**: rows that compare equal on the active key
  preserve their pre-sort relative order. This matters because the server
  returns rows in timestamp-desc order, so sorting by Action ascending should
  still produce timestamp-desc as the implicit tiebreaker.
- A row whose value for the active key is missing, `null`, `undefined`, or an
  empty string after trim sorts to the **end of the list regardless of
  direction**. This rule applies to all five sortable columns.

## Acceptance criteria

Search input removal:

- [ ] The Audit Logs page renders no free-text search input element. There is
      no `<input>`, `<textarea>`, or other text-entry control on the page
      whose purpose is to filter audit rows.
- [ ] No React state, hook, or handler related to a search query remains in
      the component. (No `searchQuery`/`searchTerm`/equivalent state; no
      `onChange` handler that filters the row list by substring.)
- [ ] No placeholder text, label, or icon for a search affordance is rendered
      anywhere on the page.

Page-load default:

- [ ] On a fresh visit to `/audit` (cold mount, no prior in-session state),
      rows are ordered by `timestamp` descending (newest first), matching the
      ordering produced by today's hardcoded sort.
- [ ] On that same fresh visit, the Timestamp header shows the
      active-descending icon (`ArrowDown`) and carries
      `aria-sort="descending"`.
- [ ] On that same fresh visit, each of Action, Resource, User, and Status
      headers shows the inactive-but-sortable icon (`ArrowUpDown`, reduced
      opacity) and carries `aria-sort="none"`.
- [ ] The Details header shows no sort icon and does not carry an `aria-sort`
      attribute.

Toggling the active column:

- [ ] Clicking the Timestamp header when it is active-descending flips the
      sort to ascending: the icon becomes `ArrowUp`, the `aria-sort` attribute
      becomes `"ascending"`, and the row order is reversed.
- [ ] Clicking the Timestamp header again flips back to descending: icon
      `ArrowDown`, `aria-sort="descending"`, row order reverses again.

Switching to a different sortable column:

- [ ] Clicking the User header when Timestamp is active makes User the active
      sort column with direction **ascending**. The User header shows
      `ArrowUp` and `aria-sort="ascending"`.
- [ ] In the same interaction, the Timestamp header reverts to the
      inactive-but-sortable icon (`ArrowUpDown`) and `aria-sort="none"`.
- [ ] Action, Resource, and Status behave identically to User when activated
      from an inactive state: first click sets direction to ascending.
- [ ] Clicking Timestamp from an inactive state sets its direction to
      **descending** (per the column-specific default), not ascending.

Sort behaviour over the data:

- [ ] Sort applies directly to the full result set returned by `GET /audit`;
      there is no upstream filter step.
- [ ] Action sort orders rows by `action_type` case-insensitively. Asc puts
      values starting with `A` above those starting with `C`; desc reverses.
- [ ] Status sort orders rows by `status` case-insensitively, alphabetically
      — no severity-style custom order.
- [ ] Resource and User sort the same way (case-insensitive alphabetic on
      their respective fields).
- [ ] The sort is stable: when two rows have the same value for the active
      key, their original relative order is preserved in the output. (Testable
      by sorting a fixture where multiple rows share a value and asserting
      their order matches the input order.)
- [ ] Rows whose active-key value is missing, `null`, `undefined`, or empty
      after trim appear **at the bottom of the rendered list regardless of
      sort direction**, on every one of the five sortable columns.

Row-expand coexistence:

- [ ] After any sort change, clicking a row body still toggles that row's
      expansion exactly as today.
- [ ] Clicking a sortable header does not toggle expansion on any row.
- [ ] If a row is expanded before a sort change, that row's expanded state
      survives the re-order (the expanded row moves with its parent row to
      the new position). If implementing that is non-trivial, collapsing all
      expansions on a sort change is an acceptable fallback — the architect
      decides which to do, but the chosen behaviour must be consistent.

Sort state does not persist:

- [ ] Navigating away from `/audit` (e.g. to `/findings`) and back to
      `/audit` resets the active sort to Timestamp descending. Any sort the
      user had set is gone.
- [ ] A full page reload likewise resets to Timestamp descending. There is
      no URL query parameter, no `localStorage` entry, and no `sessionStorage`
      entry that captures the sort.

Keyboard and a11y:

- [ ] Tab order reaches each sortable header in left-to-right column order.
      The expand-chevron header and the Details header are skipped (they are
      not interactive).
- [ ] With a sortable header focused, pressing Enter toggles or activates the
      sort exactly as a mouse click would.
- [ ] With a sortable header focused, pressing Space does the same. (Space
      must not scroll the page when a sortable header has focus.)
- [ ] At any moment, exactly one `<th>` in the table has an `aria-sort`
      attribute whose value is not `"none"`; the other sortable headers carry
      `aria-sort="none"`. The non-sortable headers carry no `aria-sort`
      attribute at all.
- [ ] Each sortable header button has a visible focus indicator when reached
      via keyboard.

## Edge cases and error states

- **All rows tie on the active key**: render in original order (the stability
  guarantee covers this). No special UI.
- **All rows have a missing value for the active key**: every row sorts to the
  end, which means the render order matches the original order. No special
  UI; no empty state message.
- **Zero rows returned from `GET /audit`**: the existing "no rows" rendering
  applies unchanged. The sort headers remain visible and clickable; clicking
  them is a no-op on an empty set.
- **Timestamp value is missing or malformed**: those rows sort to the bottom
  per the missing-value rule. They do not throw. (Today's hardcoded sort uses
  `(b.timestamp || '').localeCompare(a.timestamp || '')`, which already
  tolerates a missing timestamp by treating it as empty string; the new sort
  must remain at least that tolerant.)
- **`status` field absent entirely** (older audit rows): rows sort to the
  bottom when Status is the active column; the existing `<StatusBadge>`
  fallback continues to render the cell.
- **User holds Enter or Space on a focused header**: treat as a single
  activation. Do not flip the sort on every key-repeat tick. (A standard
  `<button>` element gets this for free; a `role="button"` element must
  handle the keydown explicitly.)
- **Very rapid clicking**: clicks are processed synchronously in click order.
  No queuing, no debounce.

## Open questions

None. All decisions called out in the research brief's "Open questions"
section have been resolved by the user before this spec was written:

- Sortable columns are Timestamp, Action, Resource, User, Status (Details
  excluded).
- Single-column sort, two-state toggle.
- Page-load default is Timestamp descending.
- No persistence across navigation or reload.
- The free-text search input is **removed** from the page. The sort arrows
  are the only refinement UI; pipeline is fetch → sort → render.
- Status and Action sort alphabetically, case-insensitive; no custom
  canonical order.
- Row-expand behaviour is unchanged.
- A11y target is standard sortable-table semantics (`aria-sort`, focusable
  button-wrapped header, Enter/Space activation).
- Client-side sort only. No server-side sort, no API changes.
