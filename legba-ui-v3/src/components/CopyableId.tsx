/**
 * CopyableId — truncated raw-id display + click-to-copy, for product surfaces
 * that must show a UUID/SHA (U-5: the Inspector header finding id, the export
 * preview's basket-item id fallback). Full id lives in `title` (mouse hover)
 * and is always what gets copied — only the DISPLAY is truncated.
 */
import { useState } from 'react'
import { Copy, Check } from 'lucide-react'
import { truncateId } from '@/lib/idDisplay'

export function CopyableId({
  id,
  className,
  testId = 'copyable-id',
}: {
  id: string
  className?: string
  testId?: string
}) {
  const [copied, setCopied] = useState(false)

  async function copy() {
    try {
      await navigator.clipboard.writeText(id)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1200)
    } catch {
      // Clipboard API unavailable (permissions/context) — no crash, no feedback.
    }
  }

  return (
    <button
      type="button"
      onClick={copy}
      title={copied ? 'copied' : `${id} — click to copy`}
      className={`inline-flex items-center gap-1 rounded font-mono hover:text-ink-1 ${className ?? ''}`}
      data-testid={testId}
      data-copied={copied || undefined}
    >
      <span className="truncate">{truncateId(id)}</span>
      {copied ? (
        <Check className="h-3 w-3 shrink-0" aria-hidden />
      ) : (
        <Copy className="h-3 w-3 shrink-0 opacity-60" aria-hidden />
      )}
    </button>
  )
}
