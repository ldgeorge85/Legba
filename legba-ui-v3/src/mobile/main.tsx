/**
 * The mobile entry point — the phone shell (M-2).
 *
 * Mirrors `src/main.tsx`'s provider shape (its own `QueryClient` with the
 * workstation's defaults — no private stale time — and `PreferencesProvider`
 * because shared components read the density context) but mounts the phone
 * shell (`shell/PhoneShell.tsx`) instead of the Dockview workstation.
 * Authentication is the workstation's: the registry bearer lives in
 * `localStorage.legba_token` and `lib/api.ts` reads it on every call, the
 * session cookie rides same-origin, Caddy's `basic_auth` perimeter fronts
 * `/m/*` exactly as it does the workstation — and (X4/D4) `SignInGate` asks
 * `GET /auth/me` before the page renders, so a 401 shows the sign-in screen.
 *
 * `globals.css` is imported for the design tokens and Tailwind base;
 * `mobile.css` styles the phone pieces the shell still reuses (the read view,
 * the Consult sheet); `shell/phone.css` is the shell's own layer.
 *
 * Nothing under `src/mobile/` may import the desktop frame or its heavy
 * libraries (`tools/lint-phone-imports.mjs`, run in the UI gate).
 */

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { PreferencesProvider } from '@/components/density/PreferencesProvider'
import { SignInGate } from '@/auth/SignInGate'
import PhoneShell from './shell/PhoneShell'
import { PwaBanners } from './pwa/PwaBanners'
import { registerPhoneWorker } from './pwa/register'
import '@/globals.css'
import './mobile.css'
import './shell/phone.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // The workstation's defaults (plan §3.3: the phone keeps no private
      // cache policy). The phone's lists refresh by hand, not on focus.
      staleTime: 30_000,
      retry: 1,
      refetchOnWindowFocus: false,
      // P-3 — offline, a read FAILS (and the screen says so) instead of
      // pausing silently behind "checking sign-in…"; back online, the
      // banners refresh everything on screen.
      networkMode: 'always',
    },
  },
})

// P-1 — the installable app's service worker (scope /m/, the shell only).
void registerPhoneWorker()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <PreferencesProvider>
        {/* P-1 / P-3: offline, update and install lines — above the sign-in
            gate, so the sign-in screen says "no connection" too. */}
        <PwaBanners />
        {/* X4/D4 (W11-F76's phone half): the same door as the workstation — a
            session that ended, or a phone that never signed in, is shown the
            sign-in screen instead of a page of bare 401s. */}
        <SignInGate>
          <PhoneShell />
        </SignInGate>
      </PreferencesProvider>
    </QueryClientProvider>
  </StrictMode>,
)
