# Research brief: column-sort arrows on the Audit Logs page

## Problem framing

The Audit Logs page renders a table with seven header cells (expand-toggle plus
Timestamp, Action, Resource, User, Status, Details) but the headers are inert.
Today, sorting is hardcoded to newest-first by timestamp and the only
user-controllable refinement is a single free-text search input. The user wants
clickable header arrows on Timestamp, Action, Resource, User, Status, and
Details that toggle ascending/descending sort, replacing the current
search-only experience as the main way to organize the table. This brief
collects the facts a designer needs to write that spec.

## Key findings

### 1. Where the page lives and what it renders today

- File: `ui/src/pages/AuditLogs.jsx` (213 lines, single component).
- The table has a 7-column header row at `AuditLogs.jsx:153-162`:
  expand-chevron, Timestamp, Action, Resource, User, Status, Details. Headers
  are `<th>` cells with no click handler, no icon, no `aria-sort`.
- Rows render with a click-to-expand chevron; clicking the row opens
  `<ExpandedDetail>` (a second `<tr>` with `colSpan={7}`) that shows the parsed
  JSON of `details`. This expand-on-row-click behavior will need to coexist
  with any new header-click behavior.
- A search-style filter input at `AuditLogs.jsx:135-143` does substring matching
  across `action_type`, `resource`, `user`, and `details`. It does not touch
  `timestamp` or `status`.
- Sort today: client-side, hardcoded:
  `[...filtered].sort((a, b) => (b.timestamp || '').localeCompare(a.timestamp || ''))`
  at `AuditLogs.jsx:99`. There is no user control over direction or column.

### 2. Why the current setup is "not good"

- The only sortable axis is timestamp, and the user cannot reverse it.
- No column gives any visual affordance that it could be sorted — headers look
  identical to plain text.
- Status and Action are categorical and would be useful sort keys for an
  auditor scanning by event type, but neither is reachable.
- The free-text filter is fine as a finder but is not a substitute for ordering
  by column.

### 3. Data shape — what each row actually has

From the live API (`Infra/functions/api_handler/api_handler.py:539-550`) the
response is `{logs: [...]}` where each item is a raw DynamoDB row from the
audit table. The mock fixture (`ui/src/mockData.js:726-772`) and the CSV
export header (`AuditLogs.jsx:106`) agree on the field set:

| Field | Type | Notes |
|---|---|---|
| `log_id` / `event_id` | string | Row key. Both names appear; UI prefers `event_id` then falls back. |
| `timestamp` | ISO-8601 string | Sortable lexicographically. Already used as the default sort key. |
| `action_type` | string enum-ish | e.g. `SCAN_TRIGGERED`, `CR_CREATED`, `JIRA_LINKED`. The page renders this as "Action" with `_` replaced by space. |
| `resource` | string | Free-form identifier. |
| `user` | string | Email or `system`. |
| `status` | string enum | `COMPLETED`, `PENDING_APPROVAL`, `APPROVED`, etc. Rendered through `<StatusBadge>`. |
| `details` | stringified JSON | Free-form. The visible cell value is derived by `shortDetails(log)` (`AuditLogs.jsx:33-52`), which is computed, not stored. |

Worth flagging: `details` is not naturally sortable. The displayed text comes
from a parsing heuristic that returns different shapes per action type. Sorting
by the rendered string would be meaningful only as alphabetic-of-derived-text,
which is not what an auditor would expect.

### 4. Where the data comes from and ordering guarantees

- UI hook: `useAudit()` at `ui/src/hooks/useApi.js:756-769` — `GET /audit`,
  reads `data.logs`.
- API: `_handle_list_audit` at `api_handler.py:539-550` does an unbounded
  `audit_table.scan(Limit=200)` and then sorts server-side by `timestamp`
  descending before returning. So the client receives at most 200 rows,
  pre-sorted newest-first.
