/**
 * subject.page · Situations tab (`#situations`) — the subject's open situations and their tracked events.
 *
 * UI-B3 (the merges idiom): the tab mounts the retired `target.situations` component
 * UNMODIFIED, bound to the page's subject. `target.situations` aliases here
 * (`panel-registry/aliases.ts`), so a saved layout, a hash or a recorded
 * case that names it lands on this tab.
 */
import type { PanelProps } from '@/types'
import SituationsPanel from '@/panels/target/Situations'

export default function SubjectSituationsTab(props: PanelProps) {
  return <SituationsPanel {...props} />
}
