// Custom Playwright reporter for the adversarial harness E2E layer.
//
// Purpose
// -------
// The harness's coverage builder (`src/coverage/builder.py::load_results`)
// reads a single `results.json` per layer. Playwright's built-in JSON reporter
// emits a Playwright-specific tree-shaped object (suites/specs/projects) that
// the builder would have to re-parse, and the harness-of-the-harness golden
// tests already mandate the flat `TestResult` shape (one row per test).
//
// This reporter listens to `onTestEnd` and accumulates one row per test in the
// builder's expected shape. `onEnd` writes the array to:
//   ${RUN_DIR}/e2e/results.json
// when RUN_DIR is set (the orchestrator at task 25 sets it), otherwise:
//   test-reports/_local/e2e/results.json
// matching the convention `playwright.config.js` already uses.
//
// How specs emit "TestResult" metadata
// ------------------------------------
// Playwright doesn't have a first-class "test metadata" API, so we use
// `testInfo.annotations`. Each test that needs to land in results.json must
// add an annotation with `type: 'harness-result'` and a JSON-stringified
// payload describing the fields the builder needs (target_kind / target_id /
// persona / evidence_path).
//
// Example annotation written by `pages-per-persona.spec.js`:
//
//   test.info().annotations.push({
//     type: 'harness-result',
//     description: JSON.stringify({
//       target_kind: 'page',
//       target_id: 'governance',
//       persona: 'ciso',
//       evidence_path: 'e2e/artifacts/e2e.page.governance.ciso.png',
//     }),
//   })
//
// Tests that don't add the annotation (the task-8 smoke test, future Cognito
// Hosted UI test) are intentionally absent from results.json — they're
// Playwright-only signals, not coverage-matrix targets.
//
// Test title convention
// ---------------------
// The test title IS the `test_id`. Specs must title their tests exactly
// `e2e.page.<page-id>.<persona-id>` (or whatever stable id the coverage
// matrix expects). The reporter does not synthesise an id from path + line.
//
// Status mapping
// --------------
// Playwright -> harness CellStatus (lowercase strings the builder accepts):
//   passed                    -> pass
//   failed | timedOut         -> fail
//   skipped                   -> skipped
//   interrupted               -> fail   (run was killed mid-test; treat as
//                                        a failure rather than mask it)
//
// Determinism
// -----------
// The accumulated rows are sorted by `test_id` before writing so a re-run
// produces byte-identical results.json when the same tests passed.
//
// Module system: CommonJS — matches playwright.config.js (see its header).

const fs = require('node:fs')
const path = require('node:path')

const HARNESS_ANNOTATION_TYPE = 'harness-result'
// Severity tag, optional, written into the row as `severity`. Specs push this
// via `testInfo.annotations.push({ type: 'severity', description: 'high' })`.
// The negative-gating spec (task 10) uses this to mark a leaked page (FAIL)
// as severity:high so the report ranks it correctly. If a spec pushes the
// annotation multiple times (e.g. defaults to 'low' then upgrades to 'high'
// on detected leak), the LAST one wins — matching the natural reading of
// "the spec's final classification."
const SEVERITY_ANNOTATION_TYPE = 'severity'
const ALLOWED_SEVERITIES = new Set(['low', 'medium', 'high', 'critical', 'info'])

function statusToHarnessStatus(playwrightStatus) {
  switch (playwrightStatus) {
    case 'passed':
      return 'pass'
    case 'failed':
    case 'timedOut':
    case 'interrupted':
      return 'fail'
    case 'skipped':
      return 'skipped'
    default:
      // Future-proof: an unknown Playwright status surfaces as a fail rather
      // than silently being dropped. The reporter doesn't try to guess.
      return 'fail'
  }
}

class HarnessResultsReporter {
  constructor(options = {}) {
    // `outputFile` may be overridden by the playwright.config reporter entry.
    // Default mirrors the path the coverage builder reads from.
    const runDir = process.env.RUN_DIR
      || path.resolve(__dirname, '..', '..', 'test-reports', '_local')
    this.outputFile = options.outputFile || path.join(runDir, 'e2e', 'results.json')
    this.rows = []
  }

  onBegin(_config, _suite) {
    // Nothing to do here — but the hook must exist so Playwright recognises
    // us as a valid reporter class.
  }

