/**
 * UI-B2 — the projection reader (`useProjection`) and the profile's tier /
 * alias helpers.
 *
 * The projection route refuses BY NAME (409 `projection_disabled` /
 * `projection_empty` / `projection_stale`, 404 `plane_unknown` /
 * `section_undeclared`); the reader turns every refusal into a typed absence
 * `{state: 'absent', name, reason}` a surface prints, never a blank. A 200
 * carries the build stamp (`projected_at`) — the rows' as-of.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { mockErrorResponse } from '@/test/apiMocks'
import { ApiError } from '@/lib/api'
import noneFixture from '@/test/profileNone.fixture.json'
import raw from './profile_ui.json'
import {
  PROFILE_UI,
  bandsFromProjection,
  profileUiFrom,
  projectionAbsenceText,
  projectionOk,
  projectionPath,
  projectionRefusal,
  readTiers,
  resolveAliasedId,
  subjectGroupsFromProjection,
  subjectGroupsToList,
  subjectTierIds,
  tiersFromProjection,
  untargetedSubject,
  useProjection,
  useReadTiers,
} from './profileUi'

const sanctions = profileUiFrom(noneFixture)

function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

function stub(impl: (url: string) => Promise<Response>) {
  const f = vi.fn().mockImplementation(impl)
  vi.stubGlobal('fetch', f)
  return f
}

afterEach(() => vi.unstubAllGlobals())

describe('the plane', () => {
  it('the export carries the plane; a profile without one reads its own id', () => {
    expect(raw.plane).toBe('geopolitics')
    expect(PROFILE_UI.plane).toBe('geopolitics')
    expect(profileUiFrom({ profile: 'x' }).plane).toBe('x')
    expect(projectionPath('tiers')).toBe('/registry/projection/geopolitics/tiers')
    expect(projectionPath('bands', sanctions)).toBe('/registry/projection/sanctions_export_control/bands')
  })
})

describe('useProjection — a refusal is an absence BY NAME', () => {
  for (const [status, state] of [
    [409, 'projection_empty'],
    [409, 'projection_disabled'],
    [409, 'projection_stale'],
    [404, 'section_undeclared'],
    [404, 'plane_unknown'],
  ] as const) {
    it(`${status} ${state}`, async () => {
      const f = stub(async () =>
        mockErrorResponse(status, { detail: { state, reason: `because ${state}`, plane: 'geopolitics' } }),
      )
      const { result } = renderHook(() => useProjection('tiers'), { wrapper: wrapper() })
      await waitFor(() => expect(result.current.state).toBe('absent'))
      expect(result.current).toEqual({
        state: 'absent',
        section: 'tiers',
        plane: 'geopolitics',
        name: state,
        reason: `because ${state}`,
      })
      expect(projectionAbsenceText(result.current)).toBe(`${state} — because ${state}`)
      // a refusal is an answer: asked once, never retried
      expect(f).toHaveBeenCalledTimes(1)
      expect(String(f.mock.calls[0][0])).toBe('/api/v1/registry/projection/geopolitics/tiers')
    })
  }

  it('a transport failure is named too (projection_unreachable), never a blank', () => {
    expect(projectionRefusal(new ApiError(502, 'bad gateway'), 'bands', 'p')).toMatchObject({
      state: 'absent',
      name: 'projection_unreachable',
      reason: 'HTTP 502',
    })
    expect(projectionRefusal(new TypeError('Failed to fetch'), 'bands', 'p')).toMatchObject({
      name: 'projection_unreachable',
      reason: 'Failed to fetch',
    })
  })

  it('a 200 carries its rows in `ord` order and the build stamp', async () => {
    stub(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        state: 'ok',
        plane: 'geopolitics',
        projected_at: '2026-10-04T10:27:00Z',
        age_seconds: 60,
        max_age_seconds: 7200,
        section: 'bands',
        rows: [
          { key: 'checked', ord: 1, value: { label: 'Held', question: 'What held?' } },
          { key: 'changed', ord: 0, value: { label: 'Moved', question: 'What moved?' } },
        ],
        unmeasured: null,
      }),
    }) as unknown as Response)
    const { result } = renderHook(() => useProjection('bands'), { wrapper: wrapper() })
    await waitFor(() => expect(result.current.state).toBe('ok'))
    const read = result.current
    if (read.state !== 'ok') throw new Error('unreachable')
    expect(read.projectedAt).toBe('2026-10-04T10:27:00Z')
    expect(read.rows.map((r) => r.key)).toEqual(['changed', 'checked'])
    expect(bandsFromProjection(read.rows)).toEqual([
      { id: 'changed', label: 'Moved', question: 'What moved?' },
      { id: 'checked', label: 'Held', question: 'What held?' },
    ])
    expect(projectionAbsenceText(read)).toBeNull()
  })

  it('a 200 that is not the route shape is projection_malformed', () => {
    expect(projectionOk({ nope: 1 }, 'tiers', 'p')).toMatchObject({ state: 'absent', name: 'projection_malformed' })
  })
})

describe('useReadTiers — the projection wins when it answers; the packaged profile otherwise', () => {
  it('a refusal keeps the profile tiers byte for byte, and carries the refusal', async () => {
    stub(async () => mockErrorResponse(409, { detail: { state: 'projection_empty', reason: 'never built' } }))
    const { result } = renderHook(() => useReadTiers(), { wrapper: wrapper() })
    // first paint: the packaged tiers, before the route answers
    expect(result.current.tiers).toEqual(readTiers())
    await waitFor(() => expect(result.current.read.state).toBe('absent'))
    expect(result.current.source).toBe('profile')
    expect(result.current.tiers.map((t) => [t.id, t.label, t.producer])).toEqual(
      raw.frames.map((f) => [f.id, f.label, f.producer]),
    )
  })

  it('an answering plane supplies the tiers', async () => {
    stub(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        rows: [
          { key: 'jurisdiction', ord: 0, value: { label: 'Jurisdiction', parent: null, producer: 'jurisdiction_read' } },
          { key: 'regime', ord: 1, value: { label: 'Regime', parent: 'jurisdiction', producer: 'regime_read' } },
        ],
      }),
    }) as unknown as Response)
    const { result } = renderHook(() => useReadTiers(), { wrapper: wrapper() })
    await waitFor(() => expect(result.current.source).toBe('projection'))
    expect(result.current.tiers.map((t) => t.label)).toEqual(['Jurisdiction', 'Regime'])
  })
})

describe('section decoders', () => {
  it('tiers and subject groups', () => {
    expect(
      tiersFromProjection([
        { key: 'world', ord: 0, value: { label: 'World', producer: 'w' } },
        { key: 'bad', ord: 1, value: { producer: 'x' } },
      ]),
    ).toEqual([{ id: 'world', label: 'World', parent: null, producer: 'w' }])
    const rows = [
      { key: 'region', ord: 0, value: { kind: 'region', label: 'Region frames', member_rule: [], members: ['country_g20_de'] } },
      { key: 'region_europe', ord: 1, value: { kind: 'region', label: 'Region — Europe', parent: 'region', member_rule: ['tag:region_europe'], members: ['country_g20_de'] } },
      { key: 'escalation_composition', ord: 2, value: { kind: 'thematic', label: 'Escalation', member_rule: ['analyst:escalation_composition'], members: [] } },
    ]
    expect(subjectGroupsFromProjection(rows)).toEqual([
      { id: 'region', kind: 'region', label: 'Region frames', parent: null, memberRule: [], members: ['country_g20_de'] },
      { id: 'region_europe', kind: 'region', label: 'Region — Europe', parent: 'region', memberRule: ['tag:region_europe'], members: ['country_g20_de'] },
      { id: 'escalation_composition', kind: 'thematic', label: 'Escalation', parent: null, memberRule: ['analyst:escalation_composition'], members: [] },
    ])
    // the kind-of-kinds and the analyst lane head nothing
    expect(subjectGroupsToList(rows).map((g) => g.id)).toEqual(['region_europe'])
  })
})

describe('the tier helpers read the profile, not a literal', () => {
  const tiers = readTiers()

  it('an untargeted read on a parentless tier names the top floor; a nested tier names itself', () => {
    const by = (id: string) => tiers.find((t) => t.id === id)!
    expect(untargetedSubject(by('world'), tiers)).toBe('World')
    expect(untargetedSubject(by('assessment'), tiers)).toBe('World')
    expect(untargetedSubject(by('region'), tiers)).toBe('Region')
    expect(untargetedSubject(by('country'), tiers)).toBe('Country')
  })

  it('the per-subject tier is the nested frame nothing hangs under', () => {
    expect([...subjectTierIds(tiers)]).toEqual(['country'])
    expect([...subjectTierIds(readTiers(sanctions))]).toEqual([])
  })
})

describe('THE ID ALIASES — a profile renames a stance or a mission by label; the id never changes', () => {
  it("a second profile's label and the shell's own both reach the same id", () => {
    const stances = [
      { id: 'desk', label: sanctions.stances.desk.label, defaultLabel: 'Desk' },
      { id: 'trust', label: 'Trust', defaultLabel: 'Trust' },
    ]
    expect(sanctions.stances.desk.label).toBe('Regime')
    expect(resolveAliasedId('Regime', stances)).toBe('desk')
    expect(resolveAliasedId('desk', stances)).toBe('desk')
    expect(resolveAliasedId('Desk', stances)).toBe('desk')
    expect(sanctions.missions.desk_watch).toBe('Counterparty watch')
    expect(
      resolveAliasedId('counterparty watch', [{ id: 'desk_watch', label: 'Counterparty watch', defaultLabel: 'Desk Watch' }]),
    ).toBe('desk_watch')
  })
})
