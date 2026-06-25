/**
 * modality -> renderer registry — the UI half of the modality -> {extractor,
 * renderer} registry (DESIGN §7.5), mirroring the ingest-side
 * `default_extractor_registry()`.
 *
 * Resolution is most-specific-first: an exact `mime_type` entry wins over the
 * coarse `modality`, with `default` as the fallback (same shape as the backend
 * subprovider/extractor resolution).
 *
 * TODAY only `text` is a "real" renderer (the panel's title/body carries it);
 * every other modality is a PLACEHOLDER that surfaces the modality badge + a
 * link to the referenced media. Wiring a real renderer — a MapLibre map for
 * `application/geo+json`, an `<audio>`/`<video>` player, an `<img>` + OCR — is a
 * drop-in: set `implemented: true` and add the component here. No schema change,
 * no call-site change. (GIS / `structured` is the model-free first candidate.)
 */

export interface ModalityNode {
  modality?: string | null
  mime_type?: string | null
  media_ref?: string | null
  canonical_url?: string | null
}

export interface ModalityRenderer {
  /** Human label for the modality badge. */
  label: string
  /** Tailwind classes for the badge chip. */
  badgeClass: string
  /** `text` is the default modality and shows no badge (title carries it). */
  showBadge: boolean
  /** True once a real renderer is wired; false = placeholder. */
  implemented: boolean
  /** What a real renderer will be, shown as the placeholder hint. */
  pending?: string
}

const MEDIA_BADGE = 'bg-sky-950 text-sky-300'
const STRUCTURED_BADGE = 'bg-emerald-950 text-emerald-300'
const NEUTRAL_BADGE = 'bg-slate-800 text-slate-300'

export const MODALITY_RENDERERS: Record<string, ModalityRenderer> = {
  // Coarse modality axis.
  text: { label: 'text', badgeClass: NEUTRAL_BADGE, showBadge: false, implemented: true },
  audio: { label: 'audio', badgeClass: MEDIA_BADGE, showBadge: true, implemented: false, pending: 'audio player + transcript' },
  video: { label: 'video', badgeClass: MEDIA_BADGE, showBadge: true, implemented: false, pending: 'video player + transcript' },
  image: { label: 'image', badgeClass: MEDIA_BADGE, showBadge: true, implemented: false, pending: 'image viewer + OCR' },
  structured: { label: 'structured', badgeClass: STRUCTURED_BADGE, showBadge: true, implemented: false, pending: 'structured / map view' },
  binary: { label: 'binary', badgeClass: NEUTRAL_BADGE, showBadge: true, implemented: false, pending: 'download' },
  // Fine mime axis (most-specific-first) — GIS is the model-free first target.
  'application/geo+json': { label: 'geo+json', badgeClass: STRUCTURED_BADGE, showBadge: true, implemented: false, pending: 'map' },
  // Fallback.
  default: { label: 'unknown', badgeClass: NEUTRAL_BADGE, showBadge: true, implemented: false, pending: 'renderer' },
}

/** Resolve the renderer for a node — mime_type, then modality, then default. */
export function resolveModalityRenderer(
  modality?: string | null,
  mimeType?: string | null,
): ModalityRenderer {
  if (mimeType && MODALITY_RENDERERS[mimeType]) return MODALITY_RENDERERS[mimeType]
  if (modality && MODALITY_RENDERERS[modality]) return MODALITY_RENDERERS[modality]
  return MODALITY_RENDERERS.default
}

/**
 * Compact modality + source-link surface for a node (lineage list, signal row).
 * Renders the registry-driven modality badge (for non-`text` modalities) plus a
 * link to the acquisition source / media. Returns null when there's nothing to
 * show (a body-less `text` node). The badge `title` flags the pending renderer.
 */
export function ModalityRef({
  node,
  className = '',
}: {
  node: ModalityNode
  className?: string
}) {
  const url = node.canonical_url || node.media_ref || null
  const r = resolveModalityRenderer(node.modality, node.mime_type)
  const showBadge = r.showBadge && !!node.modality
  if (!url && !showBadge) return null
  return (
    <div className={`flex flex-wrap items-baseline gap-2 ${className}`}>
      {showBadge && (
        <span
          className={`shrink-0 rounded px-1 text-[10px] ${r.badgeClass}`}
          title={r.implemented ? undefined : `${r.pending} renderer pending`}
        >
          {node.mime_type ? `${node.modality} · ${node.mime_type}` : node.modality}
        </span>
      )}
      {url && (
        <a
          href={url}
          target="_blank"
          rel="noopener noreferrer"
          onClick={(e) => e.stopPropagation()}
          className="text-sky-400 hover:text-sky-300 underline truncate max-w-full"
          title={url}
        >
          {node.canonical_url ? 'source ↗' : 'media ↗'}
        </a>
      )}
    </div>
  )
}
