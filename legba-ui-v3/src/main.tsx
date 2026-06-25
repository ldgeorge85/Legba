import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App } from './App'
import { PreferencesProvider } from './components/density/PreferencesProvider'
import './globals.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Panels typically own their own NATS-driven invalidation; staleTime
      // here is the fallback when a panel doesn't subscribe.
      staleTime: 30_000,
      retry: 1,
    },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <PreferencesProvider>
        <App />
      </PreferencesProvider>
    </QueryClientProvider>
  </StrictMode>,
)
