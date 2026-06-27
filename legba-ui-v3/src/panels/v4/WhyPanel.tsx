/**
 * v4 panel — The Why (provenance). Selection-driven: with nothing selected it
 * renders an in-panel node picker (recent findings / situations / entities) so
 * the room is useful on its own; select one — here or in any other room — and it
 * traces the lineage DAG / ego-graph in place. Wrapped in a boundary because the
 * cytoscape lineage render is the most crash-prone surface.
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
