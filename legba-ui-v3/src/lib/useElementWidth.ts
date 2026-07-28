/**
 * useElementWidth — container-width measurement via ResizeObserver, with the
 * CALLBACK-REF pattern so the observer follows the ELEMENT, not the mount.
 *
 * The trap this exists to kill (the system.timeline first-mount blank): a
 * plain `useRef` + `useLayoutEffect(..., [])` observes whatever occupies the
 * ref slot when the effect first runs. A panel that renders a loading
 * empty-state first has NOTHING in the slot at that moment — when the data
 * lands and the measured element finally mounts, the []-deps effect never
 * re-runs, the element is never observed, and the width sticks at 0 (so an
 * SVG sized off it never opens). The callback ref below runs on every
 * attach/detach of the element itself, re-pointing the observer each time.
 *
 * Usage: `const [ref, width] = useElementWidth<HTMLDivElement>()` then
 * `<div ref={ref} …>` — identical call-site shape to the old hook.
 */
import { useCallback, useEffect, useRef, useState } from 'react'

export function useElementWidth<T extends HTMLElement>(): [
  (el: T | null) => void,
  number,
] {
  const [w, setW] = useState(0)
  const roRef = useRef<ResizeObserver | null>(null)

  // React calls the callback ref with the element on mount and null on
  // unmount — exactly the attach/detach lifecycle the observer needs.
  const attach = useCallback((el: T | null) => {
    roRef.current?.disconnect()
    roRef.current = null
    if (!el) return
    // Seed BEFORE observing: real ResizeObserver delivery is async (pre-paint)
    // so the order is equivalent in a browser, but an observer that delivers
    // synchronously on observe() (test fakes, polyfills) must not have its
    // real measurement clobbered by this layout-read seed.
    setW(el.clientWidth)
    const ro = new ResizeObserver((entries) => {
      const cr = entries[0]?.contentRect
      if (cr) setW(cr.width)
    })
    ro.observe(el)
    roRef.current = ro
  }, [])

  // Belt-and-braces teardown for the case where the component unmounts
  // without React invoking the ref with null (e.g. an error boundary).
  useEffect(
    () => () => {
      roRef.current?.disconnect()
      roRef.current = null
    },
    [],
  )

  return [attach, w]
}
