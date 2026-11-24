/**
 * W2 S-batch — Cross-framing reachable from the inline citation chip.
 *
 * The chip's hover/focus card carries a "cross-framing ▸" action when the
 * surface names its desk (`crossFramingTargetId`); the action drives an
 * `open_panel` carrying the claim the chip closes (`lib/crossFramingLink`,
 * UI-B1/shell — the event bridge is retired), so the Cross-framing panel lands
 * on THAT sentence. A surface that names no desk
 * renders no action — it never guesses one.
 */
import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import CitedProse from './CitedProse'
import {
  claimSentenceBefore,
  extractCitations,
  citationsByMarker,
  tokenizeProse,
} from '@/lib/citationsModel'
import { crossFramingSubjectFromParams } from '@/lib/crossFramingLink'
import { registerShell, type ShellPort } from '@/lib/applyAction'
import { pendingPanelParams, resetPanelParams } from '@/state/panelParams'

const BODY = {
  body: 'Fuel imports fell sharply in September [1]. The ministry denies a shortage [2][3].',
  data: {
    citations: [
      { marker: '[1]', signal_id: '11111111-1111-4111-8111-111111111111', title: 'Port data' },
      { marker: '[2]', signal_id: '22222222-2222-4222-8222-222222222222', title: 'Ministry' },
      { marker: '[3]', signal_id: '33333333-3333-4333-8333-333333333333', title: 'Wire' },
    ],
  },
}

let opened: string[] = []
let unregister: () => void = () => {}

beforeEach(() => {
  opened = []
  const shell = {
    mode: 'personal',
    openSingleton: (kind: string) => {
      opened.push(kind)
      return true
    },
  } as unknown as ShellPort
  unregister = registerShell(shell)
})

afterEach(() => {
  unregister()
  resetPanelParams()
})

describe('claimSentenceBefore — the claim a chip closes', () => {
  const citations = extractCitations(BODY)
  const tokens = tokenizeProse(BODY.body, citationsByMarker(citations))
  const markerIdx = (m: string) => tokens.findIndex((t) => t.kind === 'marker' && t.marker === m)

  it('is the sentence running up to the marker, not the whole paragraph', () => {
    expect(claimSentenceBefore(tokens, markerIdx('[1]'))).toBe('Fuel imports fell sharply in September')
    expect(claimSentenceBefore(tokens, markerIdx('[2]'))).toBe('The ministry denies a shortage')
  })

  it('every chip of an adjacent run names the same sentence', () => {
    expect(claimSentenceBefore(tokens, markerIdx('[3]'))).toBe('The ministry denies a shortage')
  })

  it('a marker with no prose before it names no claim', () => {
    const t = tokenizeProse('[1] opens the line.', citationsByMarker(citations))
    expect(claimSentenceBefore(t, 0)).toBeNull()
  })

  it('a period before the marker and one after it name the same claim', () => {
    const t = tokenizeProse('Prices rose 4%.[1]', citationsByMarker(citations))
    expect(claimSentenceBefore(t, 1)).toBe('Prices rose 4%')
  })
})

describe('the chip card’s cross-framing action', () => {
  it('opens Cross-framing on the claim the chip closes, for the named desk', () => {
    render(
      <CitedProse
        text={BODY.body}
        citations={extractCitations(BODY)}
        crossFramingTargetId="country_ruritania"
      />,
    )
    const actions = screen.getAllByTestId('citation-card-cross-framing')
    expect(actions).toHaveLength(3)
    fireEvent.click(actions[1])

    // ONE open_panel through the shell, and the claim rides its params.
    expect(opened).toEqual(['analysis.cross_framing'])
    const req = pendingPanelParams('analysis.cross_framing')
    expect(req && crossFramingSubjectFromParams(req.params)).toEqual({
      targetId: 'country_ruritania',
      claimId: null,
      claimText: 'The ministry denies a shortage',
      origin: 'cited-prose',
    })
  })

  it('the action does not fire the chip’s own drill', () => {
    const onCite = vi.fn()
    render(
      <CitedProse
        text={BODY.body}
        citations={extractCitations(BODY)}
        crossFramingTargetId="country_ruritania"
        onCiteClick={onCite}
      />,
    )
    fireEvent.click(screen.getAllByTestId('citation-card-cross-framing')[0])
    expect(onCite).not.toHaveBeenCalled()
  })

  it('a surface that names no desk renders no action and keeps the inert tooltip', () => {
    render(<CitedProse text={BODY.body} citations={extractCitations(BODY)} />)
    expect(screen.queryByTestId('citation-card-cross-framing')).toBeNull()
    const card = screen.getAllByTestId('citation-card')[0]
    expect(card.getAttribute('role')).toBe('tooltip')
    expect(card.className).toContain('pointer-events-none')
  })
})
