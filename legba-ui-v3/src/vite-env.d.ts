/// <reference types="vite/client" />

/**
 * Build-time env vars consumed via `import.meta.env`.
 *
 * Keep this list narrow — every entry must be prefixed `VITE_` so it
 * gets serialized into the browser bundle. Secrets do NOT belong here.
 */
interface ImportMetaEnv {
  /**
   * Default deployment mode (`personal` | `above_ai` | `cis`).
   * Falls back to `'personal'` if unset. Used by `currentMode()` when
   * neither URL `?mode=` nor a JWT claim resolves the mode.
   */
  readonly VITE_LEGBA_DEFAULT_MODE: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

/**
 * Ambient declaration for `react-cytoscapejs` — the package ships ESM
 * without bundled `.d.ts` and the DefinitelyTyped `@types/react-cytoscapejs`
 * (v1.2.6) is stale relative to the v2.x React-18 release we depend on.
 * The minimal surface used by `panels/target/Graph.tsx` is enough; expand
 * here if other panels start consuming more of the API.
 */
declare module 'react-cytoscapejs' {
  import type { ComponentType, CSSProperties } from 'react'
  import type { Core, ElementDefinition, LayoutOptions, StylesheetStyle } from 'cytoscape'

  export interface CytoscapeComponentProps {
    elements: ElementDefinition[]
    stylesheet?: StylesheetStyle[] | ReadonlyArray<unknown>
    layout?: LayoutOptions | { name: string; [key: string]: unknown }
    style?: CSSProperties
    className?: string
    cy?: (cy: Core) => void
    zoom?: number
    pan?: { x: number; y: number }
    minZoom?: number
    maxZoom?: number
    wheelSensitivity?: number
    boxSelectionEnabled?: boolean
    autoungrabify?: boolean
    autounselectify?: boolean
    userZoomingEnabled?: boolean
    userPanningEnabled?: boolean
  }

  const CytoscapeComponent: ComponentType<CytoscapeComponentProps>
  export default CytoscapeComponent
}
