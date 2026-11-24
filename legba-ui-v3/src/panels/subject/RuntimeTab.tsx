/**
 * subject.page · Runtime tab (`#runtime`) — the subject's runtime (the descriptor, its units' runs, the gap strip).
 *
 * UI-B3 (the merges idiom): the tab mounts the retired `target.overview` component
 * UNMODIFIED, bound to the page's subject. `target.overview` aliases here
 * (`panel-registry/aliases.ts`), so a saved layout, a hash or a recorded
 * case that names it lands on this tab.
 */
import type { PanelProps } from '@/types'
import OverviewPanel from '@/panels/target/Overview'

export default function SubjectRuntimeTab(props: PanelProps) {
  return <OverviewPanel {...props} />
}
