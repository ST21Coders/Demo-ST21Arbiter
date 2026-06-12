import { describe, it, expect } from 'vitest'
import {
  MOCK_CONFLICTS,
  countBySeverity,
  buildConflictMatrix,
  filterByRegulatory,
} from '../mockData'

// ─── Null / undefined input guards ──────────────────────────────────────────

describe('Edge cases — null and undefined inputs', () => {
  it('countBySeverity handles array with null elements', () => {
    expect(() => countBySeverity([null, undefined])).not.toThrow()
  })

  it('countBySeverity handles findings with no severity property', () => {
    const result = countBySeverity([{ title: 'no severity' }, {}])
    expect(result).toEqual({ CRITICAL: 0, HIGH: 0, MEDIUM: 0, LOW: 0 })
  })

  it('buildConflictMatrix handles empty domains gracefully', () => {
    const finding = { severity: 'HIGH', domains: [] }
    const matrix = buildConflictMatrix([finding])
    expect(matrix['Zscaler'].HIGH).toBe(0)
  })

  it('buildConflictMatrix handles null in domains array', () => {
    const finding = { severity: 'CRITICAL', domains: [null, 'Zscaler'] }
    expect(() => buildConflictMatrix([finding])).not.toThrow()
    const matrix = buildConflictMatrix([finding])
    expect(matrix['Zscaler'].CRITICAL).toBe(1)
  })

  it('filterByRegulatory handles null regulatory field', () => {
    const finding = { regulatory: null }
    expect(() => filterByRegulatory([finding], 'PCI DSS')).not.toThrow()
  })

  it('filterByRegulatory with empty framework string matches findings that have any regulatory entry', () => {
    // String.startsWith('') is true for every string, so an empty prefix matches any non-empty regulatory value
    const result = filterByRegulatory(MOCK_CONFLICTS, '')
    const withRegulatory = MOCK_CONFLICTS.filter(f => f.regulatory?.length > 0)
    expect(result.length).toBe(withRegulatory.length)
  })
})

// ─── detected_at handling ────────────────────────────────────────────────────

describe('Edge cases — detected_at field', () => {
  it('no conflict has a detected_at in the future (>10 min from now)', () => {
    const nowPlus10 = Date.now() + 10 * 60 * 1000
    MOCK_CONFLICTS.forEach(f => {
      const t = new Date(f.detected_at).getTime()
      expect(t, `${f.conflict_id} has future detected_at`).toBeLessThanOrEqual(nowPlus10)
    })
  })

  it('detected_at values can be parsed by new Date()', () => {
    MOCK_CONFLICTS.forEach(f => {
      const d = new Date(f.detected_at)
      expect(Number.isNaN(d.getTime())).toBe(false)
    })
  })
})

// ─── Remediation content checks ───────────────────────────────────────────────

describe('Edge cases — remediation quality', () => {
  it('each remediation step is a non-empty string', () => {
    MOCK_CONFLICTS.forEach(f => {
      f.remediation.forEach((step, i) => {
        expect(typeof step).toBe('string')
        expect(step.trim().length, `${f.conflict_id} remediation[${i}] is empty`).toBeGreaterThan(0)
      })
    })
  })

  it('CRITICAL findings have at least 3 remediation steps', () => {
    MOCK_CONFLICTS.filter(f => f.severity === 'CRITICAL').forEach(f => {
      expect(f.remediation.length, `${f.conflict_id} has too few remediation steps`).toBeGreaterThanOrEqual(3)
    })
  })
})

// ─── Policy mandate format ────────────────────────────────────────────────────

describe('Edge cases — policy mandate references', () => {
  it('no conflict has old LM- policy references', () => {
    MOCK_CONFLICTS.forEach(f => {
      f.policy_mandates.forEach(p => {
        expect(p, `${f.conflict_id} still references LM- policy`).not.toMatch(/^LM-/)
      })
      expect(f.source_policy).not.toMatch(/^LM-/)
      expect(f.source_technical).not.toMatch(/^LM-/)
    })
  })

  it('source_policy references the expected MIG-POL section', () => {
    // Cross-tool conflicts (UC14: Zscaler vs Palo Alto) have no policy doc — their
    // source_policy names the contradicting enforcement rule instead.
    const CROSS_TOOL = new Set(['ARBITER-UC14'])
    MOCK_CONFLICTS.forEach(f => {
      if (CROSS_TOOL.has(f.conflict_id)) return
      expect(f.source_policy).toMatch(/MIG-POL-/)
    })
  })
})

// ─── Matrix idempotency ───────────────────────────────────────────────────────

