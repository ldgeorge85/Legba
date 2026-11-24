/**
 * ConsultFrameContext — how the frame is holding Consult (U-6). `compact` is
 * the ask bar alone: the conversation, the model picker and the options fold
 * away, and the line naming the point, the box and its controls stay. `null`
 * outside the frame (Consult as a panel, or in a test): everything shows.
 */
import { createContext, useContext } from 'react'

export interface ConsultFrame {
  compact: boolean
}

const Ctx = createContext<ConsultFrame | null>(null)

export const ConsultFrameProvider = Ctx.Provider

export function useConsultFrame(): ConsultFrame | null {
  return useContext(Ctx)
}
