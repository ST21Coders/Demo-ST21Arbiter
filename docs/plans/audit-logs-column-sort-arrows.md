# Plan: column-sort arrows on the Audit Logs page

## Approach

This is a single-file UI change inside `ui/src/pages/AuditLogs.jsx`. The page
already does a client-side sort over the ~200 rows returned by `GET /audit`. We
keep that pipeline and replace the hardcoded `localeCompare` with a small
comparator driven by two pieces of component state: the active sort column and
its direction. Five of the seven header cells (Timestamp, Action, Resource,
User, Status) become real `<button>`-wrapped, keyboard-operable controls with
visible `lucide-react` arrow icons; the expand-chevron column and Details stay
inert. The free-text search input and its associated `filter` state come out
entirely so the only refinement UI on the page is the new arrows. No new files
under `components/` — the new `SortableTh` lives at the top of `AuditLogs.jsx`
as a local function component since the spec explicitly scopes reuse to this
page only.

On a fresh mount the state is `{ sortKey: 'timestamp', sortDir: 'desc' }`,
which matches today's visible ordering exactly, so Task 2 ships invisibly and
the user-facing change lands in Task 3 when the headers become clickable.
Persistence is intentionally none — local component state resets on every
mount, satisfying the "no URL/localStorage/sessionStorage" requirement for
free.

A test file is added at `ui/src/__tests__/AuditLogs.test.jsx` (none exists
today) covering the spec's acceptance criteria using Vitest + React Testing
Library, both of which are already wired up in `ui/package.json` and the rest
of `ui/src/__tests__/`.

## Architecture decisions

- **Keep `SortableTh` local to `AuditLogs.jsx`, not extracted to
  `ui/src/components/`.** The spec says applying this pattern to Findings and
  Change Requests is out of scope. Promoting it to a shared component now
  would invent a generic API (header label, column key, sort state shape,
  callback) before we know what those other pages need. Rejected alternative:
  put it in `ui/src/components/SortableTh.jsx`. Reason for rejection: premature
  abstraction; one caller, no second use case.
- **Single comparator function `sortRows(rows, key, dir)` over a per-column
  comparator map.** The five sortable columns reduce to two comparator shapes
  (ISO-8601 lex for timestamp, case-insensitive string for the other four).
  A switch inside one function is clearer than a `{ timestamp: fn, action: fn,
  ... }` map of near-identical closures. Rejected alternative: a comparator
  map. Reason: more code, no extensibility we actually need.
- **Rely on `Array.prototype.sort` stability.** ECMAScript 2019+ guarantees
  stable sort, and every browser the project targets (and Node ≥ 12, which
  Vitest uses) implements it. This means "sort by Action ascending preserves
  the server's timestamp-desc tiebreaker" is free — no index-tag scaffolding
  required. Rejected alternative: pre-tag each row with its original index and
  break ties on that index. Reason: it would work, but it is dead code given
  the spec's runtime targets. Documented in Risks below in case we ever drop
  support for an older runtime.
- **Missing values sink to bottom by extending the comparator, not by
  partitioning the array first.** The comparator returns `+1` for "a is empty,
  b is not" and `-1` for the reverse, regardless of `dir`. This keeps the
  whole sort in one pass and stays stable. Rejected alternative: split into
  `[withValue, withoutValue]`, sort the first, concat. Reason: two passes plus
  a concat, with no readability win.
- **Expanded-row state survives a sort change.** `expanded` is keyed by
  `event_id || log_id || row-index`. Since sorting just reorders the rendered
  array and the map keys do not change, an expanded row's expansion follows
  it to its new position with no extra work. The spec explicitly allows
  collapsing all on sort as a fallback, but we get the better behavior for
  free, so we take it.
- **Delete the search filter rather than relocate it.** The spec is explicit
  that the sort arrows are the only refinement UI after this change. Keeping
  `filter` state "just in case" would violate that. Rejected alternative: keep
  the state and hide the input. Reason: the spec acceptance criteria require
  the state to be gone, not just hidden.

## Data and interfaces

### State (inside `AuditLogs` component)

```js
const [sortKey, setSortKey] = useState('timestamp')
const [sortDir, setSortDir] = useState('desc')      // 'asc' | 'desc'
const [expanded, setExpanded] = useState({})        // unchanged
// filter state DELETED
```

### Column-default-direction map (module-level constant)

