const CSV_PATTERN = /\.csv$/i

export function dataGroupContentType(group) {
  const files = Array.isArray(group?.files) ? group.files : []
  const csvCount = files.filter(file => CSV_PATTERN.test(file?.name || file?.key || '')).length
  const documentCount = files.length - csvCount

  if (csvCount && documentCount) return 'mixed'
  if (csvCount) return 'structured'
  return 'documents'
}

export function dataGroupChatTarget(group) {
  const contentType = dataGroupContentType(group)
  if (contentType === 'structured') return 'structured'
  if (contentType === 'documents') return 'sharepoint'
  return 'master'
}

export function isDataGroupChatTarget(target) {
  return ['master', 'sharepoint', 'structured'].includes(target)
}

export function isStructuredEvidenceQuestion(question) {
  const text = String(question || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ')
  const signals = [
    'invoice', 'benchmark', 'weather', 'siu', 'claim id', 'policy id',
    'athena', 'table', 'column', 'row count', 'join', 'aggregate',
  ]
  return signals.filter(signal => text.includes(signal)).length >= 2
}
