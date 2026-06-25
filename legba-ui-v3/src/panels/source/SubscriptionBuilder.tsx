/**
 * UI-2 / Tier C — subscription builder.
 *
 * Composes a target's `SourceRef` (src/legba/data/schemas/source.py):
 *   - ref mode: EXPLICIT (a named `source_id`) OR SELECTOR (a `SourceSelector`
 *     over source SCOPE — tags/geo/languages/kinds/tenant + Starlark residual)
 *   - plus a `Subscription` (signal-level slice: structured filter
 *     geo/languages/tags/entity_classes/modalities + Starlark residual +
 *     canonical_only)
 *
 * Live validation (client-side first-pass; the registry re-validates on save):
 *   - structured tokens vs the pydantic patterns (GEO/tag/lang)
 *   - Starlark residual structural lint (single expression, balanced, no stmts)
 *   - exactly-one-of source_id / source_selector (the SourceRef invariant)
 *
 * Preview ("what it'd match"):
 *   - EXPLICIT → resolve the named source (GET /registry/sources/{id})
 *   - SELECTOR → GET /registry/sources, apply the selector's structured filter
 *     client-side → matching source list
 *   - then GET /signals (recent window), apply the Subscription structured
 *     filter → matching-signal preview + a match-rate readout.
 *
 * Output: the composed SourceRef JSON, copy-ready to paste into a target
 * descriptor's `sources: [...]` (the inline DescriptorEditor escape hatch).
 */

import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { ScopePicker } from '@/components/ScopePicker'
import { apiGet } from '@/lib/api'
import type { PanelProps } from '@/types'
import {
  buildSourceRef,
  emptySelector,
  emptySubscription,
  lintSelector,
  lintSourceId,
  lintSubscription,
  parseTokens,
  signalGeo,
  type FieldIssue,
  type SignalRow,
  type SignalsPage,
  type SourceDescriptorOut,
  type SourceSelector,
  type Subscription,
} from './sourceTypes'

type RefMode = 'explicit' | 'selector'

/** Does a source-descriptor row match a SourceSelector's structured fields?
 *  (Mirrors the coarse match the source-selector router does over source
 *  SCOPE — the Starlark residual is server-evaluated, not previewed here.) */
function sourceMatchesSelector(src: SourceDescriptorOut, sel: SourceSelector): boolean {
  if (sel.kinds.length && !(src.kind && sel.kinds.includes(src.kind))) return false
  if (sel.owner_tenant && (src.owner_tenant ?? 'default') !== sel.owner_tenant) return false
  if (sel.tags.length && !sel.tags.every((t) => src.tags.includes(t))) return false
  if (sel.geo.length && !sel.geo.some((g) => src.geo.includes(g))) return false
  if (sel.languages.length && !sel.languages.some((l) => src.languages.includes(l))) return false
  return true
}

/** Does a signal match the Subscription's structured filter? (geo/lang/tags/
 *  entity_classes/modalities — the indexed coarse slice. The residual is
 *  server-evaluated.) The coarse facets are TOP-LEVEL arrays on the signal row
 *  (`geo`/`tags`/`entity_classes`) — precomputed by the ingest pipeline — NOT
 *  nested under `data` (where `data.geo` is the geocode OBJECT, not an array).
 *  `language` is top-level; `modalities` has no first-class column yet, so we
 *  fall back to matching the signal's `category` against the requested set. */
function signalMatchesSubscription(sig: SignalRow, sub: Subscription): boolean {
  if (sub.languages.length && !(sig.language && sub.languages.includes(sig.language))) return false
  if (sub.geo.length) {
    // top-level facet + geocoded country_iso2 fallback
    const iso2 = signalGeo(sig)?.country_iso2
    const g = iso2 ? [...sig.geo, iso2] : sig.geo
    if (!sub.geo.some((x) => g.includes(x))) return false
  }
  if (sub.tags.length) {
    if (!sub.tags.every((x) => sig.tags.includes(x))) return false
  }
  if (sub.entity_classes.length) {
    if (!sub.entity_classes.some((x) => sig.entity_classes.includes(x))) return false
  }
  if (sub.modalities.length) {
    // no first-class modality facet — best-effort against category
    const cat = sig.category
    if (!sub.modalities.some((x) => x === cat)) return false
  }
  return true
}

