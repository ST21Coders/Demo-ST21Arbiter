// Block D classifier library — pure-function regex scans used by
// `e2e/tests/bundle-secrets.spec.js` and `bundle-tabnabbing.spec.js`.
//
// Everything in this file is deterministic + has no side effects: feed it
// strings, get back arrays of matches. The Playwright specs are thin wrappers
// that fetch the SPA bundle and feed the bytes through these classifiers.
// Python parity tests live at `tests/test_block_d_bundle_scanner.py` and
// re-implement the same regex set so the behavior is pinned from both sides.
//
// Module system: CommonJS (matches the other e2e/lib helpers).

// ─── Probe 1: hardcoded keys in JS bundles (checklist item #25) ────────────
//
// Each pattern is paired with a severity. The Slack/GitHub/AWS/Anthropic
// shapes are HIGH because finding any of them in a deployed bundle means a
// real credential is in the static asset. The generic JWT shape is MEDIUM —
// JWTs do legitimately appear in test fixtures or as base64 blobs and are
// flagged for human review.
//
// The `aws_secret_access_key` regex deliberately requires a quoted string of
// 30+ chars right after the assignment, because the bare phrase appears in
// AWS SDK type definitions and would otherwise alarm-spam. The minimum-length
// requirement on the OpenAI/Anthropic `sk-` prefix dodges short identifiers
// like `sk-12345` that occur in mock data.
const HARDCODED_KEY_PATTERNS = [
  {
    id: 'aws-access-key-id',
    severity: 'high',
    label: 'AWS access key id',
    // AWS access key IDs are exactly 16 uppercase-alphanumeric chars after AKIA.
    regex: /AKIA[0-9A-Z]{16}/g,
  },
  {
    id: 'aws-secret-access-key',
    severity: 'high',
    label: 'AWS secret access key assignment',
    // Either snake_case or camelCase variable name followed by an = / : and a
    // quoted string of at least 30 chars (real secret length is 40).
    regex: /(?:aws_secret_access_key|secretAccessKey)\s*[:=]\s*["']([^"']{30,})["']/g,
  },
  {
    id: 'jwt-shape',
    severity: 'medium',
    label: 'JWT-shape token (flag for review)',
    regex: /eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*/g,
  },
  {
    id: 'slack-token',
    severity: 'high',
    label: 'Slack token',
    regex: /xox[bpa]-[0-9]{10,}-[0-9]{10,}-[a-zA-Z0-9]{24,}/g,
  },
  {
    id: 'github-pat',
    severity: 'high',
    label: 'GitHub personal access token',
    regex: /ghp_[A-Za-z0-9]{36}/g,
  },
  {
    id: 'openai-anthropic-key',
    severity: 'high',
    label: 'OpenAI / Anthropic API key shape',
    regex: /sk-[A-Za-z0-9]{40,}/g,
  },
]

// ─── Probe 3: sensitive comments / debug logging (checklist item #42) ──────
//
// Severity is LOW (informational) per the task spec — a leftover TODO is
// embarrassing but not a credential. Each category is reported separately so
// the renderer can show "1 TODO + 0 FIXME + 0 leaked comment + 1 console.log"
// instead of collapsing them all to a single count.
const SENSITIVE_COMMENT_PATTERNS = [
  {
    id: 'todo-secret',
    severity: 'low',
    label: 'TODO referencing a secret keyword',
    regex: /TODO[^\n]{0,80}?(password|secret|token|key|admin)/gi,
  },
  {
    id: 'fixme-secret',
    severity: 'low',
    label: 'FIXME referencing a secret keyword',
    regex: /FIXME[^\n]{0,80}?(password|secret|token|key)/gi,
  },
  {
    id: 'html-comment-secret',
    severity: 'low',
    label: 'HTML comment with credential / debug keyword',
    // Non-greedy comment body up to the closing `-->`. Keyword check is
    // case-insensitive and tolerates word boundaries / punctuation.
    regex: /<!--[\s\S]{0,200}?(api[._-]?key|password|secret|admin|debug|backdoor)[\s\S]{0,200}?-->/gi,
  },
  {
    id: 'console-log-secret',
    severity: 'low',
    label: 'console.log() leaking sensitive identifier',
    regex: /console\.log\([^)]{0,200}?(password|token|secret|userid|email)[^)]{0,200}?\)/gi,
  },
]

