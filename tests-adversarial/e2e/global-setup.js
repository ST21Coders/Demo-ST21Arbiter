// Playwright globalSetup for the deployed-env E2E layer (task 8).
//
// Runs once, synchronously, before any test. Shells out to
// `python3.13 -m scripts.emit_storage_states <storage-states-dir>` which
// calls `src.identity.cognito_auth.fetch_all()` for the 4 demo personas and
// writes one Playwright storageState JSON file per persona.
//
// If the Python script exits non-zero (DEMO_PASSWORD unset, Cognito refused
// the credentials, COGNITO_USER_POOL_ID / COGNITO_CLIENT_ID missing, etc.)
// this function throws. Playwright reports "globalSetup failed" with the
// rejection's message and skips every test — no per-spec retries, no flaky
// half-runs.
//
// Why shell out to Python instead of porting the logic to JS:
//   - The Cognito helper, decode-JWT logic, and persona definitions all
//     already live in `src/identity/cognito_auth.py` (task 4).
//   - The other three harness layers (fuzz, auth, llm) call the same helper,
//     so duplicating it in JS would split the source of truth.
//   - boto3's USER_PASSWORD_AUTH flow is one line in Python; the JS AWS SDK
//     v3 equivalent is heavier and would add a runtime dep.
//
// Note on `python3.13`: the project pins CPython 3.13 (CLAUDE.md "Stack"
// section). Using `python3` here would pick up whatever Homebrew has
// symlinked, which on this team's laptops is often a different minor.
//
// Module system: CommonJS (matches playwright.config.js — see its header).

const { spawnSync } = require('node:child_process')
const path = require('node:path')
const fs = require('node:fs')

// Harness root is one level above e2e/. The Python module path
// `scripts.emit_storage_states` is resolved relative to this cwd.
const HARNESS_ROOT = path.resolve(__dirname, '..')
const STORAGE_DIR = path.join(__dirname, 'storage-states')

module.exports = async function globalSetup() {
  // Ensure the output directory exists so the script doesn't have to (also
  // makes the "no files written" failure mode legible — empty dir is a clue
  // that the script ran but produced no output).
  fs.mkdirSync(STORAGE_DIR, { recursive: true })

  const result = spawnSync(
    'python3.13',
    ['-m', 'scripts.emit_storage_states', STORAGE_DIR],
    {
      cwd: HARNESS_ROOT,
      encoding: 'utf-8',
      // Propagate the parent env so DEMO_PASSWORD / COGNITO_* / TARGET_BASE_URL
      // and PYTHONPATH all flow through. The harness assumes the operator has
      // already sourced `.env` into their shell — same model as the spec §8
      // env var table.
      env: process.env,
    },
  )

  if (result.error) {
    // ENOENT here means python3.13 is not on PATH.
    throw new Error(
      `globalSetup: failed to spawn python3.13: ${result.error.message}. ` +
      'Ensure python3.13 is installed and on PATH (CLAUDE.md pins CPython 3.13).',
    )
  }

  if (result.status !== 0) {
    const stderr = (result.stderr || '').trim()
    const stdout = (result.stdout || '').trim()
    throw new Error(
      `globalSetup: emit_storage_states exited ${result.status}.\n` +
      `stderr: ${stderr || '(empty)'}\n` +
      `stdout: ${stdout || '(empty)'}`,
    )
  }

  // Sanity check: confirm all 4 files landed on disk. The script prints one
  // path per persona on stdout; we verify the file actually exists rather
  // than trust the print.
  const expected = ['ciso', 'soc', 'grc', 'employee']
  const missing = expected.filter(
    (persona) => !fs.existsSync(path.join(STORAGE_DIR, `${persona}.json`)),
  )
  if (missing.length > 0) {
    throw new Error(
      `globalSetup: emit_storage_states succeeded but storage-state files ` +
      `missing for: ${missing.join(', ')}. Check ${STORAGE_DIR}/`,
    )
  }
}
