import { useState, useEffect, useRef, useCallback } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import { ArrowRight, ArrowLeft, Send, RefreshCw } from 'lucide-react'
import { Badge } from '@/components/common/Badge'

interface ThreadMessage {
  id: string
  direction: 'inbound' | 'outbound'
  timestamp: string
  content: string
  priority?: string
  requires_response?: boolean
  cycle_number?: number
  in_reply_to?: string | null
}

interface MessagesResponse {
  messages: ThreadMessage[]
  nats_available: boolean
}

const PRIORITY_COLORS: Record<string, string> = {
  normal: 'bg-secondary text-muted-foreground',
  urgent: 'bg-amber-500/20 text-amber-400',
  directive: 'bg-red-500/20 text-red-400',
}

function formatTime(iso: string): string {
  try {
    const d = new Date(iso)
    return d.toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    })
  } catch {
    return iso
  }
}

export function InboxPanel() {
  const [content, setContent] = useState('')
  const [priority, setPriority] = useState<'normal' | 'urgent' | 'directive'>('normal')
  const [requiresResponse, setRequiresResponse] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)
  const queryClient = useQueryClient()

  const { data, isLoading, refetch } = useQuery({
    queryKey: ['messages'],
    queryFn: () => api.get<MessagesResponse>('/api/v2/messages'),
    refetchInterval: 30_000,
  })

  const sendMutation = useMutation({
    mutationFn: (body: { content: string; priority: string; requires_response: boolean }) =>
      api.post<MessagesResponse>('/api/v2/messages', body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['messages'] })
      setContent('')
      setPriority('normal')
      setRequiresResponse(false)
    },
  })

  // Auto-scroll to bottom when new messages arrive
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [data?.messages?.length])

  const handleSubmit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault()
      if (!content.trim()) return
      sendMutation.mutate({ content: content.trim(), priority, requires_response: requiresResponse })
    },
    [content, priority, requiresResponse, sendMutation],
  )

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault()
        if (content.trim()) {
          sendMutation.mutate({ content: content.trim(), priority, requires_response: requiresResponse })
        }
      }
    },
    [content, priority, requiresResponse, sendMutation],
  )

  const messages = data?.messages ?? []

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-2 p-2 border-b border-border shrink-0">
        <span className="text-sm font-medium">Operator Inbox</span>
        <span className="text-xs text-muted-foreground">
          {messages.length} messages
        </span>
        {data && (
          <Badge className={data.nats_available ? 'bg-green-500/20 text-green-400 ml-1' : 'bg-red-500/20 text-red-400 ml-1'}>
            {data.nats_available ? 'NATS connected' : 'NATS offline'}
          </Badge>
        )}
        <button
          onClick={() => refetch()}
          className="ml-auto p-1 rounded text-muted-foreground hover:text-foreground hover:bg-secondary transition-colors"
          title="Refresh"
        >
          <RefreshCw size={14} />
        </button>
      </div>

      {/* Message thread */}
      <div className="flex-1 overflow-y-auto p-3 space-y-3">
        {isLoading ? (
          <div className="text-sm text-muted-foreground">Loading messages...</div>
        ) : messages.length === 0 ? (
          <div className="text-sm text-muted-foreground text-center py-8">
            No messages yet. Send a message to the agent below.
          </div>
        ) : (
          messages.map((msg) => (
            <div
              key={msg.id}
              className={`flex gap-2 ${msg.direction === 'inbound' ? 'justify-end' : 'justify-start'}`}
            >
              {/* Outbound: agent icon on left */}
              {msg.direction === 'outbound' && (
                <div className="shrink-0 mt-1">
                  <div className="w-6 h-6 rounded-full bg-primary/20 flex items-center justify-center" title="Agent">
                    <ArrowLeft size={12} className="text-primary" />
                  </div>
                </div>
              )}

              <div
                className={`max-w-[80%] rounded-lg px-3 py-2 text-sm ${
                  msg.direction === 'inbound'
                    ? 'bg-primary/15 border border-primary/30'
                    : 'bg-secondary border border-border'
                }`}
              >
                {/* Priority badge for inbound */}
                {msg.direction === 'inbound' && msg.priority && msg.priority !== 'normal' && (
                  <Badge className={`${PRIORITY_COLORS[msg.priority] ?? PRIORITY_COLORS.normal} text-[10px] mb-1`}>
                    {msg.priority}
                  </Badge>
                )}

                {/* Content */}
                <div className="whitespace-pre-wrap break-words">{msg.content}</div>

                {/* Footer */}
                <div className="flex items-center gap-2 mt-1 text-[10px] text-muted-foreground">
                  <span>{formatTime(msg.timestamp)}</span>
                  {msg.direction === 'outbound' && msg.cycle_number != null && msg.cycle_number > 0 && (
                    <span>cycle {msg.cycle_number}</span>
                  )}
                  {msg.direction === 'inbound' && msg.requires_response && (
                    <span className="text-amber-400">response requested</span>
                  )}
                </div>
              </div>

              {/* Inbound: operator icon on right */}
              {msg.direction === 'inbound' && (
                <div className="shrink-0 mt-1">
                  <div className="w-6 h-6 rounded-full bg-blue-500/20 flex items-center justify-center" title="Operator">
                    <ArrowRight size={12} className="text-blue-400" />
                  </div>
                </div>
              )}
            </div>
          ))
        )}
        <div ref={bottomRef} />
      </div>

      {/* Send form */}
      <form onSubmit={handleSubmit} className="border-t border-border p-2 shrink-0">
        <div className="flex items-end gap-2">
          <div className="flex-1 flex flex-col gap-1">
            <textarea
              value={content}
              onChange={(e) => setContent(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Message the agent... (Ctrl+Enter to send)"
              rows={2}
              className="w-full px-3 py-2 text-sm bg-secondary border border-border rounded resize-none focus:outline-none focus:ring-1 focus:ring-primary"
            />
            <div className="flex items-center gap-2">
              <select
                value={priority}
                onChange={(e) => setPriority(e.target.value as 'normal' | 'urgent' | 'directive')}
                className="text-xs bg-secondary border border-border rounded px-2 py-0.5 focus:outline-none"
              >
                <option value="normal">Normal</option>
                <option value="urgent">Urgent</option>
                <option value="directive">Directive</option>
              </select>
              <label className="flex items-center gap-1 text-xs text-muted-foreground cursor-pointer">
                <input
                  type="checkbox"
                  checked={requiresResponse}
                  onChange={(e) => setRequiresResponse(e.target.checked)}
                  className="rounded border-border"
                />
                Requires response
              </label>
            </div>
          </div>
          <button
            type="submit"
            disabled={sendMutation.isPending || !content.trim()}
            className="flex items-center gap-1 px-3 py-2 text-sm bg-primary text-primary-foreground rounded hover:bg-primary/90 disabled:opacity-40 transition-colors"
          >
            <Send size={14} />
            <span>Send</span>
          </button>
        </div>
        {sendMutation.isError && (
          <div className="mt-1 text-xs text-red-400">
            Failed to send message. Check NATS connection.
          </div>
        )}
      </form>
    </div>
  )
}
