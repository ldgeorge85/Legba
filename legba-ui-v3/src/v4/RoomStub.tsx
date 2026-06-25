/** Placeholder for a v4 room not yet built. Replaced wave-by-wave. */
export function RoomStub({ name, desc }: { name: string; desc: string }) {
  return (
    <div className="h-full w-full flex items-center justify-center bg-surface-300">
      <div className="text-center max-w-md px-6">
        <div className="text-2xl font-bold text-slate-200 mb-2">{name}</div>
        <div className="text-sm text-slate-500">{desc}</div>
      </div>
    </div>
  )
}
