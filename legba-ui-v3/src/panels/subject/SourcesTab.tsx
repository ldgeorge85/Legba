/**
 * subject.page · Sources tab (`#sources`) — the sources feeding the subject.
 *
 * UI-B3 (the merges idiom): the tab mounts the retired `target.sources` component
 * UNMODIFIED, bound to the page's subject. `target.sources` aliases here
 * (`panel-registry/aliases.ts`), so a saved layout, a hash or a recorded
 * case that names it lands on this tab.
 */
import type { PanelProps } from '@/types'
import SourcesPanel from '@/panels/target/Sources'

export default function SubjectSourcesTab(props: PanelProps) {
  return <SourcesPanel {...props} />
}
