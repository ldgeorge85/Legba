/**
 * The mobile entry point.
 *
 * Mirrors `src/main.tsx`'s provider shape (its own `QueryClient`, and
 * `PreferencesProvider` because the shared components below it read the
 * density context) but mounts `MobileApp` instead of the Dockview workstation.
 * There is no auth provider to add: the registry bearer lives in
 * `localStorage.legba_token` and `lib/api.ts` reads it on every call, so being
 * served same-origin under `/m/` is the whole of "authenticate the same way" —
 * plus Caddy's `basic_auth` perimeter, which the `/m/*` handle imports exactly
 * as the workstation's handle does.
 *
 * `globals.css` is imported for the design tokens and Tailwind base; `mobile.css`
 * layers the phone surface on top.
 */

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { PreferencesProvider } from '@/components/density/PreferencesProvider'
import MobileApp from './MobileApp'
import '@/globals.css'
import './mobile.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Longer than the workstation's 30s: a phone is often on a metered or
      // intermittent link, and a read does not change between two glances.
      staleTime: 120_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <PreferencesProvider>
        <MobileApp />
      </PreferencesProvider>
    </QueryClientProvider>
  </StrictMode>,
)
