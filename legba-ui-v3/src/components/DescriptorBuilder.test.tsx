/**
 * UI-4 — DescriptorBuilder + starter-descriptor library tests.
 *
 * Asserts (acceptance):
 *   - The builder produces a *valid-shaped* target descriptor from form input
 *     (identity/scope/sources/analyst/outputs all present + typed).
 *   - Client validation flags a bad id / a missing required field.
 *   - "Build & save" POSTs the assembled body to the generic descriptor
 *     endpoint, and a registry 422 surfaces inline.
 *   - Starters clone into fresh bodies (no shared refs), one per family.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import {
  DescriptorBuilder,
  buildDescriptorBody,
  validateValues,
  setPath,
  getPath,
  coerceValue,
  FIELD_SPECS,
  type FieldSpec,
} from './DescriptorBuilder'
import {
  STARTER_DESCRIPTORS,
  startersForFamily,
  starterByKey,
  VERSION_SENTINEL,
} from '@/lib/starter-descriptors'
import { mockErrorResponse } from '@/test/apiMocks'

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

/* --------------------------- path helpers ---------------------------- */

describe('path helpers', () => {
  it('setPath creates nested objects immutably', () => {
    const base = { a: { b: 1 } }
    const out = setPath(base, 'a.c.d', 5)
    expect(getPath(out, 'a.c.d')).toBe(5)
    expect(getPath(out, 'a.b')).toBe(1)
    // original untouched
    expect((base.a as Record<string, unknown>).c).toBeUndefined()
  })

  it('coerceValue splits a list field and trims', () => {
    const spec: FieldSpec = { path: 'x', label: 'x', type: 'list' }
    expect(coerceValue(spec, ' a , b ,, c ')).toEqual(['a', 'b', 'c'])
  })

  it('coerceValue parses numbers and rejects blanks', () => {
    const spec: FieldSpec = { path: 'x', label: 'x', type: 'number' }
    expect(coerceValue(spec, '42')).toBe(42)
    expect(coerceValue(spec, '')).toBeUndefined()
    expect(coerceValue(spec, 'nope')).toBeUndefined()
  })
})

/* --------------------------- build body ------------------------------ */

describe('buildDescriptorBody (target)', () => {
  it('produces a complete, typed target descriptor from form input', () => {
    const values: Record<string, string> = {
      'identity.id': 'acme_corp',
      'identity.name': 'ACME Corp',
      'identity.owner': 'analyst_jane',
      'identity.abstraction_level': 'L1',
      'scope.domain': 'geo',
      'scope.geo': 'US, GB',
      'scope.languages': 'en',
      'scope.entity_classes': 'organization, person',
      'scope.tags': 'acme, corp_watch',
      'analyst.cadence.fallback_schedule': '*/30 * * * *',
    }
    const body = buildDescriptorBody('target', values)

    // identity
    const identity = body.identity as Record<string, unknown>
    expect(identity.id).toBe('acme_corp')
    expect(identity.name).toBe('ACME Corp')
    expect(identity.owner).toBe('analyst_jane')
    expect(identity.abstraction_level).toBe('L1')
    expect(identity.version).toBe(VERSION_SENTINEL)
    expect(identity.schema_uri).toBe('legba/target/2.0.0')
    expect(typeof identity.created).toBe('string')

    // scope — lists coerced to arrays
    const scope = body.scope as Record<string, unknown>
    expect(scope.domain).toBe('geo')
    expect(scope.geo).toEqual(['US', 'GB'])
    expect(scope.languages).toEqual(['en'])
    expect(scope.entity_classes).toEqual(['organization', 'person'])
    expect(scope.tags).toEqual(['acme', 'corp_watch'])

    // structural blocks from the skeleton stay present + valid
    expect(Array.isArray(body.sources)).toBe(true)
    expect(body.pipeline).toBeTruthy()
    expect(body.coordination).toBeTruthy()
    expect(Array.isArray(body.outputs)).toBe(true)

    // nested dotted path applied
    const analyst = body.analyst as Record<string, unknown>
    const cadence = analyst.cadence as Record<string, unknown>
    expect(cadence.fallback_schedule).toBe('*/30 * * * *')
  })

  it('emits a SourceRef-shaped sources entry (source_selector, not the legacy inline binding)', () => {
    // Locks in the source-first SourceRef shape — verified against the real
    // pydantic TargetDescriptor schema (sources: list[SourceRef]).
    const body = buildDescriptorBody('target', {
      'identity.id': 'src_shape_target',
      'identity.name': 'X',
      'identity.owner': 'op',
    })
    const sources = body.sources as Array<Record<string, unknown>>
    expect(sources).toHaveLength(1)
    expect(sources[0]).toHaveProperty('source_selector')
    expect(sources[0]).not.toHaveProperty('kind')
    const sel = sources[0].source_selector as Record<string, unknown>
    expect(Array.isArray(sel.tags)).toBe(true)
  })

  it('keeps the skeleton defaults for optional fields left blank', () => {
    const body = buildDescriptorBody('target', {
      'identity.id': 'x_target',
      'identity.name': 'X',
      'identity.owner': 'op',
    })
    // languages was not provided → skeleton default ['en'] remains
    const scope = body.scope as Record<string, unknown>
    expect(scope.languages).toEqual(['en'])
  })
})

