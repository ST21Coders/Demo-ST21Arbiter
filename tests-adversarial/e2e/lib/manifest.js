// Synchronous manifest loader used by E2E specs at spec-load time.
//
// Why a tiny helper module
// ------------------------
// Playwright's spec files run their top-level `for`-loop body at collection
// time (not at test-run time). We need the manifest available synchronously
// when `pages-per-persona.spec.js` and `negative-gating.spec.js` iterate to
// declare their parametrised tests, so we read + cache the file once per
// process. Doing this inline in each spec would risk drift if a future spec
// forgets to JSON.parse or mis-resolves the path.
//
// The file is `src/coverage/manifest.json` — the same hand-curated source of
// truth the Python coverage builder reads. Both layers MUST read the same
// file; the drift detector at `scripts/check_manifest_drift.py` reconciles it
// against `ui/src/pages/*.jsx` at the start of every orchestrated run.
//
// Module system: CommonJS.

const fs = require('node:fs')
const path = require('node:path')

// Manifest sits at <harness-root>/src/coverage/manifest.json. From this file
// that's three directories up (lib -> e2e -> harness-root).
const MANIFEST_PATH = path.resolve(
  __dirname,
  '..',
  '..',
  'src',
  'coverage',
  'manifest.json',
)

let cached = null

function loadManifest() {
  if (cached !== null) return cached
  const raw = fs.readFileSync(MANIFEST_PATH, 'utf-8')
  cached = JSON.parse(raw)
  return cached
}

// Convenience helpers — keep the spec files free of array methods that would
// otherwise be repeated across pages-per-persona.spec.js and
// negative-gating.spec.js (task 10).

function pages() {
  return loadManifest().pages
}

function personas() {
  return loadManifest().personas
}

function personaIds() {
  return personas().map((p) => p.id)
}

// Returns the page entry by id; throws if not found (catches typos in spec
// code that would otherwise produce a confusing undefined-deref later).
function pageById(id) {
  const found = pages().find((p) => p.id === id)
  if (!found) {
    throw new Error(`manifest.pages has no entry with id '${id}'`)
  }
  return found
}

module.exports = {
  MANIFEST_PATH,
  loadManifest,
  pages,
  personas,
  personaIds,
  pageById,
}