```js
const SORTABLE_COLUMNS = {
  timestamp: { label: 'Timestamp', field: 'timestamp', kind: 'iso',    defaultDir: 'desc' },
  action:    { label: 'Action',    field: 'action_type', kind: 'text', defaultDir: 'asc'  },
  resource:  { label: 'Resource',  field: 'resource',  kind: 'text',   defaultDir: 'asc'  },
  user:      { label: 'User',      field: 'user',      kind: 'text',   defaultDir: 'asc'  },
  status:    { label: 'Status',    field: 'status',    kind: 'text',   defaultDir: 'asc'  },
}
```

### Header click handler

```js
function handleSortClick(columnKey) {
  if (columnKey === sortKey) {
    setSortDir(d => (d === 'asc' ? 'desc' : 'asc'))
  } else {
    setSortKey(columnKey)
    setSortDir(SORTABLE_COLUMNS[columnKey].defaultDir)
  }
}
```

### Comparator

```js
// Returns a new sorted array. Does not mutate `rows`.
// - Empty / null / undefined / whitespace-only values sink to bottom regardless of dir.
// - 'iso' kind: lexicographic compare on the raw string (ISO-8601 sorts correctly lex).
// - 'text' kind: case-insensitive locale compare.
// - Relies on Array.prototype.sort stability (ES2019+).
function sortRows(rows, key, dir) {
  const col = SORTABLE_COLUMNS[key]
  if (!col) return rows
  const getVal = (r) => {
    const raw = r?.[col.field]
    if (raw == null) return ''
    return String(raw).trim()
  }
  const sign = dir === 'asc' ? 1 : -1
  return [...rows].sort((a, b) => {
    const va = getVal(a)
    const vb = getVal(b)
    const aEmpty = va === ''
    const bEmpty = vb === ''
    if (aEmpty && bEmpty) return 0
    if (aEmpty) return 1   // a sinks
    if (bEmpty) return -1  // b sinks
    if (col.kind === 'iso') {
      if (va === vb) return 0
      return va < vb ? -1 * sign : 1 * sign
    }
    // text
    return va.localeCompare(vb, undefined, { sensitivity: 'base' }) * sign
  })
}
```

### `SortableTh` (local component in `AuditLogs.jsx`)

```jsx
function SortableTh({ columnKey, label, sortKey, sortDir, onSort }) {
  const isActive = sortKey === columnKey
  const ariaSort = isActive ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'
  const Icon = isActive
    ? (sortDir === 'asc' ? ArrowUp : ArrowDown)
    : ArrowUpDown
  return (
    <th
      aria-sort={ariaSort}
      className="text-left px-4 py-3 text-slate-500 font-medium tracking-wide"
    >
      <button
        type="button"
        onClick={() => onSort(columnKey)}
        className="inline-flex items-center gap-1.5 hover:text-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 focus-visible:ring-offset-1 rounded-sm"
      >
        <span>{label}</span>
        <Icon size={12} className={isActive ? 'opacity-100' : 'opacity-40'} aria-hidden="true" />
      </button>
    </th>
  )
}
```

(Icons `ArrowUp`, `ArrowDown`, `ArrowUpDown` are added to the existing
`lucide-react` import at the top of the file.)

### Render call sites for the 5 sortable headers

```jsx
<SortableTh columnKey="timestamp" label="Timestamp" sortKey={sortKey} sortDir={sortDir} onSort={handleSortClick} />
<SortableTh columnKey="action"    label="Action"    sortKey={sortKey} sortDir={sortDir} onSort={handleSortClick} />
<SortableTh columnKey="resource"  label="Resource"  sortKey={sortKey} sortDir={sortDir} onSort={handleSortClick} />
<SortableTh columnKey="user"      label="User"      sortKey={sortKey} sortDir={sortDir} onSort={handleSortClick} />
<SortableTh columnKey="status"    label="Status"    sortKey={sortKey} sortDir={sortDir} onSort={handleSortClick} />
```

The expand-chevron `<th>` and the Details `<th>` remain plain `<th>` cells with
no `aria-sort` and no icon, identical to today.

### Sort-pipeline replacement

The current line 91-99 block (filter + sorted) collapses to:

```js
const sorted = sortRows(logs, sortKey, sortDir)
```

The "{N} rows" counter at line 142 keeps reading `sorted.length`.

## Files affected

