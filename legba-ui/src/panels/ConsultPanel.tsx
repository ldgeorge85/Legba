import { useState, useRef, useEffect } from 'react'
import { Send, Trash2 } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

interface Message {
  role: 'user' | 'assistant' | 'system'
  content: string
}

export function ConsultPanel() {
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
  }, [messages])

  async function handleSend() {
    if (!input.trim() || loading) return
    const content = input.trim()
    setInput('')
    setMessages((prev) => [...prev, { role: 'user', content }])
    setLoading(true)

    try {
      const res = await fetch('/consult/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: content }),
      })
      const data = await res.json()
      setMessages((prev) => [...prev, { role: 'assistant', content: data.response }])
    } catch {
      setMessages((prev) => [...prev, { role: 'system', content: 'Failed to send message' }])
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
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
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
