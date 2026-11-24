/**
 * The Watch surface's wire shapes (U-9) — what the four routes answer, kept
 * to the fields the surface reads. Every field the server may leave out is
 * optional here; the model (`model.ts`) never prints a figure it was not sent.
 *
 *   GET /v3/system/alerts/log?hours=        the one alert log (docs/ALERTING.md)
 *   GET /v3/system/source-firing            per-source firing, with `quiet_class` (U-9)
 *   GET /v3/evidence/drift?window_hours=    the edit counts (summary)
 *   GET /v3/evidence/drift/flags?state=open the open flags, review order
 *   GET /v3/evidence/drift/editors          the per-source edit record
 *   GET /v3/evidence/drift/history?address= every version of one page
 */

export interface AlertEpisode {
  id: string
  kind: string
  group?: string | null
  topic?: string | null
  subject_kind: string
  subject_id: string
  severity: string
  max_severity?: string
  summary: string
  opened_at: string
  last_raised_at: string
  occurrences?: number
  paged_count?: number
  paged_severity?: string | null
  last_paged_at?: string | null
  raised_by?: string
  closed_at?: string | null
  close_reason?: string | null
}

export interface PagedAlert {
  event_id: string
  alert_id?: string | null
  raised_at: string
  kind: string
  severity: string
  why?: string
  rule_entry?: string | null
  rule_version?: string | null
  outcomes?: string[]
  delivered?: boolean
}

export interface AlertKindInfo {
  kind: string
  group: string
  subject_kind?: string
  summary?: string
  pages_when?: string[]
}

export interface AlertLogResponse {
  measured: boolean
  not_measured_reason?: string | null
  window_hours: number
  read_at?: string
  as_of?: string | null
  rule_version?: string | null
  rule_error?: string | null
  kinds?: AlertKindInfo[]
  open?: AlertEpisode[]
  closed?: AlertEpisode[]
  paged?: PagedAlert[]
}

/** `GET /v3/system/source-firing` — the fields the silences read. */
export interface FiringRow {
  source_id: string
  state?: string | null
  status: string
  last_seen_at?: string | null
  last_poll_outcome?: string | null
  recent_error_count?: number
  /** U-9: absent from a registry not yet rebuilt — the model then derives a coarser class. */
  quiet_class?: string | null
  last_poll_newest_entry_at?: string | null
}

export interface DriftSummary {
  available: boolean
  window_hours?: number
  classified?: number
  open_flags_total?: number
  flags?: Record<string, number>
  history_rewrites?: number
  would_auto_close?: { window?: number; total?: number; rule_version?: string | null; switch_on?: boolean }
  auto_closed?: { window?: number; total?: number; reopened?: number }
}

export interface DriftPassage {
  earlier?: string
  later?: string
  changed_words?: number
}

export interface DriftFlag {
  id: number | string
  address: string
  source_id?: string | null
  earlier_version_id?: number | string | null
  later_version_id?: number | string | null
  drift_class?: string | null
  dependant_kind?: string | null
  dependant_id?: string | null
  cited_span?: string | null
  state?: string
  created_at?: string
  earlier_fetched_at?: string | null
  later_fetched_at?: string | null
  changed?: DriftPassage[] | null
  removed?: string[] | null
  change_type?: string | null
  component_types?: string[] | null
  acknowledged?: string | null
  summary?: string | null
  history_rewrite?: boolean | null
  late_unexplained_change?: boolean | null
  claim_effect?: string | null
  auto_closed?: boolean
  would_auto_close?: boolean
}

export interface DriftFlagsResponse {
  available: boolean
  flags?: DriftFlag[]
}

export interface EditWindow {
  assessed?: number
  by_type?: Record<string, number>
  acknowledged?: Record<string, number>
  history_rewrite?: number
  late_unexplained_change?: number
  pages_rechecked?: number
}

export interface DriftEditorRow {
  subject: string
  classified?: number
  pages?: number
  substantive_or_worse?: number
  edit_record?: { '7d'?: EditWindow; '30d'?: EditWindow }
}

export interface DriftEditorsResponse {
  available: boolean
  window_hours?: number
  sources?: DriftEditorRow[]
}

export interface DriftVersion {
  id: number | string
  url?: string
  sha256?: string | null
  text_chars?: number | null
  status?: number | string | null
  first_seen_at?: string | null
  last_seen_at?: string | null
  fetch_count?: number | null
}

export interface DriftHistoryResponse {
  available: boolean
  address?: string
  versions?: DriftVersion[]
}
