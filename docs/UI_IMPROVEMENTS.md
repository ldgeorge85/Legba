# UI Improvements Plan

*Created: 2026-03-09*

---

## 1. Graph Explorer Improvements

**Status:** TODO

- [ ] Increase node repulsion (8000 → 50000), ideal edge length (120 → 250), reduce gravity (0.3 → 0.05)
- [ ] Scale node size by degree (high-degree nodes larger)
- [ ] Add cose-bilkent layout option (better clustering algorithm)
- [ ] Add search box — type to highlight + center on matching node
- [ ] Fit-to-view after layout completes

## 2. Facts View

**Status:** TODO

- [ ] New route: `/facts`
- [ ] Table view: subject, predicate, value, confidence, source_cycle, created_at
- [ ] Search/filter: by subject, predicate, confidence threshold
- [ ] htmx pagination (same pattern as entities/events)
- [ ] Files: `routes/facts.py`, `templates/facts/list.html`, `templates/facts/_rows.html`

## 3. Memory View

**Status:** TODO

- [ ] New route: `/memory`
- [ ] List view: cycle, significance, summary text, collection (short-term/long-term)
- [ ] Toggle between short-term and long-term collections
- [ ] Semantic search via Qdrant embedding endpoint
- [ ] Wire Qdrant client into UI StoreHolder
- [ ] Files: `routes/memory.py`, `templates/memory/list.html`, `templates/memory/_rows.html`

## 4. Data Modification (CRUD)

### 4a. Easy — Sources, Goals, Facts, Memory

**Sources** (add/edit/pause/retire):
- [ ] Inline status toggle (active/paused/retired) via `hx-put`
- [ ] Add source form (name, url, type, reliability, bias, tags)
- [ ] Edit source modal/inline
- [ ] Delete/retire with confirmation

**Goals** (create/edit/complete/abandon):
- [ ] Create goal form (description, type, priority, parent)
- [ ] Status change buttons (complete/abandon/pause) via `hx-put`
- [ ] Edit description/priority inline

**Facts** (edit/delete):
- [ ] Delete fact with confirmation via `hx-delete`
- [ ] Inline edit for value/confidence

**Memory** (delete vectors):
- [ ] Delete episode via Qdrant delete-by-ID
- [ ] Bulk delete (select + delete)

### 4b. Medium — Entities, Events, Graph Edges

**Entities** (edit profile assertions):
- [ ] Edit assertions (JSONB nested data)
- [ ] Add/remove assertions
- [ ] Merge duplicate entities

**Events** (edit/delete):
- [ ] Delete event + cascade entity links
- [ ] Edit event metadata

**Graph edges** (add/remove):
- [ ] Add edge form (source node, relationship, target node)
- [ ] Delete edge with AGE Cypher
- [ ] Inline from graph explorer detail panel
