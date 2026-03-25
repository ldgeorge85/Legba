import type React from 'react'
import { useWorkspaceStore } from '@/stores/workspace'
import { useSelectionStore } from '@/stores/selection'

interface EntityLinkProps {
  name: string
  id?: string
  type?: string
  className?: string
}

/**
 * Reusable clickable entity link.
 * Click -> updates selection store -> opens entity-detail panel.
 * Other panels (graph, map, timeline) react to the selection automatically.
 */
export function EntityLink({ name, id, type, className }: EntityLinkProps) {
  const openPanel = useWorkspaceStore(s => s.openPanel)
  const select = useSelectionStore(s => s.select)

  const handleClick = (e: React.MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    if (id) {
      select({ type: 'entity', id, name })
      openPanel('entity-detail', { id })
    } else {
      // No ID available — open entities panel to search by name
      openPanel('entities', { q: name })
    }
  }

  return (
    <span
      className={`cursor-pointer text-blue-400 hover:text-blue-300 hover:underline ${className || ''}`}
      onClick={handleClick}
      title={`View entity: ${name}${type ? ` (${type})` : ''}`}
    >
      {name}
    </span>
  )
}
