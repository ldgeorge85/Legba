import { useState } from 'react'
import { useBriefs } from '@/api/hooks'
import { TimeAgo } from '@/components/common/TimeAgo'
import { BookOpen, ChevronRight } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

export function BriefsPanel() {
  const { data: briefs, isLoading } = useBriefs()
  const [expandedId, setExpandedId] = useState<string | null>(null)

  if (isLoading)
    return <div className="p-4 text-sm text-muted-foreground">Loading...</div>

  if (!briefs?.length)
    return (
      <div className="p-6 text-sm text-muted-foreground text-center">
        No situation briefs yet. SYNTHESIZE cycles produce named briefs every ~2 hours.
      </div>
    )

  // Newest first (API should already sort, but ensure it)
  const sorted = [...briefs].sort((a, b) => b.cycle - a.cycle)

  function toggle(id: string) {
    setExpandedId((prev) => (prev === id ? null : id))
  }

  return (
    <div className="h-full overflow-auto">
      <div className="space-y-2 p-3">
        {sorted.map((brief) => {
          const id = `${brief.cycle}-${brief.title}`
          const isOpen = expandedId === id
          return (
            <div key={id} className="rounded-lg border border-border bg-card">
              <button
                onClick={() => toggle(id)}
                className="w-full flex items-start gap-3 px-4 py-3 text-left hover:bg-secondary/50 transition-colors rounded-lg"
              >
                <ChevronRight
                  size={14}
                  className={`mt-0.5 shrink-0 text-muted-foreground transition-transform ${
                    isOpen ? 'rotate-90' : ''
                  }`}
                />
                <div className="min-w-0 flex-1">
                  <p className="font-semibold text-sm text-foreground truncate">
                    {brief.title}
                  </p>
                  <p className="text-[11px] text-muted-foreground mt-0.5 flex items-center gap-2">
                    <span className="flex items-center gap-1">
                      <BookOpen size={10} />
                      Cycle {brief.cycle}
                    </span>
                    <span>&middot;</span>
                    <TimeAgo date={brief.timestamp} />
                  </p>
                </div>
              </button>

              {isOpen && (
                <div className="border-t border-border px-4 py-4">
                  <div className="prose prose-invert prose-sm max-w-none
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
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{brief.content}</ReactMarkdown>
                  </div>
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
