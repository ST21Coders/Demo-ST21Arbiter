import { describe, it, expect } from 'vitest'
import { presignUpload } from '../hooks/useApi'

// Tests run in mock mode (VITE_API_URL unset → USE_MOCK true). presignUpload's
// mock branch mirrors the destination routing the real /uploads/presign performs:
// destination=unstructured → policy-KB bucket; anything else → raw bucket.
describe('presignUpload — destination routing', () => {
  it('routes destination=unstructured to the unstructured bucket', async () => {
    const r = await presignUpload({ filename: 'acceptable-use.pdf', contentType: 'application/pdf', destination: 'unstructured' })
    expect(r.bucket).toBe('mock-unstructured')
    expect(r.key).toMatch(/acceptable-use\.pdf$/)
    expect(r.method).toBe('PUT')
  })

  it('defaults to the raw bucket when destination is omitted', async () => {
    const r = await presignUpload({ filename: 'data.csv', contentType: 'text/csv' })
    expect(r.bucket).toBe('mock')
  })

  it('routes destination=raw to the raw bucket', async () => {
    const r = await presignUpload({ filename: 'data.csv', contentType: 'text/csv', destination: 'raw' })
    expect(r.bucket).toBe('mock')
  })
})