- `ui/src/pages/AuditLogs.jsx` — remove search input + `filter` state; add
  `sortKey` / `sortDir` state and the column-default map; add `sortRows`
  comparator and `SortableTh` local component; swap five `<th>` cells for
  `<SortableTh>`; add `ArrowUp`, `ArrowDown`, `ArrowUpDown` to the
  `lucide-react` import.
- `ui/src/__tests__/AuditLogs.test.jsx` — NEW. Vitest + React Testing Library
  coverage for the acceptance criteria.

No other files change. No new dependencies. No API change.

## Task list

- [x] **Task 1: Remove the free-text search input and its `filter` state.**
  Delete `const [filter, setFilter] = useState('')` (line 86), the `filtered`
  computation (lines 91-96), and the `<input>` block inside the flex row
  (lines 135-143, but keep the `{sorted.length} rows` counter — move it so it
  stands on its own line or in a small flex row above the table). The
  hardcoded sort on line 99 now reads from `logs` directly:
  `const sorted = [...logs].sort((a, b) => (b.timestamp || '').localeCompare(a.timestamp || ''))`.
  Check: page renders, shows row count, no input present in the DOM,
  `npm run build` is clean.
  _Done: deleted `filter` state, `filtered` computation, and the search `<input>`; sort reads from `logs` directly; row counter now sits in a right-aligned flex row; `npm run build` succeeds._
- [x] **Task 2: Add sort state + `SORTABLE_COLUMNS` constant + `sortRows`
  comparator and wire it.** Add module-level `SORTABLE_COLUMNS`, component
  state `sortKey`/`sortDir` initialized to `'timestamp'`/`'desc'`, the
  `sortRows` helper, and the `handleSortClick` handler. Replace the line-99
  hardcoded sort with `const sorted = sortRows(logs, sortKey, sortDir)`.
  Headers remain inert; nothing in the UI changes visually because the
  initial state reproduces today's ordering exactly. Check: page still renders
  newest-first; toggling `sortKey`/`sortDir` via React DevTools reorders the
  table; missing-`timestamp` rows sink to the bottom in either direction.
  _Done: added module-level `SORTABLE_COLUMNS` (field-name keyed) and `sortRows` with empty-to-bottom invariant; added `sortKey`/`sortDir` state defaulting to `'timestamp'`/`'desc'`; replaced inline `.sort((a,b)=>…)` with `useMemo`-wrapped `sortRows(logs, sortKey, sortDir)`; headers untouched. `handleSortClick` deferred to Task 3 where it gets wired into headers. `npm run build` clean; no AuditLogs tests exist yet (Task 4)._
- [x] **Task 3: Introduce `SortableTh` and wire the five sortable headers.**
  Add `ArrowUp`, `ArrowDown`, `ArrowUpDown` to the `lucide-react` import. Add
  the `SortableTh` local function component above `AuditLogs`. Replace the
  Timestamp, Action, Resource, User, and Status `<th>` cells (lines 156-160)
  with `<SortableTh>` calls. Leave the expand-chevron `<th>` (line 155) and
  the Details `<th>` (line 161) untouched. Verify by clicking each header
  that it sorts as specified, that exactly one `<th>` carries a non-`"none"`
  `aria-sort` at a time, that Tab order reaches the five buttons in
  left-to-right column order, and that Enter and Space activate a focused
  header. Check: all acceptance criteria except those covered only by tests
  pass via manual interaction in `npm run dev`.
  _Done: added `ArrowUp`/`ArrowDown`/`ArrowUpDown` to the lucide import; added local `SortableTh` component above `AuditLogs` (sets `aria-sort` on the `<th>`, renders a real `<button>` with label + icon); added `handleSortClick` that toggles direction on same-column clicks and falls back to `SORTABLE_COLUMNS[columnKey]` default for new columns; swapped the five sortable `<th>` cells for `<SortableTh>` calls while leaving the expand-toggle and Details headers as plain `<th>`. Inactive icon uses `opacity-40`; focus ring uses `focus-visible:ring-2 focus-visible:ring-indigo-500/50 focus-visible:ring-offset-1` matching the codebase's `index.css` `.btn-*` focus convention. `npm run build` clean._
