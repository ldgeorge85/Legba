import { describe, expect, it } from 'vitest'

import { MODALITY_RENDERERS, resolveModalityRenderer } from './modalityRenderers'

describe('modality renderer registry (DESIGN §7.5, UI half)', () => {
  it('resolves most-specific-first: mime_type > modality > default', () => {
    // exact mime entry wins over the coarse modality
    expect(resolveModalityRenderer('structured', 'application/geo+json').label).toBe('geo+json')
    // modality when there is no mime entry
    expect(resolveModalityRenderer('video', 'video/mp4').label).toBe('video')
    expect(resolveModalityRenderer('video').label).toBe('video')
    // unknown modality / mime / nullish → default
    expect(resolveModalityRenderer('hologram').label).toBe('unknown')
    expect(resolveModalityRenderer(null, null).label).toBe('unknown')
    expect(resolveModalityRenderer(undefined, 'application/unheard-of').label).toBe('unknown')
  })

  it('text is the implemented default (no badge); every other modality is a placeholder', () => {
    const text = resolveModalityRenderer('text')
    expect(text.implemented).toBe(true)
    expect(text.showBadge).toBe(false)

    for (const key of ['audio', 'video', 'image', 'structured', 'binary', 'application/geo+json']) {
      const r = MODALITY_RENDERERS[key]
      expect(r.showBadge).toBe(true)
      expect(r.implemented).toBe(false) // drop-in seam: flip to true when a real renderer lands
      expect(r.pending).toBeTruthy()
    }
  })
})
