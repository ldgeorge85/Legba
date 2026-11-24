/**
 * subject.page · Graph tab (`#graph`) — the subject's entity and relationship graph.
 *
 * UI-B3 (the merges idiom): the tab mounts the retired `target.graph` component
 * UNMODIFIED, bound to the page's subject. `target.graph` aliases here
 * (`panel-registry/aliases.ts`), so a saved layout, a hash or a recorded
 * case that names it lands on this tab.
 */
import type { PanelProps } from '@/types'
import GraphPanel from '@/panels/target/Graph'

export default function SubjectGraphTab(props: PanelProps) {
  return <GraphPanel {...props} />
}
