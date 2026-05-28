import { describe, it, expect } from 'vitest'
import {
  MOCK_CONFLICTS,
  MOCK_CHANGE_REQUESTS,
  MOCK_AUDIT,
  countBySeverity,
  buildConflictMatrix,
  filterByRegulatory,
} from '../mockData'

// ─── Dataset completeness ────────────────────────────────────────────────────

describe('MOCK_CONFLICTS dataset', () => {
  it('contains all 12 ARBITER use cases', () => {
    expect(MOCK_CONFLICTS).toHaveLength(12)
  })

  it('every use case has a unique conflict_id', () => {
    const ids = MOCK_CONFLICTS.map(f => f.conflict_id)
    const unique = new Set(ids)
    expect(unique.size).toBe(12)
  })

  it('all UC IDs follow ARBITER-UC0N format', () => {
    MOCK_CONFLICTS.forEach(f => {
      expect(f.conflict_id).toMatch(/^ARBITER-UC\d{2}$/)
    })
  })

  it('UC01 through UC12 are all present', () => {
    for (let i = 1; i <= 12; i++) {
      const id = `ARBITER-UC${String(i).padStart(2, '0')}`
      expect(MOCK_CONFLICTS.find(f => f.conflict_id === id)).toBeDefined()
    }
  })

  it('every conflict has required fields', () => {
    const required = [
      'conflict_id', 'severity', 'type', 'title',
      'source_policy', 'source_technical', 'finding',
      'impact', 'remediation', 'domains', 'status', 'detected_at',
      'policy_mandates', 'regulatory',
    ]
    MOCK_CONFLICTS.forEach(f => {
      required.forEach(field => {
        expect(f, `${f.conflict_id} missing ${field}`).toHaveProperty(field)
      })
    })
  })

  it('severity values are one of CRITICAL|HIGH|MEDIUM|LOW', () => {
    const valid = new Set(['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'])
    MOCK_CONFLICTS.forEach(f => {
      expect(valid.has(f.severity), `${f.conflict_id} has invalid severity: ${f.severity}`).toBe(true)
    })
  })

  it('status values are OPEN or RESOLVED', () => {
    const valid = new Set(['OPEN', 'RESOLVED'])
    MOCK_CONFLICTS.forEach(f => {
      expect(valid.has(f.status)).toBe(true)
    })
  })

  it('type values are CROSS_DOMAIN or INTRA_DOCUMENT', () => {
    const valid = new Set(['CROSS_DOMAIN', 'INTRA_DOCUMENT'])
    MOCK_CONFLICTS.forEach(f => {
      expect(valid.has(f.type)).toBe(true)
    })
  })

  it('all 12 conflicts are OPEN (POC baseline state)', () => {
    MOCK_CONFLICTS.forEach(f => {
      expect(f.status).toBe('OPEN')
    })
  })

  it('all type values are CROSS_DOMAIN', () => {
    MOCK_CONFLICTS.forEach(f => {
      expect(f.type).toBe('CROSS_DOMAIN')
    })
  })

  it('detected_at is a valid ISO 8601 date string', () => {
    MOCK_CONFLICTS.forEach(f => {
      const d = new Date(f.detected_at)
      expect(isNaN(d.getTime()), `${f.conflict_id} has invalid detected_at`).toBe(false)
    })
  })

  it('every conflict has at least one domain', () => {
    MOCK_CONFLICTS.forEach(f => {
      expect(f.domains.length, `${f.conflict_id} has no domains`).toBeGreaterThan(0)
    })
  })

  it('every conflict has at least one remediation step', () => {
    MOCK_CONFLICTS.forEach(f => {
      expect(f.remediation.length, `${f.conflict_id} has no remediation steps`).toBeGreaterThan(0)
    })
  })

  it('policy_mandates reference MIG-POL documents', () => {
    MOCK_CONFLICTS.forEach(f => {
      f.policy_mandates.forEach(p => {
        expect(p, `${f.conflict_id} has non-MIG mandate: ${p}`).toMatch(/^MIG-POL-/)
      })
    })
  })
})

// ─── Severity distribution ───────────────────────────────────────────────────