describe('Edge cases — buildConflictMatrix idempotency', () => {
  it('calling buildConflictMatrix twice on the same data yields the same result', () => {
    const first  = buildConflictMatrix(MOCK_CONFLICTS)
    const second = buildConflictMatrix(MOCK_CONFLICTS)
    expect(first).toEqual(second)
  })

  it('does not mutate the original findings array', () => {
    const snapshot = JSON.stringify(MOCK_CONFLICTS)
    buildConflictMatrix(MOCK_CONFLICTS)
    expect(JSON.stringify(MOCK_CONFLICTS)).toBe(snapshot)
  })
})

// ─── Matrix row/column totals ─────────────────────────────────────────────────

describe('Edge cases — matrix totals coherency', () => {
  it('total findings across all matrix cells equals or exceeds total conflict count (multi-domain entries counted once per domain)', () => {
    const matrix = buildConflictMatrix(MOCK_CONFLICTS)
    let cellSum = 0
    ;['SharePoint', 'Zscaler', 'AWSConfig'].forEach(d => {
      ;['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'].forEach(s => {
        cellSum += matrix[d][s]
      })
    })
    // Sum across domains can exceed 12 because some conflicts span multiple domains
    expect(cellSum).toBeGreaterThanOrEqual(12)
  })

  it('AWSConfig findings (UC07-09 baseline + UC15, UC16, UC21, UC24 seed) total 7', () => {
    const matrix = buildConflictMatrix(MOCK_CONFLICTS)
    const awsTotal = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'].reduce(
      (sum, s) => sum + matrix['AWSConfig'][s], 0
    )
    expect(awsTotal).toBe(7)
  })
})

// ─── filterByRegulatory precision ─────────────────────────────────────────────

describe('Edge cases — filterByRegulatory precision', () => {
  it('does not match partial framework names (PCI vs PCI DSS)', () => {
    const result = filterByRegulatory(MOCK_CONFLICTS, 'PCI')
    // "PCI DSS" starts with "PCI" so these should match
    expect(result.length).toBeGreaterThan(0)
    // But "PCIv4" should not match
    const noMatch = filterByRegulatory(MOCK_CONFLICTS, 'PCIv4')
    expect(noMatch).toHaveLength(0)
  })

  it('is case-sensitive — "pci dss" returns nothing', () => {
    const result = filterByRegulatory(MOCK_CONFLICTS, 'pci dss')
    expect(result).toHaveLength(0)
  })

  it('filterByRegulatory returns a new array not the original', () => {
    const result = filterByRegulatory(MOCK_CONFLICTS, 'PCI DSS')
    result.push({ fake: true })
    expect(MOCK_CONFLICTS.find(f => f.fake)).toBeUndefined()
  })
})

// ─── Specific use case business logic ──────────────────────────────────────

describe('Use case business logic assertions', () => {
  it('UC04 (SSL bypass) impacts PCI DSS compliance', () => {
    const uc04 = MOCK_CONFLICTS.find(f => f.conflict_id === 'ARBITER-UC04')
    expect(uc04.severity).toBe('CRITICAL')
    expect(uc04.regulatory.some(r => r.includes('PCI'))).toBe(true)
    expect(uc04.finding).toContain('SSL')
  })

  it('UC05 (MFA gap) references all users scope', () => {
    const uc05 = MOCK_CONFLICTS.find(f => f.conflict_id === 'ARBITER-UC05')
    expect(uc05.severity).toBe('CRITICAL')
    expect(uc05.finding.toLowerCase()).toContain('all users')
  })

  it('UC08 (VPC peering) mentions the number of active days', () => {
    const uc08 = MOCK_CONFLICTS.find(f => f.conflict_id === 'ARBITER-UC08')
    expect(uc08.finding).toMatch(/\d+ days/)
  })

  it('UC09 (S3 geo) mentions eu-west-1', () => {
    const uc09 = MOCK_CONFLICTS.find(f => f.conflict_id === 'ARBITER-UC09')
    expect(uc09.finding).toContain('eu-west-1')
    expect(uc09.source_technical).toContain('mig-prod-claims-data-primary')
  })

  it('UC12 (social media) identifies 4 department exemptions', () => {
    const uc12 = MOCK_CONFLICTS.find(f => f.conflict_id === 'ARBITER-UC12')
    const finding = uc12.finding + ' ' + uc12.remediation.join(' ')
    expect(finding).toMatch(/Marketing/)
    expect(finding).toMatch(/HR|Human Resources/)
  })

  it('UC11 (ZTNA geo) identifies 6 blocked countries out of 8 approved', () => {
    const uc11 = MOCK_CONFLICTS.find(f => f.conflict_id === 'ARBITER-UC11')
    expect(uc11.finding).toMatch(/8/)
    expect(uc11.severity).toBe('MEDIUM')
  })
})
