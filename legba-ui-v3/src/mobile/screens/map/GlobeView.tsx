/**
 * The globe, on demand (M-3f): U-4's `GlobeMap` in the phone's map box — the
 * narrow preset is the Board's, without the dock's WebGL overlay harness (the
 * phone has no tile transform to escape, so the canvas sits in the page).
 * Loaded only by "Open the globe": this module is the map library's door.
 */
import GlobeMap from '@/v4/globe/GlobeMap'

export default function GlobeView({ onFallback }: { onFallback: (why: string) => void }) {
  return (
    <div className="ph-globe" data-testid="phone-globe">
      <GlobeMap preset="board" active onFallback={onFallback} />
    </div>
  )
}