// ─── Probe 1 / Probe 3 entrypoints ─────────────────────────────────────────

/**
 * Scan a single string for any hardcoded-key match. Returns a list of
 * `{patternId, severity, label, sample}` objects. The `sample` field is the
 * first 80 chars of the match — enough to identify the leak class, short
 * enough not to spill a real secret into the report.
 *
 * Deterministic: same input always produces the same ordered output.
 */
function scanForHardcodedKeys(text) {
  const findings = []
  if (typeof text !== 'string' || text.length === 0) return findings
  for (const pattern of HARDCODED_KEY_PATTERNS) {
    // Reset lastIndex — these regexes are module-level and carry state
    // between calls otherwise.
    pattern.regex.lastIndex = 0
    const matches = text.match(pattern.regex)
    if (!matches) continue
    for (const m of matches) {
      findings.push({
        patternId: pattern.id,
        severity: pattern.severity,
        label: pattern.label,
        // Truncate aggressively. A 30-char sample is enough to identify the
        // leak class without printing the full secret into the report.
        sample: m.slice(0, 30) + (m.length > 30 ? '…' : ''),
      })
    }
  }
  return findings
}

/**
 * Scan a single string for sensitive comments / debug-logging leaks.
 * Same shape as scanForHardcodedKeys; same determinism guarantee.
 */
function scanForSensitiveComments(text) {
  const findings = []
  if (typeof text !== 'string' || text.length === 0) return findings
  for (const pattern of SENSITIVE_COMMENT_PATTERNS) {
    pattern.regex.lastIndex = 0
    const matches = text.match(pattern.regex)
    if (!matches) continue
    for (const m of matches) {
      findings.push({
        patternId: pattern.id,
        severity: pattern.severity,
        label: pattern.label,
        sample: m.slice(0, 80) + (m.length > 80 ? '…' : ''),
      })
    }
  }
  return findings
}

// ─── Probe 4: Subresource Integrity (SRI) — checklist item #41 ─────────────
//
// Walk an HTML string for `<script src="...">` and
// `<link rel="stylesheet" href="...">` tags. For each tag whose URL hostname
// differs from the SPA's, check for an `integrity="..."` attribute.
//
// We use a minimal regex parser here rather than dragging in a full HTML
// parser — the SPA's index.html is small and predictable. Tags must be
// well-formed (closing `>` on the same line). If the SPA ever ships a
// multi-line script tag, this parser will miss it; that's an explicit
// trade-off documented inline.

/**
 * Extract `{tagName, src, integrity}` triples for every script + stylesheet
 * link in the HTML. Both tags self-close in HTML5 (`<script src="..." />`
 * isn't standard; we expect `<script src="...">...</script>` or `<script
 * src="..." defer></script>`). We only need the opening tag.
 */
function extractScriptsAndLinks(html) {
  const results = []
  if (typeof html !== 'string' || html.length === 0) return results

  // <script src="..."> — case-insensitive, attribute order flexible.
  const scriptRegex = /<script\b([^>]*)>/gi
  let m
  while ((m = scriptRegex.exec(html)) !== null) {
    const attrs = m[1]
    const src = _extractAttr(attrs, 'src')
    if (!src) continue
    results.push({
      tagName: 'script',
      url: src,
      integrity: _extractAttr(attrs, 'integrity'),
    })
  }

  // <link rel="stylesheet" href="..."> — we only care about stylesheet rels
  // (not preconnect/preload/icon).
  const linkRegex = /<link\b([^>]*)>/gi
  while ((m = linkRegex.exec(html)) !== null) {
    const attrs = m[1]
    const rel = (_extractAttr(attrs, 'rel') || '').toLowerCase()
    if (rel !== 'stylesheet') continue
    const href = _extractAttr(attrs, 'href')
    if (!href) continue
    results.push({
      tagName: 'link',
      url: href,
      integrity: _extractAttr(attrs, 'integrity'),
    })
  }

  return results
}

