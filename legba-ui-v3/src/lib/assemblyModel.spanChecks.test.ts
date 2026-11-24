/**
 * Lane N-D — the span checks and the honest quote-fidelity count.
 *
 * `blocks[].span_checks` (server: `assembly_span_checks`) and the
 * `assembly_quote_sha_verified` counter (server: `assembly_arms`) both reach
 * the page through `projectAssembly` / `projectArms`; these tests pin the
 * projection, including the fallback for rows written before the counter.
 */
import { describe, expect, it } from 'vitest'
import worldRow from '@/v4/read/__fixtures__/assembly.world.json'
import {
  projectArms,
  projectAssembly,
  projectSpanChecks,
  recordBadge,
  type ReadFindingRow,
} from './assemblyModel'

const world = worldRow as unknown as ReadFindingRow

function withCounters(counters: Record<string, number>): ReadFindingRow {
  return {
    ...(world as unknown as Record<string, unknown>),
    verification: {
      branch_scores: { assembly: { score: 1, checkable: 22, supported: 22 } },
      counters,
    },
  } as unknown as ReadFindingRow
}

describe('quote fidelity is stated as k of n when some spans were not fully checked', () => {
  it('falls back to checked - sha_unverifiable on a row written before the counter', () => {
    const arms = projectArms(
      withCounters({
        assembly_quote_spans_checked: 16,
        assembly_quote_spans_ok: 16,
        assembly_quote_sha_unverifiable: 12,
      }),
    )!
    expect(arms.quote_fidelity).toBe(1)
    expect(arms.spans_fully_checked).toBe(4)
    const badge = recordBadge(projectAssembly(world)!, arms)
    expect(badge.quoteChecked).toEqual([4, 16])
  })

  it('reads the server counter when present and leaves a fully checked record alone', () => {
    const arms = projectArms(
      withCounters({
        assembly_quote_spans_checked: 16,
        assembly_quote_spans_ok: 16,
        assembly_quote_sha_verified: 16,
      }),
    )!
    expect(arms.spans_fully_checked).toBe(16)
    expect(recordBadge(projectAssembly(world)!, arms).quoteChecked).toBeNull()
  })
})

describe('projectSpanChecks', () => {
  it('projects the RU 470c27a4 number flag and the US 4e230ea2 actor flag', () => {
    const c = projectSpanChecks({
      version: 'span_evidence_checks.v1',
      flags: ['number_unsupported', 'actor_mismatch'],
      number_unsupported: [
        { number: 'eight', phrase: 'eight ukrainian region', markers: ['[11]', '[51]'] },
      ],
      actor_mismatch: [{ marker: '[18]', names: ['ru', 'ua', 'us'], span_actors: ['il', 'ir', 'us'] }],
    })!
    expect(c.flags).toEqual(['number_unsupported', 'actor_mismatch'])
    expect(c.number_unsupported[0].phrase).toBe('eight ukrainian region')
    expect(c.actor_mismatch[0].marker).toBe('[18]')
  })

  it('is total over a malformed entry', () => {
    expect(projectSpanChecks(null)).toBeNull()
    expect(projectSpanChecks({ flags: 'x' })).toEqual({
      flags: [],
      number_unsupported: [],
      actor_mismatch: [],
    })
  })
})
