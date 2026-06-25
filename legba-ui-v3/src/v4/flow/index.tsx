/**
 * The Flow — composed by the orchestrator from the Wave-1 Track B agents'
 * file-disjoint pieces (projection / canvas+edit / telemetry / wiring). The
 * registry is projected → laid out (ELK) → rendered; telemetry overlays live.
 */
import { useState, useEffect } from 'react'
import { Plus } from 'lucide-react'
import { useGraphProjection } from './projection'
import { layoutGraph } from './layout'
import FlowCanvas from './FlowCanvas'
import { useFlowTelemetry } from './useFlowTelemetry'
import WiringModal from './WiringModal'
import type { GraphProjection } from './types'

export default function FlowRoom() {
  const { projection, isLoading } = useGraphProjection()
  const [laid, setLaid] = useState<GraphProjection | undefined>()
  const [wiringOpen, setWiringOpen] = useState(false)

  useFlowTelemetry(laid)

  useEffect(() => {
    let live = true
    if (projection) {
      layoutGraph(projection).then((p) => {
        if (live) setLaid(p)
      })
    }
    return () => {
      live = false
    }
  }, [projection])

  return (
    <div className="h-full w-full relative bg-surface-300">
      {laid ? (
        <FlowCanvas projection={laid} />
      ) : (
        <div className="h-full flex items-center justify-center text-slate-500 text-sm">
          {isLoading ? 'Projecting the registry graph…' : 'No descriptors registered.'}
        </div>
      )}
      <button
        type="button"
        onClick={() => setWiringOpen(true)}
        className="absolute top-3 right-3 z-10 flex items-center gap-1 bg-surface-50 hover:bg-surface-100 text-slate-200 text-xs px-3 py-1.5 rounded border border-slate-700"
      >
        <Plus size={14} /> Wire
      </button>
      <WiringModal open={wiringOpen} onClose={() => setWiringOpen(false)} />
    </div>
  )
}