- Volume / pagination: there is no client-side or server-side pagination. The
  hard cap is the 200-row scan limit. For a demo this is fine, but it means
  any "sort" the user picks operates on at most 200 rows already in memory —
  client-side sort is sufficient and there is no need to round-trip to the API.

### 5. Existing table conventions in the codebase

I searched all of `ui/src/` for `aria-sort`, `onSort`, `sortBy`, `sortField`,
`sortDir` — zero hits. There is no precedent for sortable column headers
anywhere in the app.

- `ui/src/pages/Findings.jsx` uses a hardcoded severity-order sort
  (`Findings.jsx:70`) and the same chevron-row-expand pattern as Audit Logs;
  headers are inert.
- `ui/src/pages/ActionCenter.jsx` has no sort/aria-sort references.
- No table library is installed. `ui/package.json` dependencies are
  `react`, `react-dom`, `react-router-dom`, `lucide-react`, `recharts`,
  `date-fns`. Anything we build is hand-rolled with Tailwind and `lucide-react`
  icons — which is consistent with the rest of the codebase.

So this feature would establish a new pattern. That is worth being deliberate
about because Findings, ChangeRequests, and Action Center would all reasonably
adopt the same headers later.

### 6. Tests

- No existing test file for Audit Logs. `ui/src/__tests__/` contains tests
  for helpers, edge cases, mock data, and the new Jira components, but
  nothing covering `AuditLogs.jsx`.

### 7. Common patterns for sortable column headers

Standard web patterns, ordered by complexity:

- **Single-column sort with a 2-state toggle** (asc ↔ desc). Click cycles
  between ascending and descending; one column is always sorted. Simplest
  mental model; matches GitHub issue lists, Linear, most admin dashboards.
- **Single-column sort with a 3-state toggle** (asc → desc → unsorted →
  default). Lets the user "clear" a sort and fall back to the natural order.
  Useful when the default sort (newest timestamp) is meaningful and the user
  wants to return to it.
- **Multi-column sort** (shift-click to add a secondary key). Power-user
  feature; overkill for ≤200 rows and adds non-obvious UI state.

Visual indicator placement: convention is a small up/down chevron rendered
inline with the header label, right-aligned within the cell or immediately
after the text. Inactive columns typically show a faint dual-chevron
(`ChevronsUpDown` in lucide) to advertise sortability; the active column shows
a single `ChevronUp` or `ChevronDown` at full opacity.

Accessibility: the WAI-ARIA pattern is to put `aria-sort="ascending" |
"descending" | "none"` on the active `<th>` (role `columnheader`) and wrap the
label in a `<button>` so it is focusable and operable by keyboard. Only one
header should carry a non-`none` value at a time. Source: MDN aria-sort.

Given this codebase is React + Tailwind + lucide-react with ~200 rows and no
table library, a hand-rolled single-column 2- or 3-state toggle with
`lucide-react` chevrons and `aria-sort` on the active header is the natural
fit. No new dependency required.

## Options and tradeoffs

### A. Add sort to all six data columns, keep search input, client-side only

Wire each header as a button. Clicking cycles asc/desc (and optionally
unsorted). Active column shows `ChevronUp`/`ChevronDown`; inactive show
`ChevronsUpDown` at reduced opacity. Sort runs after the existing filter, so
search + sort compose.

- Pros: solves the user's stated need with no API changes; client cap of 200
  rows makes this trivial; consistent with the codebase's pure-React style;
  sets a reusable pattern for Findings / CRs.
- Cons: sorting by `details` is misleading because the displayed value is
  derived. Sorting by `status` and `action_type` alphabetically is technically
  correct but may not match how an auditor mentally groups events (e.g. they
  may want `CRITICAL` before `INFO`, not alphabetic).

### B. Sort only on columns where order has real meaning (Timestamp, Action, Resource, User, Status)

Same as A but exclude Details. Optionally use a custom comparator for Status
that respects severity order rather than alphabetic.

