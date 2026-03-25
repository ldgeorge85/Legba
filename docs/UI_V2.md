# Legba UI v2 — The Crossroads

The v2 operator console is a multi-panel intelligence workstation built with React, designed for real-time situational awareness analysis. It runs as a separate container on port **8503**, proxying API requests to the FastAPI backend on port 8501.

## Architecture

```
Browser (8503) --> nginx --> static SPA files (React build)
                         --> /api/*      proxy to FastAPI (8501)
                         --> /sse/*      proxy to FastAPI (8501, buffering off)
                         --> /ws/*       proxy to FastAPI (8501, WebSocket upgrade)
                         --> /consult/*  proxy to FastAPI (8501)
```

- **Frontend**: React 18 + TypeScript + Vite, served by nginx
- **State**: Zustand (workspace layout, entity selection, global filters)
- **Server state**: TanStack Query (caching, background refresh, mutations)
- **Layout**: Dockview (draggable, resizable, tabbed multi-panel workspace)
- **Styling**: Tailwind CSS + shadcn/ui color system, dark theme

### Container

- Image: `legba-ui-v2:dev` (multi-stage: `node:20-alpine` build, `nginx:alpine` serve)
- Dockerfile: `docker/ui-v2.Dockerfile`
- Nginx config: `docker/nginx-ui-v2.conf`
- Docker Compose: `ui-v2` service (profile: `v2`) in `docker-compose.yml`

### Source tree

```
legba-ui/
  src/
    api/          # API client, TanStack Query hooks, types, SSE/WS clients
    components/   # Layout (Sidebar, Workspace, StatusBar), common (Badge, TimeAgo, ErrorBoundary)
    panels/       # 19 panel components (one per view)
    stores/       # Zustand stores (workspace, selection, filters)
    lib/          # Utilities (cn, color helpers)
    globals.css   # Tailwind directives, CSS variables, Dockview overrides
    main.tsx      # Entry point
    App.tsx       # Root layout
```

## Layout

The workspace is a three-part layout:

1. **Sidebar** (left) — collapsible navigation grouped into sections
2. **Workspace** (center) — Dockview multi-panel area where panels can be dragged, tabbed, split, and resized
3. **Status Bar** (bottom) — live system metrics

### Sidebar groups

| Group | Panels |
|-------|--------|
| Overview | Dashboard |
| Intelligence | Events, Entities, Sources, Goals, Facts |
| Visualization | Graph, Map, Timeline |
| Real-Time | Live Feed, Consult |
| Tracking | Situations, Watchlist |
| System | Analytics, Cycles, Journal, Reports |

## Panels

### Dashboard
KPI grid showing counts for entities, events, sources, goals, facts, situations, watchlist items, and relationships. Includes a recent events list, active situations, and a 7-day event volume sparkline chart.

### Events
Paginated data table of all events. Search by title, filter by category (conflict, political, economic, etc.). Click a row to open Event Detail in a new panel.

### Event Detail
Full event view: title, category, confidence score, timestamp, description, tags, source link, and linked entities (clickable to open Entity Detail).

### Entities
Paginated entity table with search and type filter (person, organization, country, location, etc.). Click to open Entity Detail. Delete supported.

### Entity Detail
Entity profile showing type, aliases, assertions (key-value pairs with confidence), and relationships from the knowledge graph. Each relationship links to the related entity.

### Sources
Source table with health indicators (fetch/fail/event counts, last fetched). CRUD operations: add new source (name, URL, type), inline edit (name, status), delete.

### Goals
Hierarchical tree view with progress bars. Filter by status (active, completed, paused, abandoned). Edit status and priority inline. Delete with child reparenting.

### Facts
Paginated fact table: subject, predicate, object, confidence %, source cycle, timestamp. Search across subjects and values. Delete per row.

### Graph (Knowledge Graph)
Interactive WebGL graph visualization using Sigma.js + Graphology.

- **ForceAtlas2** layout with adaptive iterations (250 for small graphs, 80 for 500+ nodes)
- Nodes colored by entity type, sized by degree
- **Search** with ranked matching (exact > prefix > word-boundary > substring), highlights all matches and dims non-matching nodes
- **Ego graph toggle** — when an entity is selected, shows only its neighborhood (depth 2)
- Click node to select entity and open detail panel
- Hover for tooltip, legend overlay

### Map (Geospatial)
MapLibre GL JS with CartoDB Dark Matter raster tiles (no API key required).

- Circle markers for geo-located entities, colored by type
- Text labels at zoom >= 4
- Hover popup with entity name and type
- Click to select entity and open detail
- Fly-to animation when entity selected from other panels
- Legend overlay

### Timeline
vis-timeline with dynamically imported libraries (code-split for bundle size).

- Events plotted with category-specific colors
- Dark theme via CSS injection
- Click event to select and open detail
- Pagination (200 events per page)
- Category legend bar

### Live Feed (Event Stream)
Real-time SSE event stream showing new events as they arrive. Auto-scroll with connection status indicator.

### Consult
Chat interface for querying the AI about intelligence data. Messages sent via REST to `/consult/send`. Assistant responses rendered as markdown with full formatting (headings, lists, tables, code blocks). Session clear button.

### Situations
Situation tracker with status filter (active, resolved, escalating). Shows severity, event count, timestamps. Click to select.

### Watchlist
Watch item cards showing tracked entities (blue badges), keywords (purple badges), categories, priority, and trigger count. Full CRUD: add new watch items with entity/keyword/category lists, edit name/description/priority, two-click delete.

### Analytics
Dashboard placeholder for Recharts-based analytics (event volume trends, entity distribution, source health).