- [x] **Task 4: Add the test file.** Create
  `ui/src/__tests__/AuditLogs.test.jsx`. Mock `useAudit` (the way other tests
  in `ui/src/__tests__/` mock `useApi`) to return a small in-memory fixture
  with: rows in non-timestamp-desc order on input, rows that share an `action_type`
  value (to exercise stability), at least one row with an empty `user`, at
  least one row with `status` missing. Cover the spec's acceptance criteria:
  (a) default sort is Timestamp desc and the Timestamp header carries
  `aria-sort="descending"` with `ArrowDown` rendered; (b) the other four
  sortable headers carry `aria-sort="none"`; (c) Details `<th>` has no
  `aria-sort` attribute; (d) clicking Timestamp flips asc/desc and reorders;
  (e) clicking User from inactive sets `aria-sort="ascending"` and the
  previous active header reverts to `aria-sort="none"`; (f) clicking
  Timestamp from inactive sets it to descending, not ascending; (g) Action
  sort is case-insensitive; (h) sort is stable on ties; (i) rows with
  empty/missing active-key value render last in both directions; (j) no
  `<input>` of `type="text"` or `type="search"` is in the DOM (search-input
  removal); (k) keyboard: focusing a header and pressing Enter activates
  sort; same for Space. Use `userEvent` from `@testing-library/user-event`
  for keyboard, matching the pattern in
  `ui/src/__tests__/JiraTicketModal.test.jsx`. Check: `npm test` passes.
  _Done: added `ui/src/__tests__/AuditLogs.test.jsx` with 12 passing tests covering default sort + aria-sort, non-sortable headers carry no aria-sort, search-input removal (no textbox/searchbox), clicking active column flips asc/desc, switching columns resets the previous column to `aria-sort="none"`, empty-user-sinks-to-bottom in both directions, stable sort on shared `action_type`, keyboard Space/Enter activation, row expansion still works after a sort change, and sort state resetting on remount. Mocked `useAudit` via hoisted state holder (pattern from `AnalystView.integration.test.jsx`); fixture has 4 rows with one empty `user` and two rows tying on `action_type`. `npx vitest run src/__tests__/AuditLogs.test.jsx` -> 12/12 pass._
- [ ] **Task 5 (optional polish, only if it didn't already land in Task 3):**
  tune icon opacity for inactive headers (~`opacity-40`), confirm the
  focus-visible ring matches the rest of the page, confirm spacing between
  label and icon (`gap-1.5`) reads cleanly at the page's `text-xs` size.
  Check: visual review in `npm run dev`; a11y check via browser DevTools that
  the focus ring is visible on Tab.

## Risks

- **`Array.prototype.sort` stability.** Guaranteed by ES2019+ across all
  modern browsers and Node ≥ 12. If we ever target an older runtime, the
  stability acceptance criterion (`stable: ties keep input order`) breaks
  silently. Mitigation if that ever happens: change `sortRows` to tag each
  row with its input index, compare on that index as a final tiebreaker. Not
  worth doing now.
- **Missing timestamp on every row.** The spec says missing values sink to
  bottom regardless of direction. If every row has a missing timestamp, the
  comparator returns `0` for every pair and the original order is preserved.
  That matches today's behavior (`localeCompare('', '') === 0`) so no
  regression, but worth a test case.
- **Existing tests that import `AuditLogs.jsx`.** Research confirmed there is
  no existing test for this page and no other test imports it. Removing the
  search input cannot break any existing test. Verified by grepping
  `ui/src/__tests__/` for `AuditLogs` (zero hits).
- **CSV export.** Currently exports `logs` (the unfiltered, unsorted raw
  array), not `filtered` or `sorted`. We are not changing the CSV path. After
  the change it still exports `logs` in whatever order the API returned. The
  spec is silent on CSV; leaving it alone matches "no changes to the
  CSV-export path."
- **Row-expand survival across sort.** We are relying on `expanded` being
  keyed by stable row id (`event_id || log_id`). If a fixture row has neither
  and falls back to `row-${i}` (which is index-based), its expansion would
  jump to a different row after sort. Real audit rows always have
  `event_id`/`log_id`; mock fixtures used in the test should include one of
  these. Document this as a known limitation in the test setup rather than
  defending against it in production code.
- **`aria-sort="none"` vs omitting the attribute.** The spec is explicit: the
  three sortable-but-inactive headers carry `aria-sort="none"`; the two
  non-sortable headers carry no `aria-sort` attribute at all. The `SortableTh`
  component always emits `aria-sort`; the plain `<th>` cells for the
  expand-chevron and Details columns must continue to emit none. Easy to get
  wrong if someone reflexively adds `aria-sort="none"` to every header for
  consistency — don't.
