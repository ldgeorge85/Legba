import React, { useMemo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useWorkspaceStore } from '@/stores/workspace'
import { useSelectionStore } from '@/stores/selection'

/**
 * IntelMarkdown — a wrapper around ReactMarkdown that adds intel-aware features:
 *
 * 1. Standard markdown rendering (tables, code, blockquotes, lists) via remark-gfm
 * 2. Clickable deep links for [SIG:uuid] and [EVT:uuid] patterns
 * 3. Entity names in **bold** rendered as clickable <EntityLink>-style spans
 *
 * Entity name detection requires passing an entityNames map (name -> id).
 * Without it, bold text renders normally.
 */

interface IntelMarkdownProps {
  children: string
  /** Map of entity name (lowercase) -> { id, name (original case) } for bold-text entity linking */
  entityNames?: Map<string, { id: string; name: string }>
  /** Additional className for the wrapper div */
  className?: string
}

/** Regex to detect [SIG:uuid] and [EVT:uuid] reference patterns */
const REF_PATTERN = /\[(SIG|EVT):([0-9a-f-]{36})\]/gi

/**
 * Pre-process markdown text to convert [SIG:uuid] and [EVT:uuid] patterns
 * into clickable placeholder markers that our custom renderers can pick up.
 * We convert them to markdown links with a custom scheme: [SIG:uuid](intel://sig/uuid)
 */
function preprocessRefs(text: string): string {
  return text.replace(REF_PATTERN, (_, type: string, uuid: string) => {
    const upper = type.toUpperCase()
    return `[${upper}:${uuid.slice(0, 8)}...](intel://${upper.toLowerCase()}/${uuid})`
  })
}

export function IntelMarkdown({ children, entityNames, className }: IntelMarkdownProps) {
  const openPanel = useWorkspaceStore((s) => s.openPanel)
  const select = useSelectionStore((s) => s.select)

  // Pre-process the markdown to convert reference patterns
  const processed = useMemo(() => preprocessRefs(children), [children])

  // Custom link renderer to handle intel:// links
  const components = useMemo(
    () => ({
      a: ({ href, children: linkChildren, ...props }: React.AnchorHTMLAttributes<HTMLAnchorElement> & { children?: React.ReactNode }) => {
        if (href?.startsWith('intel://')) {
          const match = href.match(/^intel:\/\/(sig|evt)\/(.+)$/)
          if (match) {
            const [, refType, uuid] = match
            const handleClick = (e: React.MouseEvent) => {
              e.preventDefault()
              e.stopPropagation()
              if (refType === 'evt') {
                select({ type: 'event', id: uuid, name: `Event ${uuid.slice(0, 8)}` })
                openPanel('event-detail', { id: uuid })
              } else if (refType === 'sig') {
                // No signal-detail panel — open signals panel with filter
                openPanel('signals', { q: uuid })
              }
            }
            return (
              <span
                className="cursor-pointer text-cyan-400 hover:text-cyan-300 hover:underline font-mono text-[0.85em]"
                onClick={handleClick}
                title={`Open ${refType.toUpperCase()}:${uuid}`}
              >
                {linkChildren}
              </span>
            )
          }
        }
        // Normal external links
        return (
          <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
            {linkChildren}
          </a>
        )
      },
      // Custom strong renderer for entity linking
      strong: ({ children: strongChildren, ...props }: React.HTMLAttributes<HTMLElement> & { children?: React.ReactNode }) => {
        if (entityNames && typeof strongChildren === 'string') {
          const lookup = entityNames.get(strongChildren.toLowerCase())
          if (lookup) {
            const handleClick = (e: React.MouseEvent) => {
              e.preventDefault()
              e.stopPropagation()
              select({ type: 'entity', id: lookup.id, name: lookup.name })
              openPanel('entity-detail', { id: lookup.id })
            }
            return (
              <strong
                className="cursor-pointer text-blue-400 hover:text-blue-300 hover:underline"
                onClick={handleClick}
                title={`View entity: ${lookup.name}`}
                {...props}
              >
                {strongChildren}
              </strong>
            )
          }
        }
        // If children is an array with a single string child, try that too
        if (entityNames && Array.isArray(strongChildren) && strongChildren.length === 1 && typeof strongChildren[0] === 'string') {
          const text = strongChildren[0] as string
          const lookup = entityNames.get(text.toLowerCase())
          if (lookup) {
            const handleClick = (e: React.MouseEvent) => {
              e.preventDefault()
              e.stopPropagation()
              select({ type: 'entity', id: lookup.id, name: lookup.name })
              openPanel('entity-detail', { id: lookup.id })
            }
            return (
              <strong
                className="cursor-pointer text-blue-400 hover:text-blue-300 hover:underline"
                onClick={handleClick}
                title={`View entity: ${lookup.name}`}
                {...props}
              >
                {strongChildren}
              </strong>
            )
          }
        }
        return <strong {...props}>{strongChildren}</strong>
      },
    }),
    [entityNames, openPanel, select]
  )

  return (
    <div className={className}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {processed}
      </ReactMarkdown>
    </div>
  )
}
