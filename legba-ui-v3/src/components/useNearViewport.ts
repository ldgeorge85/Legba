/**
 * useNearViewport — latches true once the element has come within `margin`
 * of the viewport (U-5). A band far down a long read (the Morning Read's GAPS
 * band, one `/v3/absence` read per desk) gates its reads on this, so the
 * landing does not fetch what nobody has scrolled to.
 *
 * `armed` false holds the observer off while the content above is still
 * loading. Where `IntersectionObserver` does not exist (jsdom, an old engine) the
 * answer is true at once: the reads go out as they did before.
 */
import { useEffect, useState, type RefObject } from 'react'

export function useNearViewport(
  ref: RefObject<Element | null>,
  { margin = '200px', armed = true }: { margin?: string; armed?: boolean } = {},
): boolean {
  const [near, setNear] = useState<boolean>(() => typeof IntersectionObserver === 'undefined')
  useEffect(() => {
    // Not armed: the content above is still loading, so the element sits
    // higher than it will once that content lands (a band under two loading
    // lines is "in view" for a moment it never really is).
    if (near || !armed) return
    const el = ref.current
    if (!el || typeof IntersectionObserver === 'undefined') {
      setNear(true)
      return
    }
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) setNear(true)
      },
      { rootMargin: `${margin} 0px ${margin} 0px` },
    )
    io.observe(el)
    return () => io.disconnect()
  }, [near, ref, margin, armed])
  return near
}