describe('buildDescriptorBody (other families)', () => {
  it('source body carries identity.kind + a poll cron under cadence.schedule.raw', () => {
    const body = buildDescriptorBody('source', {
      'identity.id': 'source.acme.rss',
      'identity.name': 'ACME RSS',
      'identity.kind': 'rss',
      'identity.owner': 'op',
      'cadence.schedule.raw': '*/10 * * * *',
    })
    const identity = body.identity as Record<string, unknown>
    expect(identity.kind).toBe('rss')
    const cadence = body.cadence as Record<string, unknown>
    const schedule = cadence.schedule as Record<string, unknown>
    expect(schedule.raw).toBe('*/10 * * * *')
    expect(schedule.factory_kind).toBe('cron')
  })

  it('analyst body carries the chosen kind + a numeric budget', () => {
    const body = buildDescriptorBody('analyst', {
      'identity.id': 'my_analyst',
      'identity.name': 'My Analyst',
      'identity.owner': 'op',
      'identity.kind': 'predictor',
      'method.budget_tokens_per_day': '12345',
    })
    const identity = body.identity as Record<string, unknown>
    expect(identity.kind).toBe('predictor')
    const method = body.method as Record<string, unknown>
    expect(method.budget_tokens_per_day).toBe(12345)
    // identity.kind (AnalystKind) and method.kind (MethodKind) are distinct
    // enums — the skeleton's method.kind must stay a valid MethodKind
    // (llm_planner), not be overwritten by the AnalystKind choice.
    expect(method.kind).toBe('llm_planner')
  })

  it('action_pack body carries governor numbers + applies_to_tags', () => {
    const body = buildDescriptorBody('action_pack', {
      'identity.id': 'my_pack',
      'identity.name': 'My Pack',
      'identity.owner': 'op',
      'applies_to_tags': 'a, b',
      'governor.max_invocations_per_hour': '30',
    })
    expect(body.applies_to_tags).toEqual(['a', 'b'])
    const gov = body.governor as Record<string, unknown>
    expect(gov.max_invocations_per_hour).toBe(30)
  })
})

/* --------------------------- validation ------------------------------ */

describe('validateValues', () => {
  it('flags a missing required field', () => {
    const errors = validateValues('target', { 'identity.name': 'x', 'identity.owner': 'o' })
    expect(errors['identity.id']).toBe('required')
  })

  it('flags a target id that violates the snake_case pattern', () => {
    const errors = validateValues('target', {
      'identity.id': 'Bad Id!',
      'identity.name': 'x',
      'identity.owner': 'o',
    })
    expect(errors['identity.id']).toMatch(/must match/)
  })

  it('accepts a valid full target form', () => {
    const errors = validateValues('target', {
      'identity.id': 'good_target',
      'identity.name': 'Good',
      'identity.owner': 'op',
    })
    expect(Object.keys(errors)).toHaveLength(0)
  })
})

/* --------------------------- component ------------------------------- */

