import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

// DataPipeline pulls the whole useApi surface at import time. Stub every function
// it uses so the page can mount without touching the network; listDataGroupingProjects
// is the only one called on mount (refreshGroups), so it must resolve a shape.
vi.mock('../hooks/useApi', () => ({
  getUploadStatus: vi.fn().mockResolvedValue({ raw: { exists: false }, processed: { exists: false } }),
  listDataGroupingProjects: vi.fn().mockResolvedValue({ groups: [] }),
  listScanRuns: vi.fn().mockResolvedValue({ scan_runs: [] }),
  materializeDataGroupingProject: vi.fn().mockResolvedValue({}),
  presignUpload: vi.fn().mockResolvedValue({ url: '#mock', key: 'users/mock/x', bucket: 'mock', headers: {} }),
  triggerDataIngest: vi.fn().mockResolvedValue({}),
  uploadToPresignedUrl: vi.fn().mockResolvedValue({ ok: true, status: 200 }),
}))

import DataPipeline, {
  uploadDestinationForMix,
  POLICY_DOC_FILE_MIXES,
  isPolicyDocUpload,
  stepDefsFor,
  stepStatesFor,
} from '../pages/DataPipeline'

// ── point 1: which group-content mixes are routed to the unstructured bucket ──
describe('uploadDestinationForMix — policy-doc bucket routing', () => {
  it('routes the 3 policy-document mixes to the unstructured bucket', () => {
    expect(uploadDestinationForMix('text_only')).toBe('unstructured')
    expect(uploadDestinationForMix('csv_text')).toBe('unstructured')
    expect(uploadDestinationForMix('csv_text_media')).toBe('unstructured')
  })

  it('leaves every other mix on the raw-bucket flow (point 3)', () => {
    // csv_only → Glue; unstructured_vector / structured_vector_glue → S3 Vectors worker.
    expect(uploadDestinationForMix('csv_only')).toBe('')
    expect(uploadDestinationForMix('unstructured_vector')).toBe('')
    expect(uploadDestinationForMix('structured_vector_glue')).toBe('')
  })

  it('defaults to the raw flow for empty/unknown mixes', () => {
    expect(uploadDestinationForMix('')).toBe('')
    expect(uploadDestinationForMix(undefined)).toBe('')
    expect(uploadDestinationForMix('nonsense')).toBe('')
  })

  it('POLICY_DOC_FILE_MIXES holds exactly the 3 policy mixes', () => {
    expect([...POLICY_DOC_FILE_MIXES].sort()).toEqual(['csv_text', 'csv_text_media', 'text_only'])
  })
})

// ── the stuck-in-progress fix: policy-doc live row status ─────────────────────
describe('policy-doc upload — live step status (unstructured bucket)', () => {
  const policyDoc = extra => ({ destination: 'unstructured', filename: 'HR-BEN-002_benefits.pdf', ...extra })

  it('uses the Raw → Unstructured → KB ingest → Scan step defs', () => {
    expect(stepDefsFor(policyDoc()).map(s => s.key)).toEqual(['raw', 'unstructured', 'kb', 'scan'])
    // even a .csv in a policy mix follows the KB path, not the Glue/structured path
    expect(isPolicyDocUpload(policyDoc({ filename: 'roster.csv' }))).toBe(true)
    expect(stepDefsFor(policyDoc({ filename: 'roster.csv' })).map(s => s.key))
      .toEqual(['raw', 'unstructured', 'kb', 'scan'])
  })

  it('marks Raw + Unstructured done once uploaded, KB running until the scan-run lands', () => {
    const s = stepStatesFor(policyDoc({ state: 'uploaded', scanRun: null }))
    expect(s.raw).toBe('done')
    expect(s.unstructured).toBe('done')
    expect(s.kb).toBe('running')   // waiting on the auto-trigger — NOT stuck on "processed"
    expect(s.scan).toBe('pending')
    expect(s.processed).toBeUndefined()
  })

  it('completes all four steps when the auto-ingest scan-run COMPLETES', () => {
    const s = stepStatesFor(policyDoc({ state: 'uploaded', scanRun: { status: 'COMPLETED' } }))
    expect([s.raw, s.unstructured, s.kb, s.scan]).toEqual(['done', 'done', 'done', 'done'])
  })

  it('surfaces a failed scan-run on the Scan step', () => {
    const s = stepStatesFor(policyDoc({ state: 'uploaded', scanRun: { status: 'FAILED' } }))
    expect(s.kb).toBe('done')
    expect(s.scan).toBe('failed')
  })

  it('shows Unstructured failed when the upload PUT fails', () => {
    const s = stepStatesFor(policyDoc({ state: 'upload_failed' }))
    expect(s.raw).toBe('done')
    expect(s.unstructured).toBe('failed')
  })
})

// ── point 2: the displayed Policy Documents process ───────────────────────────
describe('Data Pipeline — Policy Documents path card', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  function renderPage() {
    return render(
      <MemoryRouter initialEntries={['/data-pipeline']}>
        <DataPipeline />
      </MemoryRouter>,
    )
  }

  it('shows Raw → Unstructured → KB ingest → Scan and targets the new KB', async () => {
    renderPage()
    await screen.findByText('Policy Documents')

    // Step 2 is now Unstructured (was Processed); the KB step names the S3-Vectors KB.
    expect(screen.getByText('Unstructured')).toBeInTheDocument()
    expect(screen.getByText(/Stored in the .*-unstructured S3 bucket/)).toBeInTheDocument()
    expect(screen.getByText(/SQCLG3W09Y \/ NM2FVXL5T6/)).toBeInTheDocument()

    // The card subtitle advertises the unstructured-bucket → S3-Vectors KB route.
    expect(screen.getByText(/unstructured bucket → S3-Vectors KB/)).toBeInTheDocument()
  })
})
