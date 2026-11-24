/**
 * Registry (`system.registry`) — UI-B1/merges, UI-D1 Table A "family switch:
 * Subjects · Units · Sources · Stack · Packs; Canvas".
 *
 * The descriptor families are read ONE INSTEAD OF THE OTHER, so they are tabs
 * of one Engine kind rather than eight catalog rows:
 *   - Subjects → subject descriptor heads (`registry.targets`)
 *   - Units    → unit descriptor heads (`registry.analysts`)
 *   - Sources  → the source registry with the per-source detail beneath it
 *                (`registry.sources` + `source.detail` — Table B: "the Sources
 *                family + the Inspector drill"; the detail follows the shared
 *                selection, so a row click there is the drill)
 *   - Stack    → substrate stack components (`registry.stack`)
 *   - Packs    → action-pack grants (`registry.action_packs`)
 *   - Builder  → the subscription builder (`source.subscription_builder`)
 *   - Policy   → a source's subscription gate (`source.subscription_policy`)
 *   - Canvas   → the live registry canvas (`v4.flow`, re-pointed off
 *                Provenance's old Flow tab)
 *
 * The family labels for subjects and units come from the profile
 * (`PROFILE_UI`), the platform noun when the profile declares none. Every
 * retired id resolves here through `panel-registry/aliases.ts` onto the tab
 * that IS its surface; every child renders UNMODIFIED inside
 * `PanelEmbedProvider`.
 */
import { useEffect } from 'react'
import type { PanelProps } from '@/types'
import { PanelTabStrip, type PanelTabDef } from '@/components/PanelTabs'
import { PanelEmbedProvider } from '@/components/PanelEmbedContext'
import RegistryTargetsPanel from '@/panels/registry/Targets'
import RegistryAnalystsPanel from '@/panels/registry/Analysts'
import RegistryStackPanel from '@/panels/registry/Stack'
import RegistryActionPacksPanel from '@/panels/registry/ActionPacks'
import SourceRegistryPanel from '@/panels/source/SourceRegistry'
import SourceDetailPanel from '@/panels/source/SourceDetail'
import SubscriptionBuilderPanel from '@/panels/source/SubscriptionBuilder'
import SubscriptionPolicyPanel from '@/panels/source/SubscriptionPolicy'
import FlowPanel from '@/panels/v4/FlowPanel'
import { PROFILE_UI } from '@/lib/profileUi'
import { emitRead } from '@/lib/readTelemetry'
import { useMergedTab } from './useMergedTab'

type Tab = 'subjects' | 'units' | 'sources' | 'stack' | 'packs' | 'builder' | 'policy' | 'canvas'

function capitalized(word: string): string {
  return word ? word.charAt(0).toUpperCase() + word.slice(1) : word
}

export const REGISTRY_TABS: readonly PanelTabDef[] = [
  { id: 'subjects', label: capitalized(PROFILE_UI.subject.plural) },
  { id: 'units', label: capitalized(PROFILE_UI.unit_noun.plural) },
  { id: 'sources', label: 'Sources' },
  { id: 'stack', label: 'Stack' },
  { id: 'packs', label: 'Packs' },
  { id: 'builder', label: 'Builder' },
  { id: 'policy', label: 'Policy' },
  { id: 'canvas', label: 'Canvas' },
]

export default function RegistryMerged(props: PanelProps) {
  const [tab, setTab] = useMergedTab<Tab>('system.registry', props.initialTab, REGISTRY_TABS, 'subjects')

  // READ TELEMETRY (D2e) — the canvas is the production-path walk that
  // Provenance's Flow tab used to count; it keeps counting from its new home.
  useEffect(() => {
    if (tab === 'canvas') emitRead('lineage_walk', { subjectKind: 'registry_tab', subjectId: 'canvas' })
  }, [tab])

  return (
    <div className="flex h-full w-full flex-col bg-surf-2">
      <div className="flex items-center gap-2 border-b border-line bg-surf-3 px-density py-1.5">
        <span className="text-label uppercase tracking-wider text-ink-3">Registry</span>
        <PanelTabStrip
          tabs={REGISTRY_TABS}
          active={tab}
          onChange={(id) => setTab(id as Tab)}
          ariaLabel="Registry family"
          testIdPrefix="registry-tab"
        />
      </div>
      <div className="min-h-0 flex-1">
        <PanelEmbedProvider>
          {tab === 'subjects' && <RegistryTargetsPanel {...props} />}
          {tab === 'units' && <RegistryAnalystsPanel {...props} />}
          {tab === 'sources' && (
            <div className="flex h-full w-full flex-col">
              <div className="min-h-0 flex-1" data-testid="registry-sources-list">
                <SourceRegistryPanel {...props} />
              </div>
              <div className="min-h-0 flex-1 border-t border-line" data-testid="registry-sources-detail">
                <SourceDetailPanel {...props} />
              </div>
            </div>
          )}
          {tab === 'stack' && <RegistryStackPanel {...props} />}
          {tab === 'packs' && <RegistryActionPacksPanel {...props} />}
          {tab === 'builder' && <SubscriptionBuilderPanel {...props} />}
          {tab === 'policy' && <SubscriptionPolicyPanel {...props} />}
          {tab === 'canvas' && <FlowPanel />}
        </PanelEmbedProvider>
      </div>
    </div>
  )
}
