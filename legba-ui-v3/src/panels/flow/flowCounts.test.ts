import { describe, expect, it } from 'vitest'
import { nodeCount, viewCountsLine, viewGroupLabel } from './flowView'
import { graphFixture } from './flowFixtures'
import { initialExpanded, projectView, scopePinned, viewGroups } from './flowView'
import { scopeOptions } from './flowProjection'

describe('Flow counts and group labels (U-1: the two cosmetic oddities)', () => {
  const card = (id: string) => ({ id, type: 'flowCard', position: { x: 0, y: 0 }, data: {} }) as never
  const box = (id: string) => ({ id, type: 'flowGroup', position: { x: 0, y: 0 }, data: {} }) as never

  it('a group box is not counted as a node: every member drawn plus one bundle box reads "112 of 112"', () => {
    const nodes = [...Array.from({ length: 112 }, (_, i) => card(`n${i}`)), box('group:sources:rss')]
    const line = viewCountsLine({ nodes, edges: [], total: { nodes: 112, edges: 40 } })
    expect(line).toBe('112 of 112 nodes drawn · 1 group box · 0 edges drawn for 40 declared')
    expect(line).not.toMatch(/113/)
  })

  it('the overview counts its collapsed groups apart', () => {
    const line = viewCountsLine({ nodes: [card('a'), box('group:x'), box('group:y')], edges: [], total: { nodes: 9, edges: 3 } })
    expect(line).toBe('1 of 9 nodes drawn · 2 group boxes · 0 edges drawn for 3 declared')
  })

  it('on every scope of the fixture graph, nodes drawn never exceed nodes in scope', () => {
    const payload = graphFixture()
    for (const opt of scopeOptions(payload)) {
      const pinned = scopePinned(payload, opt.value)
      const index = viewGroups(payload)
      const expanded = initialExpanded(payload, opt.value, pinned, index)
      const view = projectView(payload, { expanded, pinned }, index)
      const m = viewCountsLine(view).match(/^(\d+) of (\d+) node/)
      expect(m, opt.value).not.toBeNull()
      expect(Number(m![1]), opt.value).toBeLessThanOrEqual(Number(m![2]))
    }
  })

  it('a group name and its count never run together, and one is "1 node"', () => {
    expect(viewGroupLabel('competing_hypotheses')).toBe('competing hypotheses')
    expect(viewGroupLabel('analyst:inquiry')).toBe('analyst · inquiry')
    expect(nodeCount(1)).toBe('1 node')
    expect(nodeCount(3)).toBe('3 nodes')
  })
})
