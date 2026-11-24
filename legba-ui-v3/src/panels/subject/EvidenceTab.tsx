/**
 * subject.page · Evidence tab (`#evidence`) — the subject's findings, the evidence its reads stand on.
 *
 * UI-B3 (the merges idiom): the tab mounts the retired `target.findings` component
 * UNMODIFIED, bound to the page's subject. `target.findings` aliases here
 * (`panel-registry/aliases.ts`), so a saved layout, a hash or a recorded
 * case that names it lands on this tab.
 */
import type { PanelProps } from '@/types'
import FindingsPanel from '@/panels/target/Findings'

export default function SubjectEvidenceTab(props: PanelProps) {
  return <FindingsPanel {...props} />
}