describe('Severity distribution', () => {
  it('has 5 CRITICAL findings', () => {
    const count = MOCK_CONFLICTS.filter(f => f.severity === 'CRITICAL').length
    expect(count).toBe(5)
  })

  it('has 4 HIGH findings', () => {
    const count = MOCK_CONFLICTS.filter(f => f.severity === 'HIGH').length
    expect(count).toBe(4)
  })

  it('has 3 MEDIUM findings', () => {
    const count = MOCK_CONFLICTS.filter(f => f.severity === 'MEDIUM').length
    expect(count).toBe(3)
  })

  it('has 0 LOW findings', () => {
    const count = MOCK_CONFLICTS.filter(f => f.severity === 'LOW').length
    expect(count).toBe(0)
  })

  it('CRITICAL findings are UC04, UC05, UC07, UC08, UC09', () => {
    const critIds = MOCK_CONFLICTS.filter(f => f.severity === 'CRITICAL').map(f => f.conflict_id).sort()
    expect(critIds).toEqual(['ARBITER-UC04', 'ARBITER-UC05', 'ARBITER-UC07', 'ARBITER-UC08', 'ARBITER-UC09'])
  })

  it('HIGH findings are UC01, UC02, UC06, UC10', () => {
    const highIds = MOCK_CONFLICTS.filter(f => f.severity === 'HIGH').map(f => f.conflict_id).sort()
    expect(highIds).toEqual(['ARBITER-UC01', 'ARBITER-UC02', 'ARBITER-UC06', 'ARBITER-UC10'])
  })

  it('MEDIUM findings are UC03, UC11, UC12', () => {
    const medIds = MOCK_CONFLICTS.filter(f => f.severity === 'MEDIUM').map(f => f.conflict_id).sort()
    expect(medIds).toEqual(['ARBITER-UC03', 'ARBITER-UC11', 'ARBITER-UC12'])
  })
})

// ─── Domain coverage ─────────────────────────────────────────────────────────

describe('Domain coverage', () => {
  it('every domain is one of SharePoint|Zscaler|AWSConfig', () => {
    const valid = new Set(['SharePoint', 'Zscaler', 'AWSConfig'])
    MOCK_CONFLICTS.forEach(f => {
      f.domains.forEach(d => {
        expect(valid.has(d), `${f.conflict_id} has unknown domain: ${d}`).toBe(true)
      })
    })
  })

  it('UC07, UC08, UC09 involve AWSConfig', () => {
    ;['ARBITER-UC07', 'ARBITER-UC08', 'ARBITER-UC09'].forEach(id => {
      const f = MOCK_CONFLICTS.find(c => c.conflict_id === id)
      expect(f.domains).toContain('AWSConfig')
    })
  })

  it('UC01, UC02, UC03, UC04, UC05 involve Zscaler', () => {
    ;['ARBITER-UC01', 'ARBITER-UC02', 'ARBITER-UC03', 'ARBITER-UC04', 'ARBITER-UC05'].forEach(id => {
      const f = MOCK_CONFLICTS.find(c => c.conflict_id === id)
      expect(f.domains).toContain('Zscaler')
    })
  })

  it('all conflicts reference SharePoint (policy source)', () => {
    MOCK_CONFLICTS.forEach(f => {
      expect(f.domains, `${f.conflict_id} missing SharePoint`).toContain('SharePoint')
    })
  })
})

// ─── Regulatory references ───────────────────────────────────────────────────

describe('Regulatory references', () => {
  it('UC04 references PCI DSS', () => {
    const uc04 = MOCK_CONFLICTS.find(f => f.conflict_id === 'ARBITER-UC04')
    expect(uc04.regulatory.some(r => r.startsWith('PCI DSS'))).toBe(true)
  })

  it('UC05 references PCI DSS and NAIC MDL-668', () => {
    const uc05 = MOCK_CONFLICTS.find(f => f.conflict_id === 'ARBITER-UC05')
    expect(uc05.regulatory.some(r => r.startsWith('PCI DSS'))).toBe(true)
    expect(uc05.regulatory.some(r => r.startsWith('NAIC MDL-668'))).toBe(true)
  })

  it('UC09 references NAIC MDL-668', () => {
    const uc09 = MOCK_CONFLICTS.find(f => f.conflict_id === 'ARBITER-UC09')
    expect(uc09.regulatory.some(r => r.startsWith('NAIC MDL-668'))).toBe(true)
  })

  it('UC08 references PCI DSS', () => {
    const uc08 = MOCK_CONFLICTS.find(f => f.conflict_id === 'ARBITER-UC08')
    expect(uc08.regulatory.some(r => r.startsWith('PCI DSS'))).toBe(true)
  })
})

// ─── countBySeverity ─────────────────────────────────────────────────────────

