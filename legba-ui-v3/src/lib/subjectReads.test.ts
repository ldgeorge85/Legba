import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchSubjectReads, fromFullRow } from './subjectReads'
import { verdictsFromReads } from '@/v4/world/countryVerdicts'
import { mockErrorResponse } from '@/test/apiMocks'

afterEach(() => vi.unstubAllGlobals())

describe('subject reads at verdict weight', () => {
  it('reads the light route, and falls back to the full read when the registry does not serve it', async () => {
    const light = { available: true, data: [{ id: 'f1', title: 't', target_id: 'country_g20_br', analyst_id: 'country_composition', produced_at: '2026-11-13T00:00:00Z', confidence: 0.8, effective_confidence: 0.6, verification: { faithfulness_score: 0.9 }, citation_count: 3 }] }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => light }))
    const a = await fetchSubjectReads('country_composition')
    expect(a.data[0].citation_count).toBe(3)
    expect(verdictsFromReads(a).get('BR')?.targetId).toBe('country_g20_br')

    const full = { data: [{ id: 'f2', target_id: 'country_g20_br', produced_at: '2026-11-13T00:00:00Z', confidence: 0.7, data: { data: { citations: [1, 2] }, verification: { faithfulness_score: 0.5 } } }] }
    vi.stubGlobal('fetch', vi.fn((url: string) => Promise.resolve(url.includes('/v3/map/subject_reads') ? mockErrorResponse(404, { detail: 'Not Found' }) : { ok: true, json: async () => full })))
    const b = await fetchSubjectReads('country_comp_fallback')
    expect(b.data[0]).toMatchObject({ id: 'f2', citation_count: 2, verification: { faithfulness_score: 0.5 } })
    expect(fromFullRow(full.data[0]).effective_confidence).toBeNull()
  })
})
