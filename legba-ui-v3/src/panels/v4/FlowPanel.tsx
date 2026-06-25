/** v4 panel — The Flow (live registry canvas: sources→targets→analysts→packs). */
import { PanelBoundary } from './PanelBoundary'
import FlowRoom from '@/v4/flow'

export default function FlowPanel() {
  return (
    <PanelBoundary>
      <FlowRoom />
    </PanelBoundary>
  )
}
