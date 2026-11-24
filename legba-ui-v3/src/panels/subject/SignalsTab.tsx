/**
 * subject.page · Signals tab (`#signals`) — the ingested signals the subject's slice reads.
 *
 * UI-B3 (the merges idiom): the tab mounts the retired `target.signals` component
 * UNMODIFIED, bound to the page's subject. `target.signals` aliases here
 * (`panel-registry/aliases.ts`), so a saved layout, a hash or a recorded
 * case that names it lands on this tab.
 */
import type { PanelProps } from '@/types'
import SignalsPanel from '@/panels/target/Signals'

export default function SubjectSignalsTab(props: PanelProps) {
  return <SignalsPanel {...props} />
}
