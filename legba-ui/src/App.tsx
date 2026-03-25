import { useEffect, useRef, useState, useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Sidebar } from '@/components/layout/Sidebar'
import { StatusBar } from '@/components/layout/StatusBar'
import { Workspace } from '@/components/layout/Workspace'
import { AlertBanner } from '@/components/AlertBanner'
import { SSEClient } from '@/api/sse'
import { CommandPalette } from '@/components/CommandPalette'
import { LoginPage } from '@/components/LoginPage'

export default function App() {
  const queryClient = useQueryClient()
  const sseRef = useRef<SSEClient | null>(null)
  const [authenticated, setAuthenticated] = useState<boolean | null>(null) // null = checking

  // Check auth on mount
  useEffect(() => {
    fetch('/api/v2/auth/me', { credentials: 'include' })
      .then(res => {
        setAuthenticated(res.ok)
      })
      .catch(() => setAuthenticated(false))
  }, [])

  // SSE connection (only when authenticated)
  useEffect(() => {
    if (!authenticated) return

    const client = new SSEClient(queryClient)
    client.connect()
    sseRef.current = client

    return () => {
      client.disconnect()
      sseRef.current = null
    }
  }, [queryClient, authenticated])

  const handleLogin = useCallback(() => {
    setAuthenticated(true)
  }, [])

  // Loading state
  if (authenticated === null) {
    return (
      <div className="flex items-center justify-center h-screen w-screen bg-background">
        <p className="text-muted-foreground">Loading...</p>
      </div>
    )
  }

  // Not authenticated — show login
  if (!authenticated) {
    return <LoginPage onLogin={handleLogin} />
  }

  // Authenticated — show app
  return (
    <div className="flex h-screen w-screen overflow-hidden">
      <Sidebar />
      <div className="flex flex-col flex-1 min-w-0">
        <AlertBanner />
        <Workspace />
        <StatusBar />
      </div>
      <CommandPalette />
    </div>
  )
}
