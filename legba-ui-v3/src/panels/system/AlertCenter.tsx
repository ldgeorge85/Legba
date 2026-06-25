/**
 * P-UI6-B. Alert / Notification Center (`system.alert_center`) — UI-6 (Tier G).
 *
 * v2's Watchlist reframed for the source-first model. The operator manages
 * **alert subscriptions** — rules of (scope × min-severity) where scope is a
 * target_id or an analyst_id — and the center fires an alert whenever a NEW
 * finding satisfies a rule.
 *
 * Mechanics:
 *  - The findings feed is **polled** (GET /findings) on a fixed interval; there
 *    is no per-finding push surface, so the center diffs successive polls.
 *  - The **first poll seeds** the seen-id set WITHOUT firing — otherwise every
 *    pre-existing finding would alert on panel open. Only findings that appear
 *    after the seed fire.
 *  - Fired alerts **de-dup on finding id** (a finding never alerts twice).
 *  - Each fired alert is annotated with the subscription it matched (scope +
 *    severity-floor) and supports a **lineage walk** (dispatches
 *    `legba:open-lineage` with the finding id, same cross-panel pattern as the
 *    Findings feed) so the operator can trace its provenance.
 *
 * Subscriptions persist to localStorage (operator-local watchlist — no backend
 * write surface). Subscription targets/analysts are seeded from the registry
 * list endpoints so the operator picks from real ids. Persistence + matching
 * logic is reused from `@/lib/alertModel` (DOM-free, unit-tested).
 */

import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { ScopePicker } from '@/components/ScopePicker'
import { apiGet } from '@/lib/api'
import type { PanelProps } from '@/types'
import { selectRow } from '@/state/selection'
import {
  SEVERITY_ORDER,
  loadSubscriptions,
  persistSubscriptions,
  removeSubscription,
  severityAtLeast,
  subId,
  toggleMute,
  upsertSubscription,
  type AlertScopeKind,
  type AlertSubscription,
  type FiredAlert,
  type Severity,
} from '@/lib/alertModel'

/** Local floor option superset: 'any' bypasses the severity gate (live
 * findings are frequently unscored, so scope-only rules must be expressible). */
type Floor = Severity | 'any'
const FLOOR_OPTIONS: readonly Floor[] = ['any', ...SEVERITY_ORDER] as const

const POLL_MS = 20_000

interface FindingRow {
  id: string
  title: string | null
  severity: string | null
  target_id: string | null
  analyst_id: string | null
  produced_at: string | null
}

async function pollFindings(): Promise<FindingRow[]> {
  try {
    const r = await apiGet<{ data?: FindingRow[] } | FindingRow[]>('/findings?limit=100')
    if (Array.isArray(r)) return r
    return Array.isArray(r?.data) ? r.data : []
  } catch {
    return []
  }
}

/** Map a polled finding into the FiredAlert shape (reuses the model type). */
function findingToAlert(f: FindingRow): FiredAlert {
  const sevRaw = f.severity
  const severity = sevRaw && SEVERITY_ORDER.includes(sevRaw as Severity) ? (sevRaw as Severity) : null
  return {
    id: f.id,
    title: f.title || '(finding)',
    severity,
    target_id: f.target_id ?? null,
    analyst_id: f.analyst_id ?? null,
    fired_at: f.produced_at ?? new Date().toISOString(),
    matched_sub_id: null,
  }
}

/**
 * Match a finding-derived alert against a subscription. Mirrors the model's
 * matchSubscription but understands the local 'any' floor (scope-only rule).
 */
function matchAlert(alert: FiredAlert, subs: AlertSubscription[]): string | null {
  for (const s of subs) {
    if (s.muted) continue
    const scopeVal = s.scope_kind === 'target' ? alert.target_id : alert.analyst_id
    if (scopeVal !== s.scope_id) continue
    if ((s.severity_floor as Floor) !== 'any' && !severityAtLeast(alert.severity, s.severity_floor))
      continue
    return s.id
  }
  return null
}

