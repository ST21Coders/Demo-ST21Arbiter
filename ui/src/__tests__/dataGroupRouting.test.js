import { describe, expect, it } from 'vitest'
import { dataGroupChatTarget, dataGroupContentType, isDataGroupChatTarget, isStructuredEvidenceQuestion } from '../dataGroupRouting'

describe('Data Group routing', () => {
  it('routes document groups to the KB-backed SharePoint specialist', () => {
    const group = { files: [{ name: 'policy.pdf' }, { name: 'requirements.docx' }] }
    expect(dataGroupContentType(group)).toBe('documents')
    expect(dataGroupChatTarget(group)).toBe('sharepoint')
  })

  it('routes CSV-only groups to the structured specialist', () => {
    const group = { files: [{ name: 'controls.csv' }, { key: 'inventory.CSV' }] }
    expect(dataGroupContentType(group)).toBe('structured')
    expect(dataGroupChatTarget(group)).toBe('structured')
  })

  it('routes mixed groups through the master orchestrator', () => {
    const group = { files: [{ name: 'controls.csv' }, { name: 'policy.pdf' }] }
    expect(dataGroupContentType(group)).toBe('mixed')
    expect(dataGroupChatTarget(group)).toBe('master')
  })

  it('recognizes every target that can retain Data Group scope', () => {
    expect(['master', 'sharepoint', 'structured'].every(isDataGroupChatTarget)).toBe(true)
    expect(isDataGroupChatTarget('jira')).toBe(false)
  })

  it('recognizes structured evidence questions inside mixed groups', () => {
    expect(isStructuredEvidenceQuestion('Show claim ID, invoice total, weather match, and SIU score')).toBe(true)
    expect(isStructuredEvidenceQuestion('Compare these two policy documents')).toBe(false)
  })
})