function _extractAttr(attrs, name) {
  // Double-quoted, single-quoted, or unquoted attribute value. Returns null
  // if the attribute is absent. The leading `\b` keeps `src` from matching
  // inside `srcset`, etc.
  const dquoted = new RegExp(`\\b${name}\\s*=\\s*"([^"]*)"`, 'i')
  const squoted = new RegExp(`\\b${name}\\s*=\\s*'([^']*)'`, 'i')
  const bare = new RegExp(`\\b${name}\\s*=\\s*([^\\s>]+)`, 'i')
  return (
    (attrs.match(dquoted) && attrs.match(dquoted)[1])
    || (attrs.match(squoted) && attrs.match(squoted)[1])
    || (attrs.match(bare) && attrs.match(bare)[1])
    || null
  )
}

/**
 * Decide whether a given URL is "third-party" relative to a same-origin host.
 * Anything that isn't a same-origin absolute URL or a relative path is treated
 * as third-party. Protocol-relative URLs (`//cdn.example.com/x.js`) are also
 * third-party if their host differs.
 *
 * `spaHostname` should be the hostname of the SPA (e.g. `d5u0vv1zl3eqd.cloudfront.net`).
 */
function isThirdPartyUrl(url, spaHostname) {
  if (typeof url !== 'string' || url.length === 0) return false
  // Relative paths (`/assets/foo.js`, `assets/foo.js`, `./x`) are first-party.
  if (!/^([a-z]+:)?\/\//i.test(url)) return false
  // Parse out the hostname. URL is universally available in modern Node.
  try {
    // Construct with a base in case the URL is protocol-relative.
    const u = new URL(url, `https://${spaHostname || 'example.com'}`)
    return u.hostname.toLowerCase() !== (spaHostname || '').toLowerCase()
  } catch {
    // Malformed URL — treat as third-party out of caution (better to false-
    // positive an SRI check than silently let a Magecart vector through).
    return true
  }
}

/**
 * Scan a parsed-tag list for SRI compliance. Returns `{thirdPartyCount,
 * missingSri}` where `missingSri` is the list of tags that need integrity
 * but don't have it. Caller decides PASS/FAIL/SKIP based on the counts.
 */
function scanForSriCompliance(tags, spaHostname) {
  const thirdParty = tags.filter((t) => isThirdPartyUrl(t.url, spaHostname))
  const missingSri = thirdParty.filter((t) => !t.integrity)
  return {
    thirdPartyCount: thirdParty.length,
    missingSri,
  }
}

// ─── Probe 5: tabnabbing — `target="_blank"` rel guards (item #59) ─────────
//
// Extract every `<a target="_blank" href="...">` tag from an HTML string,
// classify it as same-origin or external, and check the rel attribute for
// the required `noopener` and `noreferrer` tokens. Returns the list of
// external links that fail the check.
function extractTargetBlankAnchors(html) {
  const out = []
  if (typeof html !== 'string') return out
  const anchorRegex = /<a\b([^>]*)>/gi
  let m
  while ((m = anchorRegex.exec(html)) !== null) {
    const attrs = m[1]
    const target = (_extractAttr(attrs, 'target') || '').toLowerCase()
    if (target !== '_blank') continue
    const href = _extractAttr(attrs, 'href')
    if (!href) continue
    out.push({
      href,
      rel: (_extractAttr(attrs, 'rel') || '').toLowerCase(),
    })
  }
  return out
}

/**
 * Given an anchor's rel attribute, decide whether it satisfies the
 * tabnabbing-mitigation contract: both `noopener` and `noreferrer` tokens
 * present. Token-set comparison is whitespace-tolerant.
 */
function relProtectsAgainstTabnabbing(relValue) {
  if (typeof relValue !== 'string') return false
  const tokens = new Set(
    relValue.toLowerCase().split(/\s+/).filter(Boolean),
  )
  return tokens.has('noopener') && tokens.has('noreferrer')
}

// Public surface. Specs import from this module; tests pin the same
// behavior at `tests/test_block_d_bundle_scanner.py`.
module.exports = {
  HARDCODED_KEY_PATTERNS,
  SENSITIVE_COMMENT_PATTERNS,
  scanForHardcodedKeys,
  scanForSensitiveComments,
  extractScriptsAndLinks,
  isThirdPartyUrl,
  scanForSriCompliance,
  extractTargetBlankAnchors,
  relProtectsAgainstTabnabbing,
}
