import { useState, useRef, useEffect } from 'react'
import { Send, Trash2, Wrench, ChevronDown, ChevronRight, Eye } from 'lucide-react'
import { IntelMarkdown } from '@/components/IntelMarkdown'
import { useSelectionStore } from '@/stores/selection'

interface ToolCall {
  tool: string
  args: Record<string, unknown>
  result?: string
}

interface Message {
  role: 'user' | 'assistant' | 'system'
  content: string
  /** Tool calls made during this response (populated if backend returns them) */
  toolCalls?: ToolCall[]
}

function ToolActivity({ toolCalls }: { toolCalls: ToolCall[] }) {
  const [expanded, setExpanded] = useState(false)

  if (toolCalls.length === 0) return null

  return (
    <div className="mt-2 border-t border-border/30 pt-2">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1.5 text-[11px] text-muted-foreground hover:text-foreground transition-colors"
      >
        <Wrench size={10} />
        <span>Tools used: {toolCalls.length}</span>
        {expanded ? <ChevronDown size={10} /> : <ChevronRight size={10} />}
      </button>
      {expanded && (
        <div className="mt-1.5 space-y-1">
          {toolCalls.map((tc, i) => (
            <div key={i} className="text-[11px] text-muted-foreground bg-secondary/30 rounded px-2 py-1.5">
              <span className="font-mono text-foreground/70">{tc.tool}</span>
              {tc.args && Object.keys(tc.args).length > 0 && (
                <span className="ml-1.5 text-muted-foreground/70">
                  ({Object.entries(tc.args)
                    .slice(0, 3)
                    .map(([k, v]) => `${k}=${typeof v === 'string' ? v.slice(0, 30) : JSON.stringify(v)}`)
                    .join(', ')}
                  {Object.keys(tc.args).length > 3 ? ', ...' : ''})
                </span>
              )}
              {tc.result && (
                <div className="mt-1 text-muted-foreground/60 truncate">
                  {tc.result.slice(0, 100)}{tc.result.length > 100 ? '...' : ''}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export function ConsultPanel() {
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)

  // 6d.1: Context awareness — subscribe to selection store
  const selected = useSelectionStore((s) => s.selected)
  const focusSituation = useSelectionStore((s) => s.focusSituation)

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
  }, [messages])

  // 6d.1: Build invisible context prefix for the backend
  const buildContextPrefix = () => {
    const parts: string[] = []
    if (selected?.type === 'entity') parts.push(`[Context: viewing entity "${selected.name}"]`)
    if (selected?.type === 'event') parts.push(`[Context: viewing event "${selected.name}"]`)
    if (focusSituation) parts.push(`[Context: focused on situation ${focusSituation}]`)
    return parts.length > 0 ? parts.join(' ') + '\n' : ''
  }

  // 6d.5: Derive context label for the header indicator
  const contextLabel = (() => {
    const parts: string[] = []
    if (selected?.type === 'entity') parts.push(`${selected.name} (entity)`)
    else if (selected?.type === 'event') parts.push(`${selected.name} (event)`)
    else if (selected?.type === 'situation') parts.push(`${selected.name} (situation)`)
    if (focusSituation && selected?.type !== 'situation') parts.push(`situation ${focusSituation.slice(0, 8)}...`)
    return parts.length > 0 ? parts.join(' + ') : null
  })()

  async function handleSend() {
    if (!input.trim() || loading) return
    const content = input.trim()
    setInput('')
    // Show the user's message as-is (no context prefix in UI)
    setMessages((prev) => [...prev, { role: 'user', content }])
    setLoading(true)

    // 6d.1: Prepend context prefix to the message sent to the backend (invisible to user)
    const contextPrefix = buildContextPrefix()
    const messageForBackend = contextPrefix + content

    try {
      let res: Response | null = null
      for (let attempt = 0; attempt < 3; attempt++) {
        try {
          res = await fetch('/consult/send', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: messageForBackend }),
          })
          if (res.ok) break
        } catch {
          if (attempt < 2) await new Promise((r) => setTimeout(r, 1000 * (attempt + 1)))
        }
      }
      if (res?.ok) {
        const data = await res.json()
        const assistantMsg: Message = {
          role: 'assistant',
          content: data.response ?? data.error ?? 'No response',
        }

        // 6d.3: Extract tool calls if the backend returns them
        // TODO: Backend /consult/send needs to include tool_calls in the response JSON.
        // Expected format: { response: "...", tool_calls: [{ tool: "name", args: {...}, result: "..." }] }
        if (data.tool_calls && Array.isArray(data.tool_calls)) {
          assistantMsg.toolCalls = data.tool_calls.map((tc: { tool?: string; name?: string; args?: Record<string, unknown>; arguments?: Record<string, unknown>; result?: string }) => ({
            tool: tc.tool ?? tc.name ?? 'unknown',
            args: tc.args ?? tc.arguments ?? {},
            result: tc.result,
          }))
        }

        setMessages((prev) => [...prev, assistantMsg])
      } else {
        setMessages((prev) => [...prev, { role: 'system', content: `Error: ${res?.status ?? 'connection failed'} — retries exhausted` }])
      }
    } catch {
      setMessages((prev) => [...prev, { role: 'system', content: 'Connection failed after 3 attempts' }])
    } finally {
      setLoading(false)
    }
  }

  async function handleClear() {
    await fetch('/consult/session', { method: 'DELETE' })
    setMessages([])
  }

  return (
    <div className="flex flex-col h-full">
      {/* 6d.5: Context awareness indicator */}
      {contextLabel && (
        <div className="flex items-center gap-1.5 px-3 py-1.5 bg-primary/5 border-b border-border text-[11px] text-muted-foreground shrink-0">
          <Eye size={10} className="text-primary/60" />
          <span className="text-muted-foreground/70">Viewing:</span>
          <span className="text-foreground/80 font-medium truncate">{contextLabel}</span>
        </div>
      )}

      {/* Messages */}
      <div ref={scrollRef} className="flex-1 overflow-auto p-3 space-y-3">
        {messages.length === 0 && (
          <div className="text-sm text-muted-foreground text-center mt-8">
            Ask questions about your intelligence data
          </div>
        )}
        {messages.map((msg, i) => (
          <div key={i}>
            {msg.role === 'user' ? (
              <div className="ml-8 bg-primary/10 rounded-lg p-3 text-sm text-foreground">
                {msg.content}
              </div>
            ) : msg.role === 'system' ? (
              <div className="text-center text-muted-foreground text-xs">
                {msg.content}
              </div>
            ) : (
              <div className="mr-8 bg-card border border-border rounded-lg p-3
                prose prose-invert prose-sm max-w-none
                prose-headings:text-foreground prose-headings:font-semibold
                prose-h1:text-base prose-h2:text-sm prose-h3:text-sm
                prose-p:text-foreground/85 prose-p:leading-relaxed prose-p:my-1.5
                prose-strong:text-foreground
                prose-li:text-foreground/85 prose-li:my-0.5
                prose-ul:my-1.5 prose-ol:my-1.5
                prose-table:text-xs
                prose-th:text-foreground prose-th:bg-secondary/50 prose-th:px-2 prose-th:py-1
                prose-td:px-2 prose-td:py-1 prose-td:border-border/30
                prose-a:text-primary
                prose-code:text-primary/80 prose-code:bg-secondary prose-code:px-1 prose-code:py-0.5 prose-code:rounded prose-code:text-xs
                prose-blockquote:border-primary/30 prose-blockquote:text-muted-foreground"
              >
                <IntelMarkdown>{msg.content}</IntelMarkdown>
                {/* 6d.3: Tool call visibility */}
                {msg.toolCalls && msg.toolCalls.length > 0 && (
                  <ToolActivity toolCalls={msg.toolCalls} />
                )}
              </div>
            )}
          </div>
        ))}
        {loading && (
          <div className="mr-8 bg-card border border-border rounded-lg p-3 text-sm text-muted-foreground animate-pulse">
            Thinking...
          </div>
        )}
      </div>

      {/* Input */}
      <div className="flex items-center gap-2 p-2 border-t border-border shrink-0">
        <button onClick={handleClear} className="p-1.5 rounded hover:bg-secondary text-muted-foreground" title="Clear session">
          <Trash2 size={14} />
        </button>
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleSend()}
          placeholder="Ask a question..."
          className="flex-1 px-3 py-1.5 text-sm bg-secondary border border-border rounded focus:outline-none focus:ring-1 focus:ring-primary"
        />
        <button
          onClick={handleSend}
          disabled={!input.trim() || loading}
          className="p-1.5 rounded bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-30"
        >
          <Send size={14} />
        </button>
      </div>
    </div>
  )
}
