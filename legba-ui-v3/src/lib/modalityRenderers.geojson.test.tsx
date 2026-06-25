/**
 * End-to-end UI check for the model-free GIS path: a `structured` /
 * `application/geo+json` signal — exactly the shape the backend
 * `legba.data.sources.geojson` handler now emits — reaches the existing
 * `application/geo+json` modality renderer entry via `ModalityRef`.
 *
 * We do NOT rebuild the renderer (it exists as a registry entry, MapLibre
 * wiring is a separate drop-in). This test only confirms the resolution +
 * surfacing path is correct for the new modality, so when the real MapLibre
 * component is flipped on (`implemented: true`) it receives the right node.
 */

import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'

import {
  MODALITY_RENDERERS,
  ModalityRef,
  resolveModalityRenderer,
} from './modalityRenderers'

// The Signal shape the geojson source handler emits (modality-first columns).
const GEO_NODE = {
  modality: 'structured',
  mime_type: 'application/geo+json',
  media_ref: 'https://earthquake.invalid/feed.geojson',
  canonical_url: 'https://earthquake.invalid/event/us6000abcd',
}

describe('geo+json modality path (model-free GIS source)', () => {
  it('resolves a structured/geo+json signal to the geo+json renderer entry', () => {
    const r = resolveModalityRenderer(GEO_NODE.modality, GEO_NODE.mime_type)
    // mime_type is most-specific → the geo+json entry wins over coarse `structured`.
    expect(r).toBe(MODALITY_RENDERERS['application/geo+json'])
    expect(r.label).toBe('geo+json')
    expect(r.pending).toBe('map') // MapLibre is the drop-in target
  })

  it('ModalityRef surfaces the geo+json badge + a source link for the signal', () => {
    render(<ModalityRef node={GEO_NODE} />)
    // Badge text is `${modality} · ${mime_type}` for a fine-mime node.
    expect(screen.getByText('structured · application/geo+json')).toBeTruthy()
    // canonical_url present → a "source" link (not a bare media link).
    const link = screen.getByRole('link', { name: /source/i }) as HTMLAnchorElement
    expect(link.href).toBe('https://earthquake.invalid/event/us6000abcd')
  })
})
