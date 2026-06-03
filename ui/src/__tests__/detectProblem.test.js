import { describe, it, expect } from 'vitest'
import { detectProblem } from '../detectProblem'

const userMsg = (content) => ({ role: 'user', content })
const botMsg  = (content) => ({ role: 'assistant', content })

describe('detectProblem — trigger logic', () => {
  it('returns hasProblem=false on empty input', () => {
    expect(detectProblem({ messages: [] }).hasProblem).toBe(false)
    expect(detectProblem({}).hasProblem).toBe(false)
  })

  it('returns hasProblem=false when no assistant message present', () => {
    const out = detectProblem({ messages: [userMsg('hi')] })
    expect(out.hasProblem).toBe(false)
  })

  it('does not trigger on an initial greeting that mentions "conflict" by coincidence', () => {
    // Regression: the Conflict Detector MCP greeting and the AnalystView
    // greeting both contain the word "conflict" in their description. The
    // button must not appear before the user has typed anything.
    const greeting = botMsg(
      'Connected to **Conflict Detector MCP** (v1.8.3) at `mcp-conflict:8002`.\n\n' +
      'Cross-domain conflict detection engine. Correlates policies with technical configurations.\n\n' +
      'I have access to 5 tools.'
    )
    expect(detectProblem({ messages: [greeting] }).hasProblem).toBe(false)
  })

  it('does not trigger when the system flag is set on the assistant message', () => {
    const msgs = [
      userMsg('Walk me through the conflict.'),
      { role: 'assistant', system: true, content: 'CRITICAL: production isolation violation in sg-mig-prod.' },
    ]
    expect(detectProblem({ messages: msgs }).hasProblem).toBe(false)
  })

  it('does not trigger on a transport-error stub from the chat surface', () => {
    // Regression: MCPChat pushes `⚠️ Chat failed: ...` on sendChat errors,
    // and AnalystView pushes `⚠ Agent error: ...`. Both contain "failed"
    // which is a trigger word — but they are not real findings.
    const cases = [
      botMsg('⚠️ Chat failed: 503 Service Unavailable'),
      botMsg('⚠ Agent error: timeout'),
      botMsg('⚠ Could not load session: 404'),
    ]
    for (const errMsg of cases) {
      const out = detectProblem({ messages: [userMsg('Scan for conflicts'), errMsg] })
      expect(out.hasProblem).toBe(false)
    }
  })

  it('surfaces on a pure how-to with neutral answer (button shows for all questions)', () => {
    const out = detectProblem({
      messages: [
        userMsg('How do I list AWS security groups?'),
        botMsg('Use the AWS CLI: aws ec2 describe-security-groups.'),
      ],
    })
    expect(out.hasProblem).toBe(true)
  })

  it('triggers on a violation finding from the assistant', () => {
    const out = detectProblem({
      messages: [
        userMsg('Check the VPC peering between dev and prod.'),
        botMsg('CRITICAL: sg-mig-prod-peer-dev-001 violates MIG-POL-004-SEG01 §2.3 — production isolation breach.'),
      ],
      sessionId: 'sess-abc123',
    })
    expect(out.hasProblem).toBe(true)
    expect(out.severity).toBe('CRITICAL')
    expect(out.session_id).toBe('sess-abc123')
  })

  it('triggers when the user explicitly asks for a fix', () => {
    const out = detectProblem({
      messages: [
        userMsg('Please remediate the SSL inspection issue on financial domains.'),
        botMsg('Acknowledged. The fix is to remove the bypass rule and re-enable inspection.'),
      ],
    })
    expect(out.hasProblem).toBe(true)
  })
})

describe('detectProblem — field extraction', () => {
  it('extracts a security group as target_resource', () => {
    const out = detectProblem({
      messages: [
        userMsg('What is happening with sg-mig-prod-peer-dev-001?'),
        botMsg('It allows ALL traffic from 10.50.0.0/16 — a critical production isolation violation.'),
      ],
    })
    expect(out.hasProblem).toBe(true)
    expect(out.target_resource).toBe('sg-mig-prod-peer-dev-001')
  })

  it('extracts a MIG policy ID as target_resource when no resource is present', () => {
    const out = detectProblem({
      messages: [
        userMsg('Review MIG-POL-002-MFA01.'),
        botMsg('MIG-POL-002-MFA01 §2.1 is non-compliant: MFA only enforced for admins, policy requires ALL users.'),
      ],
    })
    expect(out.hasProblem).toBe(true)
    expect(out.target_resource).toBe('MIG-POL-002-MFA01')
  })

  it('defaults target_environment to PROD when no env keyword present', () => {
    const out = detectProblem({
      messages: [
        userMsg('Investigate the WAF gap.'),
        botMsg('Critical: ALB exposed without WAF — non-compliant.'),
      ],
    })
    expect(out.target_environment).toBe('PROD')
  })

  it('picks STAGING when the conversation references staging only', () => {
    const out = detectProblem({
      messages: [
        userMsg('There is a misconfiguration in our staging environment.'),
        botMsg('Confirmed: staging IAM role exposed via failed boundary policy.'),
      ],
    })
    expect(out.target_environment).toBe('STAGING')
  })

  it('infers Security category for MFA/SSL issues', () => {
    const out = detectProblem({
      messages: [
        userMsg('Why is the SSL bypass still active?'),
        botMsg('SSL inspection bypass on financial domains is a PCI DSS violation.'),
      ],
    })
    expect(out.category).toBe('Security')
  })

  it('infers Infrastructure category for VPC / S3 issues', () => {
    const out = detectProblem({
      messages: [
        userMsg('Check the S3 replication on mig-prod-claims-data-primary.'),
        botMsg('Replication to eu-west-1 violates the US-only data residency rule.'),
      ],
    })
    expect(out.category).toBe('Infrastructure')
  })

  it('produces a non-empty title and description', () => {
    const out = detectProblem({
      messages: [
        userMsg('Run a conflict scan.'),
        botMsg('**Production ALB exposed without WAF** — MIG-POL-004-WAF01 §2.1 violation.'),
      ],
      sessionId: 'sess-1',
      sessionTitle: 'WAF gap review',
    })
    expect(out.title).toMatch(/Production ALB exposed without WAF/)
    expect(out.description).toContain('Chat session: sess-1')
    expect(out.description).toContain('WAF gap review')
  })
})

describe('detectProblem — title heuristic', () => {
  it('skips a leading "Summary" section label and uses the next real sentence', () => {
    const out = detectProblem({
      messages: [
        userMsg('Is it safe to remove the Dropbox block in Zscaler?'),
        botMsg(
          'Summary\nZscaler policy configuration for Dropbox blocking is not visible in available sources.\n\n' +
          'Findings\n- Zscaler knowledge base does not contain explicit Dropbox block policies'
        ),
      ],
    })
    expect(out.hasProblem).toBe(true)
    expect(out.title.toLowerCase()).not.toBe('summary')
    expect(out.title.toLowerCase()).not.toBe('findings')
    expect(out.title).toMatch(/Zscaler/)
  })

  it('falls back to the user question when the assistant says it cannot answer', () => {
    const out = detectProblem({
      messages: [
        userMsg('Is it safe to remove the Dropbox block in Zscaler? What approvals do I need?'),
        botMsg(
          'The available tools cannot determine whether it is safe to remove the Dropbox block in Zscaler or the required approvals. ' +
          'Dropbox blocking may be implemented via URL categories.'
        ),
      ],
    })
    expect(out.hasProblem).toBe(true)
    expect(out.title.toLowerCase()).not.toContain('available tools cannot')
    expect(out.title).toMatch(/Dropbox/)
  })
})

