/**
 * The Case — composed by the orchestrator from the Wave-3 agents' pieces: the
 * Excalidraw casework canvas + the case rail (pinned cards + typed links). State
 * lives in caseStore.ts (localStorage-persisted).
 */
import CaseBoard from './CaseBoard'
import CaseRail from './CaseRail'

export default function CaseRoom() {
  return (
    <div className="h-full w-full flex min-h-0 bg-surface-300">
      <div className="flex-1 min-w-0 relative">
        <CaseBoard />
      </div>
      <CaseRail />
    </div>
  )
}
