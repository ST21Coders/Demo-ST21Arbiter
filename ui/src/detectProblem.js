// Client-side conversation analysis that decides whether a "Create Ticket"
// (action request) button should surface after an assistant message, and
// pre-extracts the fields the action-request API needs.
//
// All logic is heuristic — runs synchronously after every assistant turn, no
// extra Bedrock calls. Tuned for ARBITER-style chat content (policy conflicts,
// AWS resources, regulatory frameworks).

const SEVERITY_RULES = [
  { sev: 'CRITICAL', terms: ['critical', 'breach', 'data loss', 'production down', 'outage', 'pci dss', 'naic mdl', 'sox §404'] },
  { sev: 'HIGH',     terms: ['high', 'major', 'exposed', 'non-compliant', 'non_compliant', 'violation', 'violates'] },
  { sev: 'MEDIUM',   terms: ['medium', 'workaround', 'partial'] },
  { sev: 'LOW',      terms: ['low', 'cosmetic', 'minor'] },
]

const CATEGORY_RULES = [
  { cat: 'Security',       terms: ['mfa', 'ssl', 'tls', 'encryption', 'waf', 'breach', 'vulnerability', 'exposed', 'pci', 'auth', 'iam'] },
  { cat: 'Infrastructure', terms: ['vpc', 's3', 'alb', 'ec2', 'security group', 'sg-', 'subnet', 'peering', 'route table', 'cloudformation'] },
  { cat: 'Access',         terms: ['permission denied', 'access denied', 'zscaler', 'url category', 'blocked'] },
  { cat: 'Performance',    terms: ['latency', 'slow', 'throttl', 'timeout', 'degraded'] },
  { cat: 'Bug',            terms: ['error', 'exception', 'stack trace', 'crash'] },
]

const ACTION_TYPE_RULES = [
  { type: 'SECURITY_FIX',         terms: ['mfa', 'ssl', 'waf', 'breach', 'exposed', 'vulnerability', 'pci', 'encryption'] },
  { type: 'CONFIGURATION_CHANGE', terms: ['sg-', 'security group', 'vpc', 'peering', 'subnet', 's3', 'alb'] },
  { type: 'RULE_UPDATE',          terms: ['zscaler', 'url category', 'firewall rule', 'urlcat'] },
  { type: 'POLICY_UPDATE',        terms: ['mig-pol', 'policy', 'document'] },
  { type: 'ACCESS_CHANGE',        terms: ['iam', 'permission', 'role', 'access'] },
]

const RESOURCE_PATTERNS = [
  /sg-[a-z0-9-]{4,}/i,
  /vpc-[a-z0-9]{4,}/i,
  /subnet-[a-z0-9]{4,}/i,
  /MIG-POL-[A-Z0-9-]+/,
  /ARBITER-UC\d+/i,
  /mig-prod-[a-z0-9-]+/i,
  /[a-z0-9-]+\.com\b/i,
  /[A-Z]{2,}-[A-Z]+-[A-Z0-9-]+/,
]

function lower(s) { return (s || '').toLowerCase() }

function pickFirstMatch(text, rules, defaultValue) {
  const lo = lower(text)
  for (const r of rules) {
    if (r.terms.some(t => lo.includes(t))) return r.sev || r.cat || r.type
  }
  return defaultValue
}

// Section-label words the assistant emits as standalone heading lines. We
// skip past these when hunting for a real title so we don't ship "Summary"
// or "Findings" as the ticket subject.
const SECTION_LABEL_LINES = new Set([
  'summary', 'findings', 'recommendation', 'recommendations',
  'sources', 'context', 'analysis', 'overview', 'background',
])

// Leading phrases that signal a non-answer ("the available tools cannot…",
// "I'm not sure…"). When the assistant opens with one of these we fall back
// to the user's question for the title — what they asked is more useful than
// what the agent failed to answer.
const NON_ANSWER_PREFIXES = [
  'the available tools',
  "i'm not sure",
  'i am not sure',
  "i don't have",
  'i do not have',
  'unable to determine',
  'cannot determine',
  'no information',
]

function extractTitle(assistantText, userText) {
  const lines = (assistantText || '').split('\n').map(l => l.trim()).filter(Boolean)
  // Pass 1 — prefer a bold/heading-style line that names the finding.
  for (const line of lines) {
    const m = line.match(/^\*\*(.+?)\*\*$/) || line.match(/^Finding:\s*(.+)/i) || line.match(/^(ARBITER-UC\d+.+)/i)
    if (m) {
      const t = stripMarkdown(m[1])
      if (t.length >= 8 && t.length <= 140 && !SECTION_LABEL_LINES.has(t.toLowerCase())) {
        return truncate(t, 110)
      }
    }
  }
  // Pass 2 — first sentence-shaped chunk, skipping section labels and
  // non-answer openings. If the assistant didn't really answer, use the
  // user's question instead.
  const cleaned = (assistantText || '')
    .replace(/\s+/g, ' ')
    .split(/[.!?]\s/)
    .map(s => stripMarkdown(s))
    .filter(s => s.length > 12 && !SECTION_LABEL_LINES.has(s.toLowerCase()))
  for (const s of cleaned) {
    const lo = s.toLowerCase()
    if (NON_ANSWER_PREFIXES.some(p => lo.startsWith(p))) break  // bail to user text
    return truncate(s, 110)
  }
  return truncate(stripMarkdown(userText || 'Issue reported in chat'), 110)
}

