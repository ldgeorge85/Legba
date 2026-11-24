import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App } from './App'
import { SignInGate } from './auth/SignInGate'
import { PreferencesProvider } from './components/density/PreferencesProvider'
import { redirectPhoneToMobile } from './lib/phoneSurface'
import { installFetchLog } from './lib/queryLayer'
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

// U-5: every query fetch is attributed to the regions observing it, so the
// browser walk can fail on a fetch made for a region nobody can see.
installFetchLog(queryClient)

// X4/D4: a phone-width browser is sent to the phone surface (`/m/`) before the
// desk mounts; `?desktop=1` keeps it here.
if (!redirectPhoneToMobile()) {
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <PreferencesProvider>
          {/* X3-B3: the sign-in gate — the shell renders only for a caller the
              registry accepts (the perimeter operator passes as before). */}
          <SignInGate>
            <App />
          </SignInGate>
        </PreferencesProvider>
      </QueryClientProvider>
    </StrictMode>,
  )
}
