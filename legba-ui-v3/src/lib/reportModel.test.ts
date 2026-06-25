/**
 * Unit tests for the UI-6 report-export model (DOM-free).
 */

import { describe, it, expect } from 'vitest'
import {
  buildArtifact,
  buildMarkdownReport,
  buildStixBundle,
  stixId,
  type ReportItem,
} from './reportModel'

function item(over: Partial<ReportItem> = {}): ReportItem {
  return {
    kind: 'finding',
    id: 'f1',
    title: 'Coup risk',
    body: 'army movement detected',
    severity: 'high',
    target_id: 'brazil',
    produced_at: '2026-06-03T00:00:00Z',
    derived_from: [],
    ...over,
  }
}

describe('stixId', () => {
  it('is deterministic and uuid-shaped', () => {
    const a = stixId('report', 'finding:f1')
    const b = stixId('report', 'finding:f1')
    expect(a).toBe(b)
    expect(a).toMatch(/^report--[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-8[0-9a-f]{3}-[0-9a-f]{12}$/)
  })
  it('differs by seed', () => {
    expect(stixId('report', 'a')).not.toBe(stixId('report', 'b'))
  })
})

describe('buildStixBundle', () => {
  it('produces a 2.1 bundle with a report SDO per item', () => {
    const b = buildStixBundle([item()], { created: '2026-06-03T00:00:00Z' })
    expect(b.type).toBe('bundle')
    const objs = b.objects as Array<Record<string, unknown>>
    const report = objs.find((o) => o.type === 'report')!
    expect(report.spec_version).toBe('2.1')
    expect(report.name).toBe('Coup risk')
    expect(report.labels).toContain('severity:high')
    expect(report.x_legba_target_id).toBe('brazil')
    expect((report.object_marking_refs as string[])[0]).toContain('marking-definition--')
  })

  it('emits a derived-from relationship SDO per provenance link', () => {
    const b = buildStixBundle([item({ derived_from: ['p1', 'p2'] })])
    const objs = b.objects as Array<Record<string, unknown>>
    const rels = objs.filter((o) => o.type === 'relationship')
    expect(rels.length).toBe(2)
    expect(rels[0].relationship_type).toBe('derived-from')
    expect(rels[0].x_legba_parent_id).toBe('p1')
  })

  it('applies the chosen TLP marking', () => {
    const amber = buildStixBundle([item()], { tlp: 'amber' })
    const red = buildStixBundle([item()], { tlp: 'red' })
    const am = (amber.objects as any[])[0].object_marking_refs[0]
    const rm = (red.objects as any[])[0].object_marking_refs[0]
    expect(am).not.toBe(rm)
  })
})

describe('buildMarkdownReport', () => {
  it('headers, TLP line, severity grouping, provenance footnote', () => {
    const md = buildMarkdownReport(
      [
        item({ id: 'a', title: 'Crit', severity: 'critical', derived_from: ['s1'] }),
        item({ id: 'b', title: 'Low one', severity: 'low' }),
      ],
      { title: 'Brazil Brief', tlp: 'amber', created: '2026-06-03T00:00:00Z' },
    )
    expect(md).toContain('# Brazil Brief')
    expect(md).toContain('TLP:AMBER')
    // critical sorts above low
    expect(md.indexOf('## CRITICAL')).toBeLessThan(md.indexOf('## LOW'))
    expect(md).toContain('### Crit')
    expect(md).toContain('**Provenance:**')
    expect(md).toContain('`s1`')
  })
  it('handles empty selection', () => {
    expect(buildMarkdownReport([])).toContain('_No items selected._')
  })
})

describe('buildArtifact', () => {
  it('stix → .stix.json with JSON content', () => {
    const a = buildArtifact([item()], 'stix', { created: '2026-06-03T00:00:00Z' })
    expect(a.filename).toBe('legba-report-2026-06-03.stix.json')
    expect(a.mime).toBe('application/json')
    expect(JSON.parse(a.content).type).toBe('bundle')
  })
  it('markdown → .md with text/markdown', () => {
    const a = buildArtifact([item()], 'markdown', { created: '2026-06-03T00:00:00Z' })
    expect(a.filename).toBe('legba-report-2026-06-03.md')
    expect(a.mime).toBe('text/markdown')
    expect(a.content).toContain('# Legba Intelligence Report')
  })
})