function extractResource(text) {
  for (const pat of RESOURCE_PATTERNS) {
    const m = (text || '').match(pat)
    if (m) return m[0]
  }
  return ''
}

function extractEnvironment(text) {
  const lo = lower(text)
  if (/\bprod(uction)?\b/.test(lo)) return 'PROD'
  if (/\bpre[\s_-]?prod\b/.test(lo)) return 'PRE_PROD'
  if (/\bstag(ing|e)\b/.test(lo)) return 'STAGING'
  if (/\bdev(elopment)?\b/.test(lo)) return 'DEV'
  return 'PROD'
}

function extractSteps(messages) {
  // Look for numbered/bulleted lists or "Steps:" blocks in the most recent
  // user message.
  const lastUser = [...messages].reverse().find(m => m.role === 'user')
  if (!lastUser) return ''
  const m = lastUser.content.match(/steps[^\n]*\n([\s\S]{0,400})/i)
  return m ? m[1].trim() : ''
}

function stripMarkdown(s) {
  return (s || '').replace(/\*\*/g, '').replace(/[#*_`]/g, '').replace(/\s+/g, ' ').trim()
}

function truncate(s, n) {
  if (!s) return ''
  return s.length <= n ? s : s.slice(0, n - 1).trimEnd() + '…'
}

// Markers placed by the chat surfaces on messages that the heuristic must
// ignore — initial greetings, transport-error stubs, and the confirmation
// message we ourselves post after a successful ticket creation. Anything
// where `system: true` is set on the message is skipped.

// Belt-and-braces: text shapes the chat uses for transport errors before this
// flag existed (older history rows in DDB may still match).
const SYSTEM_ERROR_SHAPES = [
  /^[⚠✅]/,
  /\bchat failed\b/i,
  /\bagent error\b/i,
  /\bcould not load session\b/i,
]

function looksLikeSystemMessage(msg) {
  if (!msg) return true
  if (msg.system) return true
  const content = (msg.content || '').trim()
  if (!content) return true
  return SYSTEM_ERROR_SHAPES.some(re => re.test(content))
}

// Extract ticket prefill fields from the latest assistant answer. Returns
// hasProblem:true with those fields for any genuine Q&A turn so the Create
// Ticket button can surface on every question; returns { hasProblem: false }
// only for non-answers (initial greeting, transport errors, or before the
// user has asked anything).
export function detectProblem({ messages, sessionId, sessionTitle } = {}) {
  const msgs = Array.isArray(messages) ? messages : []
  if (!msgs.length) return { hasProblem: false }

  const lastAssistant = [...msgs].reverse().find(m => m.role === 'assistant')
  if (!lastAssistant) return { hasProblem: false }

  // Initial greetings and transport errors do not count as identifying a
  // problem — even if they mention "conflict" or "failed" by coincidence.
  if (looksLikeSystemMessage(lastAssistant)) return { hasProblem: false }

  const lastUser = [...msgs].reverse().find(m => m.role === 'user')
  // The button must never appear before the human has actually asked
  // something — protects against the initial server-introduction message.
  if (!lastUser) return { hasProblem: false }

  const combined = `${lastUser.content || ''}\n${lastAssistant.content || ''}`

  const title = extractTitle(lastAssistant.content, lastUser?.content)
  const severity = pickFirstMatch(combined, SEVERITY_RULES, 'HIGH')
  const category = pickFirstMatch(combined, CATEGORY_RULES, 'Infrastructure')
  const actionType = pickFirstMatch(combined, ACTION_TYPE_RULES, 'SECURITY_FIX')
  const resource = extractResource(combined)
  const environment = extractEnvironment(combined)
  const steps = extractSteps(msgs)

  const description = [
    lastUser?.content ? `User reported: ${truncate(stripMarkdown(lastUser.content), 280)}` : '',
    `Assistant identified: ${truncate(stripMarkdown(lastAssistant.content), 600)}`,
    steps ? `Steps observed: ${truncate(steps, 200)}` : '',
    sessionId ? `Chat session: ${sessionId}${sessionTitle ? ` (${sessionTitle})` : ''}` : '',
    'Status: not yet resolved in chat — requires owner follow-up.',
  ].filter(Boolean).join('\n\n')

  return {
    hasProblem: true,
    title,
    description,
    severity,
    category,
    action_type: actionType,
    target_resource: resource,
    target_environment: environment,
    steps,
    session_id: sessionId || null,
  }
}

