/**
 * subject.page · Findings tab (`#claims`) — the findings the subject's analysts wrote, with their verdicts.
 *
 * UI-B3 (the merges idiom): the tab mounts the retired `target.claims` component
 * UNMODIFIED, bound to the page's subject. `target.claims` aliases here
 * (`panel-registry/aliases.ts`), so a saved layout, a hash or a recorded
 * case that names it lands on this tab.
 */
import type { PanelProps } from '@/types'
import ClaimsPanel from '@/panels/target/Claims'

export default function SubjectClaimsTab(props: PanelProps) {
  return <ClaimsPanel {...props} />
}
