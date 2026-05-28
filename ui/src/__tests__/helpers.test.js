import { describe, it, expect } from 'vitest'
import {
  MOCK_CONFLICTS,
  countBySeverity,
  buildConflictMatrix,
  filterByRegulatory,
} from '../mockData'

// ─── countBySeverity — comprehensive boundary tests ─────────────────────────

describe('countBySeverity — boundary and combinatorial cases', () => {
  it('handles mixed valid and invalid severities', () => {
    const findings = [
      { severity: 'CRITICAL' },
      { severity: 'BLOCKER' },
      { severity: 'HIGH' },
      { severity: undefined },
    ]
    const result = countBySeverity(findings)
    expect(result.CRITICAL).toBe(1)
    expect(result.HIGH).toBe(1)
    expect(result.MEDIUM).toBe(0)
    expect(result.LOW).toBe(0)
  })

  it('counts multiple of the same severity correctly', () => {
    const findings = Array.from({ length: 7 }, () => ({ severity: 'CRITICAL' }))
    expect(countBySeverity(findings).CRITICAL).toBe(7)
  })

  it('total count equals input length when all severities are valid', () => {
    const allValid = MOCK_CONFLICTS
    const counts = countBySeverity(allValid)
    const total = counts.CRITICAL + counts.HIGH + counts.MEDIUM + counts.LOW
    expect(total).toBe(allValid.length)
  })
})

// ─── buildConflictMatrix — multi-domain conflicts ────────────────────────────

describe('buildConflictMatrix — multi-domain conflicts', () => {
  it('counts one finding with two different domains separately', () => {
    const finding = { severity: 'CRITICAL', domains: ['SharePoint', 'AWSConfig'] }
    const matrix = buildConflictMatrix([finding])
    expect(matrix['SharePoint'].CRITICAL).toBe(1)
    expect(matrix['AWSConfig'].CRITICAL).toBe(1)
    expect(matrix['Zscaler'].CRITICAL).toBe(0)
  })

  it('a finding spanning all 3 domains counts in each domain column', () => {
    const finding = { severity: 'HIGH', domains: ['SharePoint', 'Zscaler', 'AWSConfig'] }
    const matrix = buildConflictMatrix([finding])
    expect(matrix['SharePoint'].HIGH).toBe(1)
    expect(matrix['Zscaler'].HIGH).toBe(1)
    expect(matrix['AWSConfig'].HIGH).toBe(1)
  })

  it('matrix ALL values are non-negative integers', () => {
    const matrix = buildConflictMatrix(MOCK_CONFLICTS)
    ;['SharePoint', 'Zscaler', 'AWSConfig'].forEach(d => {
      ;['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'].forEach(s => {
        expect(matrix[d][s]).toBeGreaterThanOrEqual(0)
        expect(Number.isInteger(matrix[d][s])).toBe(true)
      })
    })
  })

  it('SharePoint has the highest total because all conflicts reference it', () => {
    const matrix = buildConflictMatrix(MOCK_CONFLICTS)
    const spTotal = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'].reduce((s, k) => s + matrix['SharePoint'][k], 0)
    const awsTotal = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'].reduce((s, k) => s + matrix['AWSConfig'][k], 0)
    expect(spTotal).toBeGreaterThanOrEqual(awsTotal)
  })
})

// ─── filterByRegulatory — multi-framework matches ────────────────────────────

describe('filterByRegulatory — multi-framework', () => {
  it('PCI DSS filter returns only PCI DSS-tagged findings', () => {
    const result = filterByRegulatory(MOCK_CONFLICTS, 'PCI DSS')
    result.forEach(f => {
      expect(f.regulatory.some(r => r.startsWith('PCI DSS'))).toBe(true)
    })
  })

  it('NAIC MDL-668 includes UC05, UC09, UC10', () => {
    const result = filterByRegulatory(MOCK_CONFLICTS, 'NAIC MDL-668')
    const ids = result.map(f => f.conflict_id)
    expect(ids).toContain('ARBITER-UC05')
    expect(ids).toContain('ARBITER-UC09')
    expect(ids).toContain('ARBITER-UC10')
  })

  it('does not modify the findings array', () => {
    const before = MOCK_CONFLICTS.length
    filterByRegulatory(MOCK_CONFLICTS, 'PCI DSS')
    expect(MOCK_CONFLICTS.length).toBe(before)
  })
})

// ─── Data integrity cross-checks ─────────────────────────────────────────────

describe('Cross-check data integrity', () => {
  it('matrix CRITICAL + HIGH + MEDIUM + LOW for Zscaler matches individual filter counts', () => {
    const matrix = buildConflictMatrix(MOCK_CONFLICTS)
    const zscalerFindings = MOCK_CONFLICTS.filter(f => f.domains?.includes('Zscaler'))
    const counts = countBySeverity(zscalerFindings)
    expect(matrix['Zscaler'].CRITICAL).toBe(counts.CRITICAL)
    expect(matrix['Zscaler'].HIGH).toBe(counts.HIGH)
    expect(matrix['Zscaler'].MEDIUM).toBe(counts.MEDIUM)
    expect(matrix['Zscaler'].LOW).toBe(counts.LOW)
  })

  it('matrix CRITICAL for AWSConfig matches individual filter count', () => {
    const matrix = buildConflictMatrix(MOCK_CONFLICTS)
    const awsCritical = MOCK_CONFLICTS.filter(
      f => f.domains?.includes('AWSConfig') && f.severity === 'CRITICAL'
    ).length
    expect(matrix['AWSConfig'].CRITICAL).toBe(awsCritical)
  })

  it('regulatory filter count for PCI DSS is consistent with manual scan', () => {
    const manual = MOCK_CONFLICTS.filter(f =>
      f.regulatory?.some(r => r.startsWith('PCI DSS'))
    )
    const filtered = filterByRegulatory(MOCK_CONFLICTS, 'PCI DSS')
    expect(filtered.length).toBe(manual.length)
  })

  it('all CRITICAL conflicts appear in countBySeverity result', () => {
    const counts = countBySeverity(MOCK_CONFLICTS)
    const criticalFromFilter = MOCK_CONFLICTS.filter(f => f.severity === 'CRITICAL').length
    expect(counts.CRITICAL).toBe(criticalFromFilter)
  })
})
