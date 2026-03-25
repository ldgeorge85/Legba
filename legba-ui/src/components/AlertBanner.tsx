import { useCallback, useEffect, useRef, useState } from 'react'
import type { AlertNotification } from '@/api/types'

const SEVERITY_STYLES: Record<AlertNotification['severity'], string> = {
  critical: 'bg-red-900/80 border-red-700 text-red-100',
  warning: 'bg-amber-900/80 border-amber-700 text-amber-100',
  info: 'bg-blue-900/80 border-blue-700 text-blue-100',
}

const SEVERITY_ICONS: Record<AlertNotification['severity'], string> = {
  critical: '\u26A0',  // warning sign
  warning: '\u25B2',   // triangle
  info: '\u24D8',      // circled i
}

const AUTO_DISMISS_MS = 60_000

export function AlertBanner() {
  const [alerts, setAlerts] = useState<AlertNotification[]>([])
  const timersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())

  const dismiss = useCallback((id: string) => {
    setAlerts((prev) => prev.filter((a) => a.id !== id))
    const timer = timersRef.current.get(id)
    if (timer) {
      clearTimeout(timer)
      timersRef.current.delete(id)
    }
  }, [])

  const addAlert = useCallback((alert: AlertNotification) => {
    setAlerts((prev) => {
      // Deduplicate by id
      if (prev.some((a) => a.id === alert.id)) return prev
      return [alert, ...prev]
    })

    // Auto-dismiss after 60 seconds
    const timer = setTimeout(() => {
      dismiss(alert.id)
    }, AUTO_DISMISS_MS)
    timersRef.current.set(alert.id, timer)
  }, [dismiss])

  // Subscribe to SSE alert:fired events
  useEffect(() => {
    const source = new EventSource('/sse/stream')

    const handler = (e: MessageEvent) => {
      try {
        const data = JSON.parse(e.data)
        const alert: AlertNotification = {
          id: data.id ?? crypto.randomUUID(),
          title: data.title ?? 'Alert',
          severity: data.severity ?? 'info',
          message: data.message ?? '',
          timestamp: data.timestamp ?? new Date().toISOString(),
        }
        addAlert(alert)
      } catch {
        // Ignore malformed events
      }
    }

    source.addEventListener('alert:fired', handler)

    return () => {
      source.removeEventListener('alert:fired', handler)
      source.close()
      // Clear all auto-dismiss timers on unmount
      for (const timer of timersRef.current.values()) {
        clearTimeout(timer)
      }
      timersRef.current.clear()
    }
  }, [addAlert])

  if (alerts.length === 0) return null

  return (
    <div className="flex flex-col gap-1 px-2 py-1 shrink-0">
      {alerts.map((alert) => (
        <div
          key={alert.id}
          className={`flex items-start gap-2 px-3 py-2 rounded border text-xs ${SEVERITY_STYLES[alert.severity]}`}
        >
          <span className="text-sm leading-none mt-0.5">{SEVERITY_ICONS[alert.severity]}</span>
          <div className="flex-1 min-w-0">
            <span className="font-semibold mr-2">{alert.title}</span>
            <span className="opacity-90">{alert.message}</span>
          </div>
          <span className="text-[10px] opacity-60 whitespace-nowrap">
            {new Date(alert.timestamp).toLocaleTimeString()}
          </span>
          <button
            onClick={() => dismiss(alert.id)}
            className="ml-1 text-sm leading-none opacity-60 hover:opacity-100 transition-opacity"
            title="Dismiss"
          >
            &times;
          </button>
        </div>
      ))}
    </div>
  )
}
