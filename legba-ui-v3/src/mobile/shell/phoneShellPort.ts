/**
 * phoneShellPort — the phone's SHELL PORT (M-2; `lib/applyAction.ts`).
 *
 * `drive` (the executor every producer uses — a link, a cross-link, a
 * Consult case) calls the shell through this port. On the phone:
 *
 *   openSingleton / openBound   push or switch to the screen that shows the
 *                               view (through the frame port's `openView`)
 *   switchStance                a reader surface becomes its tab; an operator
 *                               surface is refused, by name
 *   showRecord                  the Record's peek sheet
 *   applyPreset / runMission /  NOT ON THIS DEVICE: each throws a declared
 *   restoreLayout of a desktop  refusal, which `applyAction` turns into a
 *   arrangement                 `skipped` step carrying the reason — shown on
 *                               the Consult screen, never silently dropped
 *   serializeLayout /           M-3g: the phone's "arrangement" is its screen
 *   restoreLayout of the phone's   (the frame port's snapshot: the address and
 *   own snapshot                the Watch segment), so a case's one Undo
 *                               (`casePlayer.restore`, the snapshot taken
 *                               before step 1) puts the phone back on the
 *                               screen it was on
 */
import type { PanelKind } from '@/types'
import type { ShellPort } from '@/lib/applyAction'
import { registerShell } from '@/lib/applyAction'
import { openRecord } from '@/lib/recordSheet'
import { resolveSurfaceId } from '@/lib/surfaces'
import { openPhoneView, phoneFramePort } from './phoneFramePort'
import { currentScreen } from './phoneState'
import { navigate, tabOfSurface, TAB_SURFACE } from './phoneAddress'

/** The refusal a phone gives for what only the desktop does. */
export const NOT_ON_THIS_DEVICE = 'not on this device: the desktop arranges views, presets and missions'

/** The tag on the phone's own screen snapshot (never a desktop layout). */
const PHONE_LAYOUT = 'phone-screen/1'

interface PhoneLayout {
  kind: typeof PHONE_LAYOUT
  frame: unknown
}

function isPhoneLayout(v: unknown): v is PhoneLayout {
  return !!v && typeof v === 'object' && (v as { kind?: unknown }).kind === PHONE_LAYOUT
}

function notHere(what: string): never {
  throw new Error(`${NOT_ON_THIS_DEVICE} (${what})`)
}

export const phoneShellPort: ShellPort = {
  openSingleton: (kind: PanelKind, tab?: string) => openPhoneView(kind, { tab }) || null,
  openBound: (kind: PanelKind, recordId: string, axis: 'target' | 'analyst', tab?: string) =>
    openPhoneView(kind, { recordId, axis, tab }) || null,
  switchStance: (id: string) => {
    const surface = resolveSurfaceId(id)
    const tab = tabOfSurface(surface)
    if (!tab) notHere(`the ${surface ?? id} page is on the desktop`)
    const cur = currentScreen()
    if (cur.tab !== tab || cur.page || cur.record) navigate({ tab, page: null, record: null, peek: false }, 'replace')
  },
  applyPreset: (id: string) => notHere(`preset ${id}`),
  runMission: (id: string) => notHere(`mission ${id}`),
  // Consult is a tab on the phone: a case keeps running in it.
  reopenConsult: () => {},
  showRecord: (ref) => {
    openRecord(ref, { origin: 'case' })
  },
  currentStance: () => TAB_SURFACE[currentScreen().tab],
  serializeLayout: (): PhoneLayout => ({ kind: PHONE_LAYOUT, frame: phoneFramePort.snapshot() }),
  restoreLayout: (stance: string | null, layout: unknown | null) => {
    if (isPhoneLayout(layout)) {
      phoneFramePort.restore(layout.frame)
      return
    }
    if (layout != null) notHere('a saved arrangement')
    if (stance) phoneShellPort.switchStance(stance)
  },
}

export function registerPhoneShellPort(): () => void {
  return registerShell(phoneShellPort)
}
