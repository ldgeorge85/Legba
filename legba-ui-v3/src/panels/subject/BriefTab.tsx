/**
 * subject.page · Brief tab (`#brief`) — the subject brief (the desk brief generalized — piece A's page shape over the subject's own roster).
 *
 * The tab mounts the retired `target.desk_brief_page` component, which
 * was GENERALIZED in place (its roster is the subject's own, its words the
 * profile's — see the panel's header), bound to the page's subject.
 * `target.desk_brief_page` aliases here
 * (`panel-registry/aliases.ts`), so a saved layout, a hash or a recorded
 * case that names it lands on this tab.
 */
import type { PanelProps } from '@/types'
import DeskBriefPagePanel from '@/panels/target/DeskBriefPage'

export default function SubjectBriefTab(props: PanelProps) {
  return <DeskBriefPagePanel {...props} />
}
