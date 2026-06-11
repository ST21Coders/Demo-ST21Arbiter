import { describe, expect, it } from 'vitest'
import { MOCK_CONFLICTS } from '../mockData'
import {
  SCORE_TREND_POINTS,
  filterScoreTrend,
  frameworkSummaries,
} from '../lib/governanceScoring'

describe('governance score cards', () => {
  it('matches the approved framework score-card set', () => {
    const summaries = frameworkSummaries(MOCK_CONFLICTS)
    expect(summaries.map(s => s.name)).toEqual([
      'NAIC MDL-668',
      'PCI-DSS 4.0',
      'SOX',
      'NIST CSF 2.0',
      'ISO 27001:2022',
    ])
    expect(summaries.map(s => s.score)).toEqual([65, 73, 74, 63, 63])
  })

  it('derives the approved open and severity counts', () => {
    const byId = Object.fromEntries(frameworkSummaries(MOCK_CONFLICTS).map(s => [s.id, s]))
    expect(byId.naic).toMatchObject({ openCount: 7, criticalCount: 3, highCount: 3, mediumCount: 1 })
    expect(byId['pci-dss']).toMatchObject({ openCount: 6, criticalCount: 2, highCount: 3, mediumCount: 1 })
    expect(byId.sox).toMatchObject({ openCount: 4, criticalCount: 3, highCount: 0, mediumCount: 1 })
    expect(byId.nist).toMatchObject({ openCount: 8, criticalCount: 3, highCount: 3, mediumCount: 2 })
    expect(byId.iso27001).toMatchObject({ openCount: 8, criticalCount: 3, highCount: 3, mediumCount: 2 })
  })

  it('filters trend ranges without dropping the latest point', () => {
    const sixMonth = filterScoreTrend(SCORE_TREND_POINTS, '6M')
    expect(sixMonth).toHaveLength(7)
    expect(sixMonth.at(-1)).toMatchObject({
      month: '2026-06',
      naic: 65,
      'pci-dss': 73,
      sox: 74,
      nist: 63,
      iso27001: 63,
    })
  })
})