### Cycle Monitor
Audit-sourced cycle table showing cycle number, detected type (SURVEY, CURATE, RESEARCH, ANALYSIS, SYNTHESIZE, INTROSPECTION, EVOLVE), duration, tool/LLM call counts, events produced, and errors.

### Journal
Agent's personal journal. Shows the latest **consolidation** report (rendered as markdown) at the top, with individual cycle entries collapsed behind a toggle. Each cycle entry shows the cycle number, timestamp, and reflective observations.

### Reports
Split-pane view: report list on the left (cycle number + time ago), full markdown-rendered content on the right. **Download as Markdown** button exports the raw report text.

## Cross-Panel Interactions

Panels communicate through two Zustand stores:

### Selection Store
Tracks the currently selected entity or event. When a user clicks an entity in the Graph, Map, or Entities panel, all other panels react:
- Graph highlights the node
- Map flies to the location
- Entity Detail shows the profile
- Selection history with back/forward navigation (50-item cap)

### Workspace Store
Manages panel lifecycle. `openPanel(type, params)` either activates an existing panel or creates a new one. Panels can be dragged into tabs, split horizontally/vertically, or popped out.

## API Layer

### v2 JSON API (`/api/v2/`)

All endpoints return JSON. Mounted on the FastAPI backend.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v2/dashboard` | KPI counts, recent events, active situations |
| GET | `/api/v2/events` | Paginated, search, category filter |
| GET | `/api/v2/events/:id` | Event detail with linked entities |
| DELETE | `/api/v2/events/:id` | Delete event |
| GET | `/api/v2/entities` | Paginated, search, type filter |
| GET | `/api/v2/entities/:id` | Entity profile with assertions + relationships |
| DELETE | `/api/v2/entities/:id` | Delete entity |
| GET | `/api/v2/sources` | Paginated, search, status filter |
| POST | `/api/v2/sources` | Create source (name, url, source_type) |
| PUT | `/api/v2/sources/:id` | Update source fields |
| DELETE | `/api/v2/sources/:id` | Delete source |
| GET | `/api/v2/goals` | Goal tree (hierarchical) |
| PUT | `/api/v2/goals/:id` | Update goal status/priority |
| DELETE | `/api/v2/goals/:id` | Delete goal |
| GET | `/api/v2/situations` | List with status filter |
| GET | `/api/v2/watchlist` | Active watch items |
| POST | `/api/v2/watchlist` | Create watch item |
| PUT | `/api/v2/watchlist/:id` | Update watch item |
| DELETE | `/api/v2/watchlist/:id` | Delete watch item |
| GET | `/api/v2/watchlist/triggers` | Recent watch triggers |
| GET | `/api/v2/facts` | Paginated, search, predicate filter |
| DELETE | `/api/v2/facts/:id` | Delete fact |
| GET | `/api/v2/memory` | Qdrant memory points |
| DELETE | `/api/v2/memory/:col/:id` | Delete memory point |
| GET | `/api/v2/cycles` | Cycle list from audit index |
| GET | `/api/v2/cycles/:num` | Cycle detail with phases |

### Legacy endpoints (proxied through)

| Endpoint | Description |
|----------|-------------|
| `/api/graph` | Full knowledge graph (Cytoscape format, transformed client-side) |
| `/api/graph/ego` | Ego graph for entity |
| `/api/graph/geo` | Geo-located nodes for map |
| `/api/journal` | Journal entries + consolidation |
| `/api/reports` | Report history |
| `/consult/send` | Send consult message |
| `/consult/session` | Manage consult session |
| `/sse/stream` | SSE event stream |

## Tech Stack

| Library | Version | Purpose |
|---------|---------|---------|
| React | 18 | UI framework |
| TypeScript | 5.x | Type safety |
| Vite | 6.x | Build tool + dev server |
| Tailwind CSS | 3.x | Utility-first styling |
| @tailwindcss/typography | 0.5.x | Prose markdown rendering |
| Dockview | 4.x | Multi-panel workspace layout |
| Sigma.js | 3.x | WebGL graph rendering |
| Graphology | 0.25.x | Graph data structure + ForceAtlas2 |
| MapLibre GL JS | 5.x | Vector/raster map rendering |
| vis-timeline | 7.x | Temporal event visualization |
| TanStack Query | 5.x | Server state + caching |
| Zustand | 5.x | Client state management |
| Recharts | 2.x | Charts (analytics) |
| react-markdown | 9.x | Markdown rendering |
| Lucide React | 0.x | Icon library |
| date-fns | 4.x | Date formatting |

## Deployment

### Production (Docker Compose)

```bash
# Build and start with v2 profile
docker compose -p legba --profile v2 up -d --build

# Or build manually
docker build -f docker/ui-v2.Dockerfile -t legba-ui-v2:dev .
docker run -d --name legba-ui-v2 --network legba_default \
  -p 8503:8503 --restart unless-stopped legba-ui-v2:dev
```

### Development (Vite HMR)

```bash
# Start dev server in container with hot reload
docker compose -p legba --profile v2-dev up -d
# Vite dev server at http://localhost:5173, proxies API to FastAPI
```

### Rebuild cycle

```bash
# After code changes:
docker run --rm -v "$(pwd)/legba-ui:/app" -w /app node:20-alpine npm run build
docker build -f docker/ui-v2.Dockerfile -t legba-ui-v2:dev .
docker stop legba-ui-v2 && docker rm legba-ui-v2
docker run -d --name legba-ui-v2 --network legba_default \
  -p 8503:8503 --restart unless-stopped legba-ui-v2:dev
```

## Ports

| Service | Port | Description |
|---------|------|-------------|
| UI v2 (nginx) | 8503 | React SPA + API proxy |
| UI v1 (FastAPI) | 8501 | Legacy htmx UI + JSON API backend |
| Vite dev | 5173 | Development only (HMR) |