function TokenField({
  label,
  value,
  onChange,
  placeholder,
  testid,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  placeholder: string
  testid: string
}) {
  return (
    <label className="block">
      <span className="text-slate-500 text-[10px]">{label}</span>
      <input
        className="w-full bg-surface-100 border border-slate-800 rounded p-1 px-2 text-[11px] font-mono"
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        data-testid={testid}
      />
    </label>
  )
}

export default function SubscriptionBuilderPanel({ registration }: PanelProps) {
  const [refMode, setRefMode] = useState<RefMode>('explicit')
  const [sourceId, setSourceId] = useState('')

  // raw text fields → tokens
  const [selTags, setSelTags] = useState('')
  const [selGeo, setSelGeo] = useState('')
  const [selLangs, setSelLangs] = useState('')
  const [selKinds, setSelKinds] = useState('')
  const [selTenant, setSelTenant] = useState('')
  const [selPredicate, setSelPredicate] = useState('')

  const [subGeo, setSubGeo] = useState('')
  const [subLangs, setSubLangs] = useState('')
  const [subTags, setSubTags] = useState('')
  const [subEntity, setSubEntity] = useState('')
  const [subModalities, setSubModalities] = useState('')
  const [subPredicate, setSubPredicate] = useState('')
  const [canonicalOnly, setCanonicalOnly] = useState(true)

  const selector: SourceSelector = useMemo(
    () => ({
      ...emptySelector(),
      tags: parseTokens(selTags),
      geo: parseTokens(selGeo),
      languages: parseTokens(selLangs),
      kinds: parseTokens(selKinds),
      owner_tenant: selTenant.trim() || null,
      predicate: selPredicate.trim() || null,
    }),
    [selTags, selGeo, selLangs, selKinds, selTenant, selPredicate],
  )

  const subscription: Subscription = useMemo(
    () => ({
      ...emptySubscription(),
      geo: parseTokens(subGeo),
      languages: parseTokens(subLangs),
      tags: parseTokens(subTags),
      entity_classes: parseTokens(subEntity),
      modalities: parseTokens(subModalities),
      predicate: subPredicate.trim() || null,
      canonical_only: canonicalOnly,
    }),
    [subGeo, subLangs, subTags, subEntity, subModalities, subPredicate, canonicalOnly],
  )

  // ---- validation ----
  const issues: FieldIssue[] = useMemo(() => {
    const out: FieldIssue[] = []
    if (refMode === 'explicit') {
      const e = lintSourceId(sourceId)
      if (e) out.push({ field: 'source_id', value: sourceId, message: e })
    } else {
      out.push(...lintSelector(selector))
      const hasAny =
        selector.tags.length ||
        selector.geo.length ||
        selector.languages.length ||
        selector.kinds.length ||
        selector.owner_tenant ||
        selector.predicate
      if (!hasAny) {
        out.push({
          field: 'source_selector',
          value: '',
          message: 'selector matches ALL sources — narrow it with at least one field',
        })
      }
    }
    out.push(...lintSubscription(subscription))
    return out
  }, [refMode, sourceId, selector, subscription])

  const valid = issues.filter((i) => i.field !== 'source_selector' || i.value !== '').length === 0
  // a too-broad selector is a warning, not a hard error → still allow preview/build

  const sourceRef = useMemo(
    () => buildSourceRef(refMode, sourceId, selector, subscription),
    [refMode, sourceId, selector, subscription],
  )

  // ---- preview: matching sources ----
  const allSources = useQuery<SourceDescriptorOut[]>({
    queryKey: ['subbuilder-sources'],
    queryFn: () => apiGet<SourceDescriptorOut[]>('/registry/sources?head_only=true&limit=500'),
  })

  const explicitSource = useQuery<SourceDescriptorOut>({
    enabled: refMode === 'explicit' && lintSourceId(sourceId) === null,
    queryKey: ['subbuilder-explicit', sourceId],
    queryFn: () => apiGet<SourceDescriptorOut>(`/registry/sources/${encodeURIComponent(sourceId)}`),
    retry: false,
  })

  const matchingSources: SourceDescriptorOut[] = useMemo(() => {
    if (refMode === 'explicit') {
      return explicitSource.data ? [explicitSource.data] : []
    }
    return (allSources.data ?? []).filter((s) => sourceMatchesSelector(s, selector))
  }, [refMode, explicitSource.data, allSources.data, selector])

  // ---- preview: matching signals ----
  const signals = useQuery<SignalsPage>({
    queryKey: ['subbuilder-signals'],
    queryFn: () => apiGet<SignalsPage>('/signals?limit=100'),
  })

  const signalPreview = useMemo(() => {
    const all = signals.data?.data ?? []
    const matchSourceIds = new Set(matchingSources.map((s) => s.descriptor_id))
    // restrict to the matched sources when we have any, else all loaded signals
    const scoped =
      matchSourceIds.size > 0
        ? all.filter(
            (s) => matchSourceIds.has(s.descriptor_source_id) || matchSourceIds.has(s.source_id ?? ''),
          )
        : all
    const matched = scoped.filter((s) => signalMatchesSubscription(s, subscription))
    return { scoped, matched }
  }, [signals.data, matchingSources, subscription])

  return (
    <PanelChrome
      registration={registration}
      subtitle={valid ? 'valid SourceRef' : `${issues.length} issue${issues.length === 1 ? '' : 's'}`}
      onRefresh={() => {
        allSources.refetch()
        signals.refetch()
      }}
    >
      <div className="flex-1 overflow-auto text-xs space-y-3">
        {/* ref mode */}
        <section className="bg-surface-100 border border-slate-800 rounded p-2 space-y-2">
          <div className="flex items-center gap-2">
            <span className="text-slate-400 text-[10px] uppercase tracking-wide">source ref</span>
            <div className="flex gap-1 ml-auto">
              {(['explicit', 'selector'] as const).map((m) => (
                <button
                  key={m}
                  onClick={() => setRefMode(m)}
                  className={`rounded px-2 py-0.5 text-[10px] ${
                    refMode === m
                      ? 'bg-sky-900 text-sky-200'
                      : 'bg-surface-200 text-slate-400 hover:text-slate-200'
                  }`}
                  data-testid={`subbuilder-mode-${m}`}
                >
                  {m}
                </button>
              ))}
            </div>
          </div>

          {refMode === 'explicit' ? (
            <label className="block">
              <span className="text-slate-500 text-[10px]">source_id (pick a registered source)</span>
              <ScopePicker
                family="source"
                value={sourceId}
                onChange={setSourceId}
                placeholder="select a source…"
                className="w-full bg-surface-100 border border-slate-800 rounded p-1 px-2 text-[11px] font-mono text-slate-200"
                testId="subbuilder-source-id"
              />
            </label>
          ) : (
            <div className="grid grid-cols-2 gap-2">
              <TokenField label="tags" value={selTags} onChange={setSelTags} placeholder="news osint" testid="subbuilder-sel-tags" />
              <TokenField label="kinds" value={selKinds} onChange={setSelKinds} placeholder="rss gdelt" testid="subbuilder-sel-kinds" />
              <TokenField label="geo" value={selGeo} onChange={setSelGeo} placeholder="BR US" testid="subbuilder-sel-geo" />
              <TokenField label="languages" value={selLangs} onChange={setSelLangs} placeholder="pt en" testid="subbuilder-sel-langs" />
              <TokenField label="owner_tenant" value={selTenant} onChange={setSelTenant} placeholder="default" testid="subbuilder-sel-tenant" />
              <TokenField label="predicate (Starlark)" value={selPredicate} onChange={setSelPredicate} placeholder='"gov" in source.tags' testid="subbuilder-sel-predicate" />
            </div>
          )}
        </section>

        {/* subscription */}
        <section className="bg-surface-100 border border-slate-800 rounded p-2 space-y-2">
          <div className="text-slate-400 text-[10px] uppercase tracking-wide">
            subscription — signal-level slice
          </div>
          <div className="grid grid-cols-2 gap-2">
            <TokenField label="geo" value={subGeo} onChange={setSubGeo} placeholder="BR" testid="subbuilder-sub-geo" />
            <TokenField label="languages" value={subLangs} onChange={setSubLangs} placeholder="pt" testid="subbuilder-sub-langs" />
            <TokenField label="tags" value={subTags} onChange={setSubTags} placeholder="protest" testid="subbuilder-sub-tags" />
            <TokenField label="entity_classes" value={subEntity} onChange={setSubEntity} placeholder="person org" testid="subbuilder-sub-entity" />
            <TokenField label="modalities" value={subModalities} onChange={setSubModalities} placeholder="text image" testid="subbuilder-sub-modalities" />
            <TokenField label="predicate (Starlark residual)" value={subPredicate} onChange={setSubPredicate} placeholder='severity_at_least("high")' testid="subbuilder-sub-predicate" />
          </div>
          <label className="flex items-center gap-2 text-[11px] text-slate-400">
            <input
              type="checkbox"
              checked={canonicalOnly}
              onChange={(e) => setCanonicalOnly(e.target.checked)}
              data-testid="subbuilder-canonical"
            />
            canonical_only (dedup-aware delivery — canonical signals, not aliases)
          </label>
        </section>

        {/* validation */}
        <section data-testid="subbuilder-validation">
          {issues.length === 0 ? (
            <div className="text-emerald-400 text-[11px]" data-testid="subbuilder-valid">
              ✓ valid SourceRef — exactly one of source_id / source_selector set
            </div>
          ) : (
            <ul className="space-y-1">
              {issues.map((i, n) => (
                <li
                  key={`${i.field}-${n}`}
                  className="text-rose-300 text-[11px] bg-rose-900/20 border border-rose-800 rounded px-2 py-1"
                  data-testid="subbuilder-issue"
                >
                  <span className="font-mono text-rose-400">{i.field}</span>
                  {i.value && <span className="text-rose-500"> “{i.value}”</span>}: {i.message}
                </li>
              ))}
            </ul>
          )}
        </section>

        {/* preview: matching sources */}
        <section className="bg-surface-100 border border-slate-800 rounded p-2" data-testid="subbuilder-source-preview">
          <div className="text-slate-400 text-[10px] uppercase tracking-wide mb-1">
            preview — matching sources ({matchingSources.length})
          </div>
          {refMode === 'explicit' && explicitSource.isError && (
            <div className="text-amber-400 text-[11px]">source “{sourceId}” not found in registry</div>
          )}
          <div className="space-y-0.5">
            {matchingSources.slice(0, 12).map((s) => (
              <div key={s.descriptor_id} className="flex items-baseline gap-2 text-[11px] bg-surface-200 rounded px-2 py-0.5">
                <span className="text-slate-300 truncate flex-1">{s.descriptor_id}</span>
                {s.kind && <span className="text-slate-500">{s.kind}</span>}
                <span className="text-slate-600">{s.state}</span>
              </div>
            ))}
            {matchingSources.length === 0 && (
              <div className="text-slate-500 text-[11px]">no sources match this ref yet</div>
            )}
          </div>
        </section>

        {/* preview: matching signals */}
        <section className="bg-surface-100 border border-slate-800 rounded p-2" data-testid="subbuilder-signal-preview">
          <div className="text-slate-400 text-[10px] uppercase tracking-wide mb-1">
            preview — matching signals (structured filter)
          </div>
          <div className="text-[11px] text-slate-400 mb-1" data-testid="subbuilder-match-rate">
            {signalPreview.matched.length} / {signalPreview.scoped.length} loaded signals match
            {subscription.predicate && (
              <span className="text-slate-600"> (+ Starlark residual evaluated server-side)</span>
            )}
          </div>
          <div className="space-y-0.5 max-h-40 overflow-auto">
            {signalPreview.matched.slice(0, 15).map((s) => (
              <div key={s.id} className="flex items-baseline gap-2 text-[11px] bg-surface-200 rounded px-2 py-0.5">
                <span className="text-slate-500 shrink-0 w-14 truncate">{s.language}</span>
                <span className="text-slate-300 truncate flex-1">{s.title}</span>
              </div>
            ))}
            {signalPreview.matched.length === 0 && (
              <div className="text-slate-500 text-[11px]">
                {signals.isLoading ? 'loading signals…' : 'no loaded signals match the structured filter'}
              </div>
            )}
          </div>
        </section>

        {/* output: composed SourceRef */}
        <section data-testid="subbuilder-output">
          <div className="text-slate-400 text-[10px] uppercase tracking-wide mb-1">
            composed SourceRef — paste into a target's <code>sources: [...]</code>
          </div>
          <pre className="bg-surface-200 p-2 rounded overflow-x-auto text-[10px] text-emerald-200 max-h-60" data-testid="subbuilder-json">
            {JSON.stringify(sourceRef, null, 2)}
          </pre>
          <button
            onClick={() => {
              void navigator.clipboard?.writeText(JSON.stringify(sourceRef, null, 2))
            }}
            disabled={!valid}
            className="mt-1 bg-emerald-900 hover:bg-emerald-800 disabled:opacity-50 text-emerald-200 rounded px-2 py-1 text-[11px]"
            data-testid="subbuilder-copy"
          >
            copy SourceRef JSON
          </button>
        </section>
      </div>
    </PanelChrome>
  )
}