- Pros: avoids misleading "sort by derived string" on Details. Better UX for
  Status.
- Cons: inconsistent header treatment may look unfinished. Custom Status
  ordering needs a documented canonical sequence.

### C. Replace the table with a richer filter bar (column dropdown filters plus a sort menu)

Drop per-column arrows, add a top-of-table dropdown filter per column plus a
single "Sort by" selector. This is closer to a Salesforce-style list view.

- Pros: more powerful, scales to many columns, leaves headers clean.
- Cons: not what the user asked for; bigger surface; introduces a UI pattern
  not used elsewhere in ARBITER.

### D. Move sorting to the server (`GET /audit?sort=field&dir=asc`)

API accepts sort params and returns rows pre-sorted.

- Pros: future-proofs for >200 rows, beyond the current scan limit.
- Cons: with a 200-row cap and an in-memory client list, this is solving a
  problem we do not have. Adds API surface, IAM, and tests for no demo-visible
  gain. Worth flagging only if the dataset will grow.

## Open questions

These are the calls the user/designer needs to make before a spec can be
written:

- Should the sort UI cover all six columns the user listed (Timestamp, Action,
  Resource, User, Status, **Details**), or skip Details since its visible text
  is a derived heuristic and alphabetic sort would be misleading?
- Single-column sort only, or should multiple columns be sortable at once
  (shift-click)?
- 2-state toggle (asc ↔ desc, always sorted) or 3-state (asc → desc →
  unsorted/default)? If 3-state, the default is newest-first by timestamp.
- What is the default sort on page load — keep "Timestamp descending" as it
  is today, or something else?
- Should the chosen sort persist across navigation away and back, or across
  full page reloads (localStorage / URL query param), or reset to default each
  visit?
- Should the existing free-text search input stay alongside the new arrows
  (compose: filter then sort), be replaced, or move into a different UI slot?
- For Status, should the order be alphabetic, or a custom canonical sequence
  (and if so, what is it — e.g. `FAILED` > `PENDING_APPROVAL` > `COMPLETED`)?
  Same question for `action_type` if relevant.
- Row click currently expands the row. Header click will be a separate
  affordance — confirm the expand/collapse behavior stays unchanged.
- Any keyboard/a11y bar to clear (e.g. WCAG 2.1 AA)? The expected pattern is
  `<button>`-wrapped header text plus `aria-sort` on the active `<th>`.
- Is server-side sorting needed (Option D), or is client-side over the current
  200-row cap fine for the demo horizon?

## Recommended direction

Lean: Option A or B (per-header arrows, client-side, single-column toggle,
hand-rolled with `lucide-react` chevrons and `aria-sort`). It maps directly to
what the user asked for, fits the codebase's existing Tailwind + React style,
needs no new dependency and no API change, and establishes a pattern Findings
and Change Requests can reuse later. Option B (skip sort on Details, custom
order for Status) is the more careful version and probably what an auditor
would actually want. This is a suggestion, not a decision — the designer
should pick after answering the open questions above.

## References

- `ui/src/pages/AuditLogs.jsx` — current page, lines 84-212.
- `ui/src/hooks/useApi.js:756-769` — `useAudit()` hook.
- `Infra/functions/api_handler/api_handler.py:538-550` — `/audit` route,
  server-side sort, 200-row scan limit.
- `ui/src/mockData.js:726-772` — `MOCK_AUDIT` fixture; field shape reference.
- `ui/src/pages/Findings.jsx`, `ui/src/pages/ActionCenter.jsx` — sibling
  tables; confirmed neither has sortable headers.
- `ui/package.json` — dependency list; no table library; `lucide-react`
  available for icons.
- [MDN: `aria-sort`](https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/Attributes/aria-sort) —
  valid values (`ascending`, `descending`, `none`, `other`) and role pairing
  (`columnheader`, `rowheader`).