  onTestEnd(test, result) {
    // Extract the harness annotation if present. Specs that don't add it are
    // not part of the coverage matrix and we skip them silently.
    const harnessAnnotation = (test.annotations || []).find(
      (a) => a.type === HARNESS_ANNOTATION_TYPE,
    )
    if (!harnessAnnotation) return

    let metadata
    try {
      metadata = JSON.parse(harnessAnnotation.description || '{}')
    } catch (err) {
      // Malformed annotation is a bug in the spec, not in the test target.
      // Surface it loudly via console.error and skip the row — better than
      // silently dropping coverage signal.
      // eslint-disable-next-line no-console
      console.error(
        `[results-reporter] failed to parse harness annotation for test ` +
        `'${test.title}': ${err.message}. Raw: ${harnessAnnotation.description}`,
      )
      return
    }

    // The test title IS the test_id. This is enforced by convention in the
    // specs (see pages-per-persona.spec.js); the reporter only validates that
    // the title is non-empty.
    const testId = test.title
    if (!testId) {
      // eslint-disable-next-line no-console
      console.error('[results-reporter] test has no title, skipping row')
      return
    }

    // Defensive guard (C1 follow-up): the harness builder's `_validate_result`
    // refuses page-targeted rows with `persona === null` (raises
    // MissingPersonaError) and refuses any row with a missing target_kind /
    // target_id (raises UnknownTargetError). A spec that pushes a malformed
    // annotation would otherwise abort the entire `load_results → build_matrix`
    // step on the first such row. Skip-with-warn here keeps the rest of the
    // run salvageable AND surfaces the bug loudly enough to fix.
    if (!metadata.target_kind || !metadata.target_id) {
      // eslint-disable-next-line no-console
      console.warn(
        `[results-reporter] test '${testId}' annotation missing target_kind ` +
        `or target_id (got target_kind='${metadata.target_kind}', ` +
        `target_id='${metadata.target_id}'); skipping row to keep the run ` +
        `salvageable. Fix the spec's harness-result annotation.`,
      )
      return
    }
    if (metadata.target_kind === 'page'
        && (metadata.persona === null || metadata.persona === undefined)) {
      // eslint-disable-next-line no-console
      console.warn(
        `[results-reporter] test '${testId}' is page-targeted but ` +
        `persona is null/undefined; skipping row (builder would raise ` +
        `MissingPersonaError otherwise). Either add a persona or drop the ` +
        `harness-result annotation if this test isn't a coverage row.`,
      )
      return
    }

    const row = {
      test_id: testId,
      status: statusToHarnessStatus(result.status),
      layer: 'e2e',
      target_kind: metadata.target_kind,
      target_id: metadata.target_id,
      // Playwright reports duration in milliseconds; harness contract is
      // seconds (float), matching plan §5.2.
      duration_seconds: Math.round((result.duration || 0)) / 1000,
    }

    // Optional fields — only emit if the spec supplied them, so the JSON
    // doesn't carry `null` keys for tests that don't have a persona (e.g.
    // future api_route or agent_tool tests routed through this reporter).
    if (metadata.persona !== undefined) row.persona = metadata.persona
    if (metadata.evidence_path !== undefined) row.evidence_path = metadata.evidence_path
    if (metadata.skipped_reason !== undefined) row.skipped_reason = metadata.skipped_reason

    // Severity, optional. Walk annotations and take the LAST `severity` tag —
    // see SEVERITY_ANNOTATION_TYPE doc above for why "last wins". Unknown
    // severities are silently dropped so a typo in the spec doesn't corrupt
    // the report; we log to stderr to catch the typo at run time.
    const severityAnnotations = (test.annotations || []).filter(
      (a) => a.type === SEVERITY_ANNOTATION_TYPE,
    )
    if (severityAnnotations.length > 0) {
      const raw = severityAnnotations[severityAnnotations.length - 1].description
      const severity = typeof raw === 'string' ? raw.trim().toLowerCase() : ''
      if (ALLOWED_SEVERITIES.has(severity)) {
        row.severity = severity
      } else {
        // eslint-disable-next-line no-console
        console.error(
          `[results-reporter] test '${test.title}' has unknown severity ` +
          `'${raw}'. Allowed: ${[...ALLOWED_SEVERITIES].join(', ')}. Row written without severity.`,
        )
      }
    }

    // AC20 invariant: every FAIL row must carry an evidence_path. If the
    // spec didn't set one, fall back to Playwright's per-test output dir
    // (where it dropped its screenshot/trace on failure). The path is
    // relative to RUN_DIR so the report is forwardable.
    if (row.status === 'fail' && !row.evidence_path) {
      // testInfo.outputDir is normally under RUN_DIR/e2e/artifacts/<test>;
      // best we can do at reporter time is the test's outputPath base.
      const outputDir = (result.attachments || [])
        .map((a) => a.path)
        .filter(Boolean)[0]
      row.evidence_path = outputDir || `e2e/artifacts/${testId}/`
    }

    this.rows.push(row)
  }

  onEnd(_result) {
    // Sort by test_id so a re-run with identical pass/fail produces an
    // identical results.json (stable diff requirement, spec §1).
    const sorted = [...this.rows].sort((a, b) => {
      if (a.test_id < b.test_id) return -1
      if (a.test_id > b.test_id) return 1
      return 0
    })

    // Ensure parent dir exists. mkdirSync recursive is a no-op if it already
    // does; safe to call unconditionally.
    fs.mkdirSync(path.dirname(this.outputFile), { recursive: true })
    fs.writeFileSync(
      this.outputFile,
      JSON.stringify(sorted, null, 2) + '\n',
      'utf-8',
    )
  }

  // Required by Playwright's Reporter interface — printsToStdio: false stops
  // Playwright from suppressing its own console output on our behalf.
  printsToStdio() {
    return false
  }
}

module.exports = HarnessResultsReporter