export default function AlertCenterPanel({ registration }: PanelProps) {
  const [subs, setSubs] = useState<AlertSubscription[]>(() => loadSubscriptions())
  const [fired, setFired] = useState<FiredAlert[]>([])
  const [pollOn, setPollOn] = useState(true)

  // New-subscription form state.
  const [scopeKind, setScopeKind] = useState<AlertScopeKind>('target')
  const [scopeId, setScopeId] = useState('')
  const [floor, setFloor] = useState<Floor>('any')

  // ---- findings poll ----
  const { data: findings, refetch } = useQuery<FindingRow[]>({
    queryKey: ['alert-findings-poll'],
    queryFn: pollFindings,
    refetchInterval: pollOn ? POLL_MS : false,
  })

  // Seen-id set; the FIRST non-empty poll seeds it without firing.
  const seenRef = useRef<Set<string>>(new Set())
  const seededRef = useRef(false)
  const subsRef = useRef(subs)
  subsRef.current = subs

  useEffect(() => {
    if (!findings) return
    if (!seededRef.current) {
      // Seed pass: record every existing id, fire nothing.
      seenRef.current = new Set(findings.map((f) => f.id))
      seededRef.current = true
      return
    }
    const fresh: FiredAlert[] = []
    for (const f of findings) {
      if (seenRef.current.has(f.id)) continue
      seenRef.current.add(f.id)
      const alert = findingToAlert(f)
      alert.matched_sub_id = matchAlert(alert, subsRef.current)
      fresh.push(alert)
    }
    if (fresh.length === 0) return
    setFired((prev) => {
      const have = new Set(prev.map((a) => a.id))
      const add = fresh.filter((a) => !have.has(a.id)) // de-dup on finding id
      if (add.length === 0) return prev
      return [...add, ...prev].slice(0, 200)
    })
  }, [findings])

  function addSub() {
    const id = scopeId.trim()
    if (!id) return
    const sub: AlertSubscription = {
      id: subId(scopeKind, id),
      scope_kind: scopeKind,
      scope_id: id,
      // The literal floor (including the local 'any') is stored on this field
      // and read back as a Floor at match time; it round-trips through
      // localStorage so scope-only rules survive a reload.
      severity_floor: floor as Severity,
      muted: false,
      created_at: new Date().toISOString(),
    }
    const next = upsertSubscription(subs, sub)
    setSubs(next)
    persistSubscriptions(next)
    setScopeId('')
  }
  function mute(id: string) {
    const next = toggleMute(subs, id)
    setSubs(next)
    persistSubscriptions(next)
  }
  function del(id: string) {
    const next = removeSubscription(subs, id)
    setSubs(next)
    persistSubscriptions(next)
  }

  const matchedCount = useMemo(() => fired.filter((a) => a.matched_sub_id).length, [fired])

  // Only the matched alerts surface as actionable; unmatched stay dimmed so the
  // operator sees what they're NOT subscribed to.
  return (
    <PanelChrome
      registration={registration}
      subtitle={`${subs.length} subscription${subs.length === 1 ? '' : 's'} · ${fired.length} fired (${matchedCount} matched)`}
      onRefresh={() => refetch()}
      actions={
        <button
          onClick={() => setPollOn((v) => !v)}
          className={`text-[10px] px-2 py-0.5 rounded border ${
            pollOn ? 'border-accent-ok text-accent-ok' : 'border-slate-700 text-slate-500'
          }`}
          title="Toggle findings poll"
          data-testid="alert-tail-toggle"
        >
          {pollOn ? '● polling' : '○ paused'}
        </button>
      }
    >
      {/* new subscription */}
      <div className="flex items-center gap-2 mb-2 text-xs flex-wrap">
        <select
          className="bg-surface-200 border border-slate-700 rounded p-1 px-2"
          value={scopeKind}
          onChange={(e) => {
            setScopeKind(e.target.value as AlertScopeKind)
            setScopeId('')
          }}
          data-testid="alert-scope-kind"
        >
          <option value="target">target</option>
          <option value="analyst">analyst</option>
        </select>
        <ScopePicker
          family={scopeKind}
          value={scopeId}
          onChange={setScopeId}
          placeholder={`select ${scopeKind}…`}
          className="flex-1 min-w-[140px] bg-surface-200 border border-slate-700 rounded p-1 px-2 text-slate-200"
          testId="alert-scope-id"
        />
        <select
          className="bg-surface-200 border border-slate-700 rounded p-1 px-2"
          value={floor}
          onChange={(e) => setFloor(e.target.value as Floor)}
          data-testid="alert-severity-floor"
        >
          {FLOOR_OPTIONS.map((s) => (
            <option key={s} value={s}>
              floor: {s}
            </option>
          ))}
        </select>
        <button
          onClick={addSub}
          className="bg-surface-200 hover:bg-surface-300 border border-slate-700 rounded px-2 py-1"
          data-testid="alert-add-sub"
        >
          + subscribe
        </button>
      </div>

      {/* subscriptions list */}
      <div className="mb-3" data-testid="alert-subs">
        <div className="text-[11px] text-slate-500 mb-1">Subscriptions</div>
        {subs.length === 0 && (
          <div className="text-slate-600 text-xs">no subscriptions — add one above</div>
        )}
        <div className="space-y-1">
          {subs.map((s) => (
            <div
              key={s.id}
              className={`flex items-center gap-2 text-xs bg-surface-100 border border-slate-800 rounded p-1.5 ${
                s.muted ? 'opacity-50' : ''
              }`}
              data-testid={`alert-sub-${s.id}`}
            >
              <span className="rounded px-1 bg-slate-700 text-slate-200 shrink-0">
                {s.scope_kind}
              </span>
              <span className="font-mono text-slate-200 truncate">{s.scope_id}</span>
              <span className="text-slate-500 shrink-0">
                {(s.severity_floor as Floor) === 'any' ? 'any sev' : `≥ ${s.severity_floor}`}
              </span>
              <button
                onClick={() => mute(s.id)}
                className="ml-auto text-slate-400 hover:text-slate-200 shrink-0"
                data-testid={`alert-sub-mute-${s.id}`}
              >
                {s.muted ? 'unmute' : 'mute'}
              </button>
              <button
                onClick={() => del(s.id)}
                className="text-slate-600 hover:text-rose-400 shrink-0"
                title="delete subscription"
                data-testid={`alert-sub-del-${s.id}`}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      </div>

      {/* recently fired */}
      <div className="flex-1 overflow-auto" data-testid="alert-fired">
        <div className="text-[11px] text-slate-500 mb-1">Recently fired</div>
        {fired.length === 0 && (
          <div className="text-slate-600 text-xs">
            {pollOn ? 'watching the findings feed for new findings…' : '(poll paused)'}
          </div>
        )}
        <div className="space-y-1">
          {fired.map((a) => (
            <button
              key={a.id}
              onClick={() => selectRow('finding', a.id, a.title, { origin: 'alert-center' })}
              className={`w-full text-left text-xs border rounded p-1.5 hover:bg-surface-200 ${
                a.matched_sub_id
                  ? 'bg-surface-100 border-accent-info/40'
                  : 'bg-surface-100/40 border-slate-800 opacity-70'
              }`}
              title="open lineage walk"
              data-testid={`alert-fired-${a.id}`}
            >
              <div className="flex items-center gap-2">
                {a.severity ? (
                  <span
                    className={`shrink-0 rounded px-1 ${
                      a.severity === 'critical'
                        ? 'bg-rose-900 text-rose-200'
                        : a.severity === 'high'
                          ? 'bg-amber-900 text-amber-200'
                          : 'bg-slate-700 text-slate-200'
                    }`}
                  >
                    {a.severity}
                  </span>
                ) : (
                  <span className="shrink-0 rounded px-1 bg-slate-800 text-slate-500">unscored</span>
                )}
                <span className="text-slate-200 font-medium truncate">{a.title}</span>
                {a.matched_sub_id && (
                  <span
                    className="ml-auto shrink-0 text-accent-info"
                    data-testid={`alert-fired-matched-${a.id}`}
                  >
                    ✓ subscribed
                  </span>
                )}
              </div>
              <div className="mt-0.5 text-slate-600 flex items-center gap-2">
                {a.target_id && <span className="font-mono">{a.target_id}</span>}
                {a.analyst_id && <span className="font-mono">{a.analyst_id}</span>}
                <span>{new Date(a.fired_at).toLocaleString()}</span>
                <span className="ml-auto text-accent-info/70">lineage →</span>
              </div>
            </button>
          ))}
        </div>
      </div>
    </PanelChrome>
  )
}