describe('countBySeverity()', () => {
  it('returns correct counts for full dataset', () => {
    const result = countBySeverity(MOCK_CONFLICTS)
    expect(result.CRITICAL).toBe(5)
    expect(result.HIGH).toBe(4)
    expect(result.MEDIUM).toBe(3)
    expect(result.LOW).toBe(0)
  })

  it('returns all zeros for empty array', () => {
    const result = countBySeverity([])
    expect(result).toEqual({ CRITICAL: 0, HIGH: 0, MEDIUM: 0, LOW: 0 })
  })

  it('counts a single-item array correctly', () => {
    const result = countBySeverity([{ severity: 'CRITICAL' }])
    expect(result.CRITICAL).toBe(1)
    expect(result.HIGH).toBe(0)
  })

  it('ignores unknown severity values', () => {
    const result = countBySeverity([{ severity: 'BLOCKER' }, { severity: 'INFO' }])
    expect(result).toEqual({ CRITICAL: 0, HIGH: 0, MEDIUM: 0, LOW: 0 })
  })

  it('handles findings with undefined severity gracefully', () => {
    const result = countBySeverity([{ severity: undefined }, { severity: null }])
    expect(result).toEqual({ CRITICAL: 0, HIGH: 0, MEDIUM: 0, LOW: 0 })
  })
})

// ─── buildConflictMatrix ─────────────────────────────────────────────────────

describe('buildConflictMatrix()', () => {
  it('returns a matrix with all 3 domains', () => {
    const matrix = buildConflictMatrix(MOCK_CONFLICTS)
    expect(Object.keys(matrix)).toEqual(expect.arrayContaining(['SharePoint', 'Zscaler', 'AWSConfig']))
  })

  it('each domain has entries for all 4 severities', () => {
    const matrix = buildConflictMatrix(MOCK_CONFLICTS)
    ;['SharePoint', 'Zscaler', 'AWSConfig'].forEach(domain => {
      expect(matrix[domain]).toHaveProperty('CRITICAL')
      expect(matrix[domain]).toHaveProperty('HIGH')
      expect(matrix[domain]).toHaveProperty('MEDIUM')
      expect(matrix[domain]).toHaveProperty('LOW')
    })
  })

  it('AWSConfig has 3 CRITICAL entries (UC07, UC08, UC09)', () => {
    const matrix = buildConflictMatrix(MOCK_CONFLICTS)
    expect(matrix['AWSConfig'].CRITICAL).toBe(3)
  })

  it('Zscaler has HIGH entries (UC01, UC02, UC06, UC10)', () => {
    const matrix = buildConflictMatrix(MOCK_CONFLICTS)
    expect(matrix['Zscaler'].HIGH).toBeGreaterThanOrEqual(4)
  })

  it('returns zero-filled matrix for empty findings', () => {
    const matrix = buildConflictMatrix([])
    ;['SharePoint', 'Zscaler', 'AWSConfig'].forEach(domain => {
      ;['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'].forEach(sev => {
        expect(matrix[domain][sev]).toBe(0)
      })
    })
  })

  it('does not double-count a domain within one finding', () => {
    const finding = { severity: 'HIGH', domains: ['Zscaler', 'Zscaler'] }
    const matrix = buildConflictMatrix([finding])
    expect(matrix['Zscaler'].HIGH).toBe(1)
  })

  it('ignores unknown domains gracefully', () => {
    const finding = { severity: 'CRITICAL', domains: ['UnknownSystem'] }
    expect(() => buildConflictMatrix([finding])).not.toThrow()
  })

  it('handles findings with undefined domains array', () => {
    const finding = { severity: 'HIGH', domains: undefined }
    expect(() => buildConflictMatrix([finding])).not.toThrow()
  })

  it('handles findings with null severity', () => {
    const finding = { severity: null, domains: ['Zscaler'] }
    const matrix = buildConflictMatrix([finding])
    expect(matrix['Zscaler'].CRITICAL).toBe(0)
    expect(matrix['Zscaler'].HIGH).toBe(0)
  })
})

// ─── filterByRegulatory ──────────────────────────────────────────────────────

