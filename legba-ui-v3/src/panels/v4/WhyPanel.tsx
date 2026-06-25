/**
 * v4 panel — The Why (provenance). Selection-driven: with nothing selected it
 * shows the world assessment; select a finding/situation anywhere and this panel
 * traces its lineage in place. Wrapped in a boundary because the cytoscape
 * lineage render is the most crash-prone surface.
 */
import { PanelBoundary } from './PanelBoundary'
import WhyRoom from '@/v4/why'

export default function WhyPanel() {
  return (
    <PanelBoundary>
      <WhyRoom />
    </PanelBoundary>
  )
}
