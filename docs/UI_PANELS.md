# V2 UI Panel Reference

## Overview

The Legba V2 UI is a React single-page application served on port **8503**. The layout engine is **Dockview**, which provides VS Code-style draggable, resizable, tabbed panels. Users can arrange any combination of the 24 available panels into split panes and tab groups. Layout state (panel positions, sizes, active tabs) is persisted to `localStorage` under the key `legba-workspace-layout` and restored on reload. The default theme is dark.

The sidebar lists 22 directly-openable panels (the two detail panels are opened indirectly via click-through). Panels are organized into 7 navigation groups. A global search box in the sidebar searches across events, entities, and facts simultaneously.

## Panel Groups

| Group | Panels |
|---|---|
| **Overview** | Dashboard |
| **Intelligence** | Signals, Events, Entities, Sources, Goals, Facts |
| **Visualization** | Graph, Map, Timeline |
| **Real-Time** | Live Feed, Consult |
| **Tracking** | Situations, Watchlist, Edge Queue |
| **Analysis** | Hypotheses, Briefs |
| **System** | Analytics, Cycles, Journal, Reports, Scorecard |

## Panel Descriptions

| Panel | Description | API Endpoint | Key Interactions |
|---|---|---|---|
| **Dashboard** | At-a-glance stat cards (events, entities, sources, goals, situations, watchlist, graph nodes, facts, hypotheses, journal entries) plus a recent-events list with severity badges and a mini event-volume sparkline. | `GET /api/v2/dashboard`, `GET /api/v2/events`, `GET /api/stats/events-timeseries` | Clicking a stat card opens the corresponding panel. Clicking an event opens Event Detail. |
| **Signals** | Paginated list of raw ingested items (RSS, scraped articles) before LLM processing. Supports full-text search, category filter, source filter, date range, and minimum confidence slider. 50 per page. | `GET /api/v2/signals`, `GET /api/v2/signals/facets` | Click a signal row to view detail. Category and source facet counts shown in filter sidebar. |
| **Events** | Paginated list of derived real-world events (clustered from signals by the agent). Filterable by category, severity, and event type. Full-text search with debounced input. 50 per page. | `GET /api/v2/events`, `GET /api/v2/events/facets` | Clicking a row opens Event Detail. Entity badges in each row open Entity Detail. Selection propagates to Graph and Map. |
| **Event Detail** | Full detail for a single event: title, description, category badge, severity, confidence score, timestamp, source attribution, and a list of linked entities with type badges. | `GET /api/v2/events/:id` | Clicking an entity badge opens Entity Detail and sets the global selection. Reacts to selection store changes. |
| **Entities** | Paginated, searchable list of all known entities (persons, organizations, locations, countries, etc.). Filters for entity type, creation date, and minimum completeness score. | `GET /api/v2/entities`, `GET /api/v2/entities/types` | Clicking a row opens Entity Detail. Selection propagates to Graph (ego subgraph) and Map (focus marker). |
| **Entity Detail** | Full profile for a single entity: type badge, aliases, first/last seen dates, event count, completeness score, linked facts, and related events. Includes an entity merge modal. | `GET /api/v2/entities/:id` | "Merge Into" button opens a search-and-merge flow. Clicking linked events opens Event Detail. |
| **Sources** | Paginated source management table with health indicators (green/amber/red based on fail rate). Supports search, status filter, inline editing of name/URL/status, and CRUD operations (add, edit, delete). | `GET /api/v2/sources`, `POST /api/v2/sources`, `PUT /api/v2/sources/:id`, `DELETE /api/v2/sources/:id` | Inline edit and delete with confirmation. Add-source form with name, URL, and type fields. |
| **Goals** | Hierarchical tree view of the agent's goal structure. Displays status (active/completed/paused/abandoned), priority, and supports inline editing. Status filter collapses non-matching branches. | `GET /api/v2/goals`, `PUT /api/v2/goals/:id`, `DELETE /api/v2/goals/:id` | Expand/collapse tree nodes. Inline status and priority editing. |
| **Facts** | Paginated list of structured facts (subject-predicate-object triples) extracted by the agent. Filterable by predicate type, subject, and minimum confidence. Full-text search. | `GET /api/v2/facts`, `GET /api/v2/facts/predicates` | Predicate facet dropdown for filtering. Delete individual facts. |
| **Graph** | Interactive force-directed knowledge graph rendered with Sigma.js on a graphology data structure. Nodes colored by entity type, edges colored by relationship type. Louvain community detection for clustering. Supports ego-graph mode (subgraph around a selected entity), edge-type filtering, and ForceAtlas2 layout. | `GET /api/graph`, `GET /api/graph/ego` | Clicking a node selects the entity globally. Hover shows label. Edge-type checkboxes filter visible relationships. Ego-graph mode activates when an entity is selected in another panel. |
| **Map** | Geospatial view using MapLibre GL JS with CartoDB Dark Matter tiles. Three layers: entities (from graph geo-coordinates), events (from event locations), and signals (from signal locations), toggled independently. Markers colored by entity type or event severity. | `GET /api/graph/geo`, `GET /api/v2/events/geo`, `GET /api/v2/signals/geo` | Click a marker to select the entity/event. Popup shows name and type. Layer toggle buttons switch between entities, events, and signals views. |
| **Timeline** | Horizontal timeline of events rendered with vis-timeline. Color-coded by category. Includes a density bar showing event volume distribution across 24 time buckets. Category legend with show/hide toggles. Severity color overlay. | `GET /api/v2/events` | Clicking an event item opens Event Detail. Category toggles filter visible items. Zoom and pan on the time axis. |
| **Live Feed** | Real-time event stream via Server-Sent Events. Shows the most recent 100 events as they arrive, with connection status indicator (connected/reconnecting/disconnected). Auto-scrolls to newest. | `EventSource /sse/stream` (SSE, `event:new` messages) | New events appear at the top automatically. Connection status badge. |
| **Consult** | Chat interface for querying the agent in natural language. Sends user messages to the backend, which routes them through the LLM with full context. Markdown rendering for responses. Session clear button. | `POST /consult/send`, `DELETE /consult/session` | Type a question, press Enter or click Send. Clear button resets conversation history. |
| **Situations** | List of tracked geopolitical situations with severity badges (critical/high/medium/low) and status filter (active/resolved/escalating). Each situation links to involved entities and events. | `GET /api/v2/situations` | Status dropdown filters the list. Clicking a situation selects it and opens related entities/events in other panels. |
| **Watchlist** | User-defined watch items that trigger alerts when matching events arrive. Each watch specifies entities, keywords, categories, and priority. Shows recent trigger history. Full CRUD support. | `GET /api/v2/watchlist`, `GET /api/v2/watchlist/triggers`, `POST /api/v2/watchlist`, `PUT /api/v2/watchlist/:id`, `DELETE /api/v2/watchlist/:id` | Add/edit/delete watch items. Trigger history shows which events matched. |
| **Edge Queue** | Review queue for agent-proposed knowledge graph edges. Shows pending edges with source entity, target entity, relationship type, confidence, and evidence text. Approve/reject workflow with status tabs (pending/approved/rejected). | `GET /api/v2/proposed-edges`, `POST /api/v2/proposed-edges/:id/review` | Approve or reject buttons on each pending edge. Tab bar switches between pending, approved, and rejected views. |
| **Hypotheses** | List of competing hypothesis pairs generated during SYNTHESIZE cycles (Analysis of Competing Hypotheses). Filterable by status (active/confirmed/refuted/stale). Shows confidence scores and supporting evidence. | `GET /api/v2/hypotheses` | Status filter dropdown. Expand to see evidence and confidence details. |
| **Briefs** | Situation briefs produced by SYNTHESIZE cycles approximately every 2 hours. Each brief has a title, cycle number, timestamp, and a full Markdown body rendered with ReactMarkdown. Sorted newest-first. | `GET /api/v2/briefs` | Click to expand/collapse brief content. Newest briefs appear at the top. |
| **Analytics** | Charts and visualizations of system health: event volume time-series (stacked area by category), entity type distribution (pie chart), and source health status (bar chart). Uses Recharts. | `GET /api/stats/events-timeseries`, `GET /api/stats/entity-distribution`, `GET /api/stats/source-health` | Hover for tooltips on chart data points. |
| **Cycles** | Paginated table of agent cycle history showing cycle number, type (with color-coded badge), duration, tool call count, LLM call count, events produced, and error count. 50 per page. | `GET /api/v2/cycles` | Pagination. Rows color-coded by cycle type (EVOLVE, INTROSPECTION, ANALYSIS, RESEARCH, ACQUIRE, NORMAL). |
| **Journal** | Agent's introspective journal with two views: the consolidated summary (produced periodically) and individual cycle entries sorted newest-first. Full Markdown rendering. Download as `.md` file. | `GET /api/journal` | Click an entry to read it. Download button exports as Markdown. Consolidation entry pinned at top. |
| **Reports** | Master-detail view of agent-generated intelligence reports. Left sidebar lists reports by cycle number; right pane renders the selected report as Markdown. Download button exports individual reports. | `GET /api/reports` | Click a report in the sidebar to view. Download button for each report. |
| **Scorecard** | Intelligence maturity scorecard showing key metrics: event counts, entity counts, fact counts, graph density, source health, and breakdowns by category and type. Metric cards color-coded (green/yellow/red) by health thresholds. | `GET /api/v2/scorecard` | Read-only dashboard. Bar lists show distribution breakdowns ranked by count. |