describe('filterByRegulatory()', () => {
  it('filters PCI DSS findings correctly', () => {
    const result = filterByRegulatory(MOCK_CONFLICTS, 'PCI DSS')
    expect(result.length).toBeGreaterThanOrEqual(3)
    result.forEach(f => {
      expect(f.regulatory.some(r => r.startsWith('PCI DSS'))).toBe(true)
    })
  })

  it('filters NAIC MDL-668 findings correctly', () => {
    const result = filterByRegulatory(MOCK_CONFLICTS, 'NAIC MDL-668')
    expect(result.length).toBeGreaterThanOrEqual(2)
    result.forEach(f => {
      expect(f.regulatory.some(r => r.startsWith('NAIC MDL-668'))).toBe(true)
    })
  })

  it('returns empty array for unknown framework', () => {
    const result = filterByRegulatory(MOCK_CONFLICTS, 'HIPAA')
    expect(result).toHaveLength(0)
  })

  it('returns empty array for empty findings', () => {
    const result = filterByRegulatory([], 'PCI DSS')
    expect(result).toHaveLength(0)
  })

  it('handles findings with empty regulatory array', () => {
    const finding = { regulatory: [] }
    const result = filterByRegulatory([finding], 'PCI DSS')
    expect(result).toHaveLength(0)
  })

  it('handles findings with undefined regulatory field', () => {
    const finding = { regulatory: undefined }
    expect(() => filterByRegulatory([finding], 'PCI DSS')).not.toThrow()
  })
})

// ─── MOCK_CHANGE_REQUESTS ────────────────────────────────────────────────────

describe('MOCK_CHANGE_REQUESTS dataset', () => {
  it('has at least 2 change requests', () => {
    expect(MOCK_CHANGE_REQUESTS.length).toBeGreaterThanOrEqual(2)
  })

  it('every CR has a unique cr_id', () => {
    const ids = MOCK_CHANGE_REQUESTS.map(cr => cr.cr_id)
    const unique = new Set(ids)
    expect(unique.size).toBe(ids.length)
  })

  it('every CR references an existing conflict_id', () => {
    const conflictIds = new Set(MOCK_CONFLICTS.map(f => f.conflict_id))
    MOCK_CHANGE_REQUESTS.forEach(cr => {
      expect(conflictIds.has(cr.conflict_id), `CR ${cr.cr_id} references unknown conflict ${cr.conflict_id}`).toBe(true)
    })
  })

  it('every CR has approvers array', () => {
    MOCK_CHANGE_REQUESTS.forEach(cr => {
      expect(Array.isArray(cr.approvers)).toBe(true)
      expect(cr.approvers.length).toBeGreaterThan(0)
    })
  })

  it('total_approvals_received does not exceed total_approvers_needed', () => {
    MOCK_CHANGE_REQUESTS.forEach(cr => {
      expect(cr.total_approvals_received).toBeLessThanOrEqual(cr.total_approvers_needed)
    })
  })

  it('CRs targeting PROD CRITICAL both require 2 approvers', () => {
    const prodCritical = MOCK_CHANGE_REQUESTS.filter(
      cr => cr.target_environment === 'PROD' && cr.severity === 'CRITICAL'
    )
    prodCritical.forEach(cr => {
      expect(cr.total_approvers_needed).toBe(2)
    })
  })

  it('approvers use meridianinsurance.com email domain', () => {
    MOCK_CHANGE_REQUESTS.forEach(cr => {
      cr.approvers.forEach(a => {
        expect(a.email).toMatch(/@meridianinsurance\.com$/)
      })
    })
  })
})

// ─── MOCK_AUDIT ──────────────────────────────────────────────────────────────

describe('MOCK_AUDIT dataset', () => {
  it('has at least 5 audit entries', () => {
    expect(MOCK_AUDIT.length).toBeGreaterThanOrEqual(5)
  })

  it('every audit entry has log_id, timestamp, action_type, user, status', () => {
    MOCK_AUDIT.forEach(entry => {
      expect(entry).toHaveProperty('log_id')
      expect(entry).toHaveProperty('timestamp')
      expect(entry).toHaveProperty('action_type')
      expect(entry).toHaveProperty('user')
      expect(entry).toHaveProperty('status')
    })
  })

  it('all timestamps are valid ISO dates', () => {
    MOCK_AUDIT.forEach(entry => {
      const d = new Date(entry.timestamp)
      expect(isNaN(d.getTime())).toBe(false)
    })
  })

  it('audit details field is valid JSON string when present', () => {
    MOCK_AUDIT.forEach(entry => {
      if (entry.details) {
        expect(() => JSON.parse(entry.details)).not.toThrow()
      }
    })
  })

  it('users reference meridianinsurance.com or system', () => {
    MOCK_AUDIT.forEach(entry => {
      const ok = entry.user === 'system' || entry.user.endsWith('@meridianinsurance.com')
      expect(ok, `Unexpected user: ${entry.user}`).toBe(true)
    })
  })
})
