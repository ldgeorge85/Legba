/** v4 panel — the world_assessor's situational one-pager (refreshes every 6h). */
import { PanelBoundary } from './PanelBoundary'
import WorldAssessment from '@/v4/why/WorldAssessment'

export default function AssessmentPanel() {
  return (
    <PanelBoundary>
      <div className="h-full w-full overflow-auto bg-surface-300 p-6">
        <WorldAssessment />
      </div>
    </PanelBoundary>
  )
}