## Cross-Panel Interactions

**Selection propagation.** A Zustand `selectionStore` holds the currently selected item (entity or event). When a user clicks an entity in the Entities panel, the selection propagates to: Graph (switches to ego-graph mode centered on that entity), Map (pans to the entity's marker), and Entity Detail (shows the full profile). The same pattern applies for event selection.

**Click-to-open-detail.** Clicking an event row in Events, Dashboard, Timeline, or Live Feed opens the Event Detail panel (or focuses it if already open). Clicking an entity in Entities, Graph, or Event Detail opens the Entity Detail panel. Dockview deduplicates panels by ID -- if a detail panel for the same item already exists, it activates the existing tab rather than creating a duplicate.

**Panel open mechanics.** The sidebar and cross-panel navigation both call `openPanel(type, params?)` on the workspace store, which sets a `pendingPanel` request. The Workspace component consumes this request, checks for an existing panel with the same ID, and either activates it or creates a new Dockview tab.

## Key Technologies

| Technology | Purpose |
|---|---|
| **Dockview** | Tabbed, draggable panel layout with serializable state |
| **Sigma.js + graphology** | Force-directed graph rendering with ForceAtlas2 layout and Louvain community detection |
| **MapLibre GL JS** | Geospatial map with CartoDB Dark Matter raster tiles |
| **vis-timeline** | Horizontal event timeline with zoom/pan |
| **Recharts** | Charts (area, pie, bar) in Analytics and Dashboard panels |
| **TanStack Query** | Server state management with auto-refetch, caching, and query invalidation |
| **Zustand** | Client state (workspace layout requests, selection propagation, sidebar collapse) |
| **ReactMarkdown + remark-gfm** | Markdown rendering in Journal, Reports, Briefs, and Consult panels |
| **Lucide React** | Icon set used throughout the sidebar and panel toolbars |

## Access

The V2 UI is served by the FastAPI backend on port **8503**. For remote access, set up an SSH tunnel:

```
ssh -L 8503:localhost:8503 <user>@<host>
```

Then open `http://localhost:8503` in a browser. The UI has no authentication layer -- access control is handled at the network/SSH level.
