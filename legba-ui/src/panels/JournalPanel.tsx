import { useState } from 'react'
import { useJournal } from '@/api/hooks'
import { TimeAgo } from '@/components/common/TimeAgo'
import { Download } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

function downloadMarkdown(filename: string, content: string) {
  const blob = new Blob([content], { type: 'text/markdown' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

type ViewItem = {
  id: string
  label: string
  sublabel?: string
  timestamp?: string
  content: string
  type: 'consolidation' | 'entry'
}

export function JournalPanel() {
  const { data, isLoading } = useJournal()
  const [selectedId, setSelectedId] = useState<string | null>(null)

  if (isLoading) return <div className="p-4 text-sm text-muted-foreground">Loading...</div>
  if (!data?.entries?.length && !data?.consolidation)
    return <div className="p-4 text-sm text-muted-foreground">No journal entries</div>

  // Build unified list: consolidation first, then cycle entries
  const items: ViewItem[] = []

  if (data.consolidation) {
    items.push({
      id: 'consolidation',
      label: 'Consolidation',
      sublabel: data.consolidation_cycle ? `Cycle ${data.consolidation_cycle}` : undefined,
      timestamp: data.consolidation_timestamp,
      content: data.consolidation,
      type: 'consolidation',
    })
  }

  const sortedEntries = [...(data.entries ?? [])].sort((a, b) => b.cycle - a.cycle)
  for (const entry of sortedEntries) {
    const lines = entry.entries ?? (entry.content ? [entry.content] : [])
    const md = lines.map((l: string) => `- ${l}`).join('\n')
    items.push({
      id: `cycle-${entry.cycle}`,
      label: `Cycle ${entry.cycle}`,
      timestamp: entry.timestamp,
      content: md,
      type: 'entry',
    })
  }

  // Auto-select first item
  const activeId = selectedId ?? (items.length > 0 ? items[0].id : null)
  const selected = activeId ? items.find((i) => i.id === activeId) : null

  function buildDownloadContent(item: ViewItem): string {
    const header = item.type === 'consolidation'
      ? `# Journal Consolidation\n\n*${item.timestamp ?? ''}*\n\n`
      : `# Journal — ${item.label}\n\n*${item.timestamp ?? ''}*\n\n`
    return header + item.content
  }

  return (
    <div className="flex h-full">
      {/* Entry list */}
      <div className="w-56 shrink-0 border-r border-border overflow-auto">
        <div className="space-y-0.5 p-1">
          {items.map((item) => (
            <button
              key={item.id}
              onClick={() => setSelectedId(item.id)}
              className={`w-full text-left px-3 py-2 rounded text-sm transition-colors ${
                activeId === item.id
                  ? 'bg-primary/10 text-primary'
                  : 'hover:bg-secondary text-foreground'
              }`}
            >
              <p className="font-medium truncate">
                {item.type === 'consolidation' ? '📋 ' : ''}{item.label}
              </p>
              {item.sublabel && (
                <p className="text-[10px] text-muted-foreground">{item.sublabel}</p>
              )}
              {item.timestamp && (
                <TimeAgo date={item.timestamp} className="text-[10px] text-muted-foreground" />
              )}
            </button>
          ))}
        </div>
      </div>

      {/* Content pane */}
      <div className="flex-1 overflow-auto">
        {selected ? (
          <>
            <div className="flex items-center justify-between px-4 py-2 border-b border-border shrink-0">
              <span className="text-sm font-medium">
                {selected.type === 'consolidation' ? 'Journal Consolidation' : selected.label}
              </span>
              <button
                onClick={() => downloadMarkdown(
                  `legba_journal_${selected.id}.md`,
                  buildDownloadContent(selected)
                )}
                className="flex items-center gap-1.5 px-2 py-1 text-xs rounded bg-secondary hover:bg-secondary/80 text-muted-foreground hover:text-foreground transition-colors"
                title="Download as Markdown"
              >
                <Download size={12} />
                Download .md
              </button>
            </div>
            <div className="p-6 prose prose-invert prose-sm max-w-none
              prose-headings:text-foreground prose-headings:font-semibold prose-headings:border-b prose-headings:border-border/50 prose-headings:pb-2 prose-headings:mb-3
              prose-h1:text-lg prose-h2:text-base prose-h3:text-sm
              prose-p:text-foreground/85 prose-p:leading-relaxed
              prose-strong:text-foreground
              prose-li:text-foreground/85 prose-li:leading-relaxed
              prose-ul:my-2 prose-ol:my-2
              prose-table:text-xs
              prose-th:text-foreground prose-th:bg-secondary/50 prose-th:px-3 prose-th:py-1.5
              prose-td:px-3 prose-td:py-1.5 prose-td:border-border/30
              prose-hr:border-border/50
              prose-a:text-primary prose-a:no-underline hover:prose-a:underline
              prose-code:text-primary/80 prose-code:bg-secondary prose-code:px-1 prose-code:py-0.5 prose-code:rounded prose-code:text-xs
              prose-blockquote:border-primary/30 prose-blockquote:text-muted-foreground"
            >
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{selected.content}</ReactMarkdown>
            </div>
          </>
        ) : (
          <div className="flex items-center justify-center h-full text-sm text-muted-foreground">
            Select a journal entry to view
          </div>
        )}
      </div>
    </div>
  )
}