describe('DescriptorBuilder component', () => {
  it('renders a field per spec and disables save until valid', () => {
    render(<DescriptorBuilder family="target" />)
    for (const spec of FIELD_SPECS.target) {
      expect(screen.getByTestId(`builder-field-${spec.path}`)).toBeInTheDocument()
    }
    // required fields empty → save disabled
    expect(screen.getByTestId('builder-save')).toBeDisabled()
  })

  it('enables save and POSTs the assembled descriptor on submit', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ version: 'deadbeef'.repeat(8) }),
    })
    vi.stubGlobal('fetch', fetchMock)
    const onSaved = vi.fn()

    render(<DescriptorBuilder family="target" onSaved={onSaved} />)
    fireEvent.change(screen.getByTestId('builder-field-identity.id'), {
      target: { value: 'wired_target' },
    })
    fireEvent.change(screen.getByTestId('builder-field-identity.name'), {
      target: { value: 'Wired' },
    })
    fireEvent.change(screen.getByTestId('builder-field-identity.owner'), {
      target: { value: 'op' },
    })
    const save = screen.getByTestId('builder-save')
    expect(save).toBeEnabled()
    fireEvent.click(save)

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('deadbeef'.repeat(8)))
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/registry/descriptors/target',
      expect.objectContaining({ method: 'POST' }),
    )
    const init = fetchMock.mock.calls[0][1] as RequestInit
    const sent = JSON.parse(init.body as string)
    expect(sent.identity.id).toBe('wired_target')
    expect(sent.identity.version).toBe(VERSION_SENTINEL)
  })

  it('surfaces the registry 422 message inline without losing input', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      mockErrorResponse(422, {
        detail: { error: 'pydantic_validation', message: 'scope.geo invalid' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    render(<DescriptorBuilder family="target" />)
    fireEvent.change(screen.getByTestId('builder-field-identity.id'), {
      target: { value: 'bad_geo_target' },
    })
    fireEvent.change(screen.getByTestId('builder-field-identity.name'), {
      target: { value: 'Bad' },
    })
    fireEvent.change(screen.getByTestId('builder-field-identity.owner'), {
      target: { value: 'op' },
    })
    fireEvent.click(screen.getByTestId('builder-save'))

    await waitFor(() => expect(screen.getByTestId('builder-error')).toBeInTheDocument())
    expect(screen.getByTestId('builder-error')).toHaveTextContent('scope.geo invalid')
    expect(screen.getByTestId('builder-error')).toHaveTextContent('422')
    // input preserved
    expect(screen.getByTestId('builder-field-identity.id')).toHaveValue('bad_geo_target')
  })

  it('renders a JSON preview of the assembled body', () => {
    render(<DescriptorBuilder family="analyst" />)
    fireEvent.click(screen.getByTestId('builder-toggle-preview'))
    const preview = screen.getByTestId('builder-preview')
    expect(preview).toHaveTextContent('"schema_uri"')
    expect(preview).toHaveTextContent('legba/analyst/1.0.0')
  })
})

/* --------------------------- starters -------------------------------- */

describe('starter-descriptor library', () => {
  it('has at least one starter per family', () => {
    for (const fam of ['target', 'source', 'analyst', 'action_pack', 'stack'] as const) {
      expect(startersForFamily(fam).length).toBeGreaterThan(0)
    }
  })

  it('builds fresh (non-shared) bodies each call', () => {
    const t = starterByKey('target.geo_basic')!
    const a = t.build()
    const b = t.build()
    expect(a).not.toBe(b)
    ;(a.identity as Record<string, unknown>).id = 'mutated'
    expect((b.identity as Record<string, unknown>).id).toBe('example_target')
  })

  it('every starter stamps the version sentinel', () => {
    for (const s of STARTER_DESCRIPTORS) {
      const body = s.build()
      // stack starters are flat; descriptor families nest under identity
      const version =
        (body.identity as Record<string, unknown> | undefined)?.version ?? body.version
      expect(version).toBe(VERSION_SENTINEL)
    }
  })

  it('the target starter is itself a valid build input (round-trips through validateValues)', () => {
    const body = starterByKey('target.geo_basic')!.build()
    const identity = body.identity as Record<string, unknown>
    const errors = validateValues('target', {
      'identity.id': String(identity.id),
      'identity.name': String(identity.name),
      'identity.owner': String(identity.owner),
    })
    expect(Object.keys(errors)).toHaveLength(0)
  })
})
