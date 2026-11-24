/**
 * phoneState — the phone shell's own small state (M-2): the screen the
 * address names (read live from the URL, never copied), the Watch segment,
 * the screen the person came from before Consult, and what Consult is
 * asked about. Nothing here is a copy of a shared store: the point is
 * `state/selection`, the window `useWorldState`, the record `lib/recordSheet`.
 */
import { useSyncExternalStore } from 'react'
import { create } from 'zustand'
import type { PhoneAsking } from '@/mobile/mobileConsult'
import { PHONE_NAV_EVENT, phoneScreenOf, type PhoneScreen } from './phoneAddress'

function subscribe(fn: () => void): () => void {
  window.addEventListener('popstate', fn)
  window.addEventListener('hashchange', fn)
  window.addEventListener(PHONE_NAV_EVENT, fn)
  return () => {
    window.removeEventListener('popstate', fn)
    window.removeEventListener('hashchange', fn)
    window.removeEventListener(PHONE_NAV_EVENT, fn)
  }
}

const readHash = () => window.location.hash

/** The current hash, re-read on every navigation. */
export function useHash(): string {
  return useSyncExternalStore(subscribe, readHash, readHash)
}

/** The screen the address names now. */
export function usePhoneScreen(): PhoneScreen {
  const hash = useHash()
  return phoneScreenOf(hash)
}

export function currentScreen(): PhoneScreen {
  return phoneScreenOf(window.location.hash)
}

export type WatchSegment = 'alerts' | 'silences' | 'edits'

interface PhoneShellState {
  watchSegment: WatchSegment
  /** The last screen that was not Consult (Consult's "what's on screen"). */
  lastScreen: PhoneScreen | null
  /**
   * What "Ask about this" pinned: the desktop's pins (`Selection`s, sent through
   * `questionWithPins` / `pinnedContext`) and the line the Consult screen shows
   * above its composer. Cleared by its ×.
   */
  asking: PhoneAsking | null
}

export const usePhoneShell = create<PhoneShellState>(() => ({
  watchSegment: 'alerts',
  lastScreen: null,
  asking: null,
}))
