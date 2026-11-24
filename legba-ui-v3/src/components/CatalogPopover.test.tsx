/**
 * The `＋` catalog says what each tile is FOR (X3/D2).
 *
 * The catalog listed thirty bare names. Every catalog kind now carries a
 * one-line `description` on its registry definition
 * (`panel-registry/descriptions.ts`), printed under the name and searched by
 * the filter. What must hold:
 *  - every kind the catalog lists, in either mode, has a non-empty description;
 *  - the description table names only registered kinds (no stale row);
 *  - no description borrows an instance's words (the platform is not the
 *    geopolitics exemplar — planning/PLATFORM_VS_EXEMPLAR §1);
 *  - the popover renders the line and the filter matches on it.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { CatalogPopover, catalogEntries } from './CatalogPopover'
import { PANEL_REGISTRY } from '@/panel-registry/registry'
import { PANEL_DESCRIPTIONS } from '@/panel-registry/descriptions'
import { catalogKinds } from '@/panel-registry/kindPlan'

afterEach(() => cleanup())

describe('catalog descriptions', () => {
  it('every catalog kind has a one-line description on its registry definition', () => {
    const kinds = catalogKinds()
    expect(kinds.length).toBeGreaterThanOrEqual(25)
    for (const k of kinds) {
      const d = PANEL_REGISTRY[k].definition.description
      expect(d, k).toBeTruthy()
      expect(d!.length, k).toBeLessThanOrEqual(120)
      expect(d!, k).not.toContain('\n')
    }
  })

  it('the description table names only registered kinds', () => {
    for (const k of Object.keys(PANEL_DESCRIPTIONS)) expect(PANEL_REGISTRY[k as keyof typeof PANEL_REGISTRY], k).toBeDefined()
  })

  it("no description spells an instance's words", () => {
    const all = Object.values(PANEL_DESCRIPTIONS).join(' ').toLowerCase()
    for (const word of ['country', 'g20', 'geopolit', 'hormuz', 'desk', 'conflict']) {
      expect(all, word).not.toContain(word)
    }
  })

  it('both modes list only described kinds', () => {
    for (const mode of ['personal', 'cis'] as const) {
      for (const e of catalogEntries(mode)) expect(e.description, `${mode} ${e.kind}`).toBeTruthy()
    }
  })

  it('the popover prints each description under its name, and the filter searches it', () => {
    render(<CatalogPopover mode="personal" onClose={() => {}} />)
    const entries = catalogEntries('personal')
    for (const e of entries) {
      expect(screen.getByTestId(`catalog-desc-${e.kind}`)).toHaveTextContent(e.description!)
    }
    // "calibration" is in Accuracy's description, not its name.
    fireEvent.change(screen.getByTestId('catalog-filter'), { target: { value: 'calibration' } })
    expect(screen.getByTestId('catalog-item-system.eval_scorecard')).toBeInTheDocument()
    expect(screen.queryByTestId('catalog-item-system.ledgers')).toBeNull()
  })
})
