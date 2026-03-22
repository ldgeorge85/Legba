#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# merge_claude_data.sh — Merge entity profiles, events, and event-entity links
# from the Claude Postgres instance into the Main Postgres instance.
#
# Safe & idempotent:
#   - Entities matched by canonical_name (case-insensitive) AND aliases
#   - Events deduped by >50% word overlap in title
#   - ON CONFLICT DO NOTHING for all inserts
#   - source_id set to NULL on imported events (Claude source IDs don't exist in Main)
#   - Dry-run mode by default; pass --apply to actually write
#
# Usage:
#   ./merge_claude_data.sh              # dry-run: shows what would be merged
#   ./merge_claude_data.sh --apply      # actually perform the merge
# ---------------------------------------------------------------------------
set -euo pipefail

CLAUDE_CONTAINER="legba-claude-postgres-1"
MAIN_CONTAINER="legba-postgres-1"
DB_USER="legba"
DB_NAME="legba"

APPLY=false
if [[ "${1:-}" == "--apply" ]]; then
    APPLY=true
fi

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

# ---------------------------------------------------------------------------
# Helper: run psql on a container
# ---------------------------------------------------------------------------
claude_psql() {
    docker exec "$CLAUDE_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -t -A -F $'\t' -c "$1"
}

main_psql() {
    docker exec "$MAIN_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -t -A -F $'\t' -c "$1"
}

main_psql_exec() {
    docker exec -i "$MAIN_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME"
}

# ---------------------------------------------------------------------------
# Step 0: Verify connectivity
# ---------------------------------------------------------------------------
echo "=== Verifying database connectivity ==="
claude_count=$(claude_psql "SELECT count(*) FROM entity_profiles;")
main_count=$(main_psql "SELECT count(*) FROM entity_profiles;")
echo "  Claude entities: $claude_count"
echo "  Main entities:   $main_count"

claude_ev=$(claude_psql "SELECT count(*) FROM events;")
main_ev=$(main_psql "SELECT count(*) FROM events;")
echo "  Claude events:   $claude_ev"
echo "  Main events:     $main_ev"

claude_links=$(claude_psql "SELECT count(*) FROM event_entity_links;")
main_links=$(main_psql "SELECT count(*) FROM event_entity_links;")
echo "  Claude links:    $claude_links"
echo "  Main links:      $main_links"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Export data from both databases as JSON
# ---------------------------------------------------------------------------
echo "=== Exporting data ==="

# Export Claude entities
claude_psql "
SELECT json_build_object(
    'id', id::text,
    'canonical_name', canonical_name,
    'entity_type', entity_type,
    'version', version,
    'completeness_score', completeness_score,
    'data', data::text
)
FROM entity_profiles
ORDER BY canonical_name;
" > "$TMPDIR/claude_entities.jsonl"
echo "  Exported $(wc -l < "$TMPDIR/claude_entities.jsonl") Claude entities"

# Export Main entities (names + aliases for matching)
main_psql "
SELECT json_build_object(
    'id', id::text,
    'canonical_name', canonical_name,
    'entity_type', entity_type,
    'summary', COALESCE(data->>'summary', ''),
    'aliases', COALESCE(data->'aliases', '[]'::jsonb)::text
)
FROM entity_profiles
ORDER BY canonical_name;
" > "$TMPDIR/main_entities.jsonl"
echo "  Exported $(wc -l < "$TMPDIR/main_entities.jsonl") Main entities"

# Export Claude events
claude_psql "
SELECT json_build_object(
    'id', id::text,
    'title', title,
    'source_id', source_id::text,
    'source_url', source_url,
    'category', category,
    'event_timestamp', event_timestamp::text,
    'language', language,
    'confidence', confidence,
    'data', data::text
)
FROM events
ORDER BY created_at;
" > "$TMPDIR/claude_events.jsonl"
echo "  Exported $(wc -l < "$TMPDIR/claude_events.jsonl") Claude events"

# Export Main event titles for dedup
main_psql "
SELECT json_build_object(
    'id', id::text,
    'title', title
)
FROM events
ORDER BY title;
" > "$TMPDIR/main_events.jsonl"
echo "  Exported $(wc -l < "$TMPDIR/main_events.jsonl") Main events"

# Export Claude event-entity links
claude_psql "
SELECT json_build_object(
    'event_id', event_id::text,
    'entity_id', entity_id::text,
    'role', role,
    'confidence', confidence
)
FROM event_entity_links;
" > "$TMPDIR/claude_links.jsonl"
echo "  Exported $(wc -l < "$TMPDIR/claude_links.jsonl") Claude links"
echo ""

# ---------------------------------------------------------------------------
# Step 2: Process with Python — dedup, match, generate SQL
# ---------------------------------------------------------------------------
echo "=== Processing merge logic ==="

python3 << 'PYTHON_SCRIPT' - "$TMPDIR" "$APPLY"
import json
import sys
import os
import re
from uuid import uuid4
from collections import defaultdict

tmpdir = sys.argv[1]
apply_mode = sys.argv[2] == "True"

# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------
def load_jsonl(path):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows

claude_entities = load_jsonl(os.path.join(tmpdir, "claude_entities.jsonl"))
main_entities = load_jsonl(os.path.join(tmpdir, "main_entities.jsonl"))
claude_events = load_jsonl(os.path.join(tmpdir, "claude_events.jsonl"))
main_events = load_jsonl(os.path.join(tmpdir, "main_events.jsonl"))
claude_links = load_jsonl(os.path.join(tmpdir, "claude_links.jsonl"))

# ---------------------------------------------------------------------------
# Build Main entity index (name -> id, alias -> id)
# ---------------------------------------------------------------------------
main_entity_by_name = {}  # lower(name) -> {id, canonical_name, summary}
main_entity_aliases = {}  # lower(alias) -> canonical_name

for e in main_entities:
    name_lower = e["canonical_name"].strip().lower()
    main_entity_by_name[name_lower] = e
    aliases = json.loads(e["aliases"]) if isinstance(e["aliases"], str) else e["aliases"]
    for alias in aliases:
        main_entity_aliases[alias.strip().lower()] = name_lower

# Known equivalent names: Claude name -> Main name (manual mappings)
KNOWN_EQUIVALENTS = {
    "uae": "united arab emirates",
    "pope leo xiv": "pope leo",
    "canada pm": "justin trudeau",  # Claude has "Canada PM", Main has "Justin Trudeau"? No, check...
    "norway police": "oslo police",
    "who": "world health organization",  # Claude has both; skip WHO if WH org exists
}

# Check if Main has Justin Trudeau
if "justin trudeau" not in main_entity_by_name:
    del KNOWN_EQUIVALENTS["canada pm"]

# Check if Main has World Health Organization
if "world health organization" not in main_entity_by_name:
    # Neither exists in Main — don't map
    if "who" in KNOWN_EQUIVALENTS:
        del KNOWN_EQUIVALENTS["who"]

# Check if Main has Oslo Police
if "oslo police" not in main_entity_by_name:
    if "norway police" in KNOWN_EQUIVALENTS:
        del KNOWN_EQUIVALENTS["norway police"]

def find_main_match(claude_name, claude_aliases=None):
    """Find if a Claude entity matches any Main entity by name or alias."""
    name_lower = claude_name.strip().lower()

    # Direct name match
    if name_lower in main_entity_by_name:
        return main_entity_by_name[name_lower]

    # Known equivalent
    if name_lower in KNOWN_EQUIVALENTS:
        equiv = KNOWN_EQUIVALENTS[name_lower]
        if equiv in main_entity_by_name:
            return main_entity_by_name[equiv]

    # Check if Claude name appears as alias in Main
    if name_lower in main_entity_aliases:
        main_name = main_entity_aliases[name_lower]
        return main_entity_by_name[main_name]

    # Reverse: check if any of Claude entity's aliases match Main entity names
    if claude_aliases:
        for alias in claude_aliases:
            alias_lower = alias.strip().lower()
            if alias_lower in main_entity_by_name:
                return main_entity_by_name[alias_lower]
            if alias_lower in main_entity_aliases:
                main_name = main_entity_aliases[alias_lower]
                return main_entity_by_name[main_name]

    return None

# ---------------------------------------------------------------------------
# Process entities
# ---------------------------------------------------------------------------
# Track: claude_entity_id -> main_entity_id (for link remapping)
entity_id_map = {}  # claude_id -> main_id (existing or new)
entities_to_insert = []  # (new_id, data_json, canonical_name, entity_type, version, completeness)
entities_to_update_summary = []  # (main_id, new_summary)
entities_skipped = []
entities_matched = []

# Events to skip
SKIP_EVENT_TITLES = {"test postgresql connection"}

# The second "Shield" event to skip (keep first, skip second)
SHIELD_SKIP_ID = "85fa5553-ab79-48b8-a803-8a15ed374667"

for ce in claude_entities:
    claude_id = ce["id"]
    claude_name = ce["canonical_name"]
    claude_data = json.loads(ce["data"]) if isinstance(ce["data"], str) else ce["data"]
    claude_summary = claude_data.get("summary", "")

    claude_aliases = claude_data.get("aliases", [])
    main_match = find_main_match(claude_name, claude_aliases)

    if main_match:
        # Entity exists in Main
        main_id = main_match["id"]
        entity_id_map[claude_id] = main_id

        main_summary = main_match.get("summary", "")

        # Merge summary if Claude has one and Main doesn't
        if claude_summary and not main_summary:
            entities_to_update_summary.append((main_id, claude_summary))
            entities_matched.append(f"  MATCH+UPDATE {claude_name} -> {main_match['canonical_name']} (add summary)")
        else:
            entities_matched.append(f"  MATCH {claude_name} -> {main_match['canonical_name']}")
    else:
        # Claude also has internal duplicates: WHO and World Health Organization
        # Skip WHO if we're also importing World Health Organization
        claude_names_lower = [e["canonical_name"].strip().lower() for e in claude_entities]
        if claude_name.strip().lower() == "who" and "world health organization" in claude_names_lower:
            # Find the WHO's data to merge into World Health Organization later
            entities_skipped.append(f"  SKIP (internal dup) {claude_name} -> will merge into World Health Organization")
            # Map WHO to whatever World Health Organization gets mapped to (handle later)
            entity_id_map[claude_id] = "__WHO_DEFER__"
            continue

        # New entity — generate new UUID to avoid conflicts
        new_id = str(uuid4())
        entity_id_map[claude_id] = new_id

        # Update the data JSON to use the new ID
        claude_data["id"] = new_id

        entities_to_insert.append({
            "id": new_id,
            "data": json.dumps(claude_data),
            "canonical_name": claude_name,
            "entity_type": claude_data.get("entity_type", "other"),
            "version": claude_data.get("version", 1),
            "completeness_score": claude_data.get("completeness_score", 0.0),
        })

# Resolve WHO defer — find what World Health Organization mapped to
for ce in claude_entities:
    if ce["canonical_name"].strip().lower() == "world health organization":
        who_target = entity_id_map.get(ce["id"])
        # Now update all WHO defers
        for cid, mid in list(entity_id_map.items()):
            if mid == "__WHO_DEFER__":
                entity_id_map[cid] = who_target
        break

# ---------------------------------------------------------------------------
# Word overlap dedup for events
# ---------------------------------------------------------------------------
def word_set(title):
    """Extract meaningful words from title."""
    words = re.findall(r"[a-z0-9]+", title.lower())
    # Remove very common words
    stop = {"the", "a", "an", "in", "on", "at", "to", "of", "and", "or", "as", "is", "it", "for", "by", "with", "from", "that", "this", "are", "was", "be", "has", "have", "had"}
    return set(w for w in words if w not in stop)

def word_overlap(title1, title2):
    """Calculate word overlap ratio between two titles."""
    w1 = word_set(title1)
    w2 = word_set(title2)
    if not w1 or not w2:
        return 0.0
    intersection = w1 & w2
    # Overlap relative to smaller set (so short titles can still match)
    min_len = min(len(w1), len(w2))
    return len(intersection) / min_len if min_len > 0 else 0.0

main_titles = [e["title"] for e in main_events]

# ---------------------------------------------------------------------------
# Process events
# ---------------------------------------------------------------------------
events_to_insert = []
events_skipped = []
events_deduped = []
imported_event_ids = set()  # Claude event IDs that were imported

for ce in claude_events:
    claude_event_id = ce["id"]
    title = ce["title"]

    # Skip test events
    if title.strip().lower() in SKIP_EVENT_TITLES:
        events_skipped.append(f"  SKIP (test) {title}")
        continue

    # Skip the second Shield of Americas duplicate
    if claude_event_id == SHIELD_SKIP_ID:
        events_skipped.append(f"  SKIP (shield dup) {title}")
        continue

    # Check word overlap with ALL main events
    is_dup = False
    for mt in main_titles:
        overlap = word_overlap(title, mt)
        if overlap > 0.5:
            events_deduped.append(f"  DEDUP {title}\n         matches: {mt} (overlap={overlap:.2f})")
            is_dup = True
            break

    if is_dup:
        continue

    # Also check against already-accepted Claude events (avoid internal dups)
    accepted_titles = [e["title"] for e in events_to_insert]
    for at in accepted_titles:
        overlap = word_overlap(title, at)
        if overlap > 0.5:
            events_deduped.append(f"  DEDUP (internal) {title}\n         matches: {at} (overlap={overlap:.2f})")
            is_dup = True
            break

    if is_dup:
        continue

    # New event — generate new UUID, null out source_id
    new_event_id = str(uuid4())
    event_data = json.loads(ce["data"]) if isinstance(ce["data"], str) else ce["data"]
    event_data["id"] = new_event_id
    event_data["source_id"] = None  # Null out source_id

    events_to_insert.append({
        "id": new_event_id,
        "old_id": claude_event_id,
        "title": title,
        "data": json.dumps(event_data),
        "source_url": event_data.get("source_url", ""),
        "category": event_data.get("category", "other"),
        "event_timestamp": ce.get("event_timestamp"),
        "language": event_data.get("language", "en"),
        "confidence": event_data.get("confidence", 0.5),
    })
    imported_event_ids.add(claude_event_id)

# Build event ID map: claude_event_id -> new_event_id
event_id_map = {}
for ev in events_to_insert:
    event_id_map[ev["old_id"]] = ev["id"]

# ---------------------------------------------------------------------------
# Process event-entity links
# ---------------------------------------------------------------------------
links_to_insert = []
links_skipped = []

for link in claude_links:
    claude_event_id = link["event_id"]
    claude_entity_id = link["entity_id"]

    # Only import links for events that we're actually importing
    if claude_event_id not in imported_event_ids:
        links_skipped.append(f"  SKIP link (event not imported) event={claude_event_id[:8]}... entity={claude_entity_id[:8]}...")
        continue

    # Map event and entity IDs
    new_event_id = event_id_map.get(claude_event_id)
    new_entity_id = entity_id_map.get(claude_entity_id)

    if not new_event_id:
        links_skipped.append(f"  SKIP link (no event mapping) event={claude_event_id[:8]}...")
        continue

    if not new_entity_id:
        links_skipped.append(f"  SKIP link (no entity mapping) entity={claude_entity_id[:8]}...")
        continue

    links_to_insert.append({
        "event_id": new_event_id,
        "entity_id": new_entity_id,
        "role": link["role"],
        "confidence": link["confidence"],
    })

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
print("=" * 70)
print("MERGE PLAN")
print("=" * 70)

print(f"\n--- ENTITIES: {len(entities_to_insert)} to insert, {len(entities_to_update_summary)} to update summary ---")
if entities_matched:
    print("\nMatched (already exist in Main):")
    for m in entities_matched:
        print(m)

if entities_skipped:
    print("\nSkipped:")
    for s in entities_skipped:
        print(s)

if entities_to_insert:
    print(f"\nNew entities to INSERT ({len(entities_to_insert)}):")
    for e in entities_to_insert:
        print(f"  + {e['canonical_name']} ({e['entity_type']})")

if entities_to_update_summary:
    print(f"\nEntities to UPDATE summary ({len(entities_to_update_summary)}):")
    for mid, summary in entities_to_update_summary:
        print(f"  ~ {mid[:8]}... <- {summary[:80]}...")

print(f"\n--- EVENTS: {len(events_to_insert)} to insert ---")
if events_skipped:
    print("\nSkipped:")
    for s in events_skipped:
        print(s)

if events_deduped:
    print(f"\nDeduped ({len(events_deduped)}):")
    for d in events_deduped:
        print(d)

if events_to_insert:
    print(f"\nNew events to INSERT ({len(events_to_insert)}):")
    for e in events_to_insert:
        print(f"  + {e['title']}")

print(f"\n--- LINKS: {len(links_to_insert)} to insert, {len(links_skipped)} skipped ---")

print(f"\n{'=' * 70}")
print(f"TOTALS: {len(entities_to_insert)} entities, {len(entities_to_update_summary)} summary updates, "
      f"{len(events_to_insert)} events, {len(links_to_insert)} links")
print(f"{'=' * 70}")

# ---------------------------------------------------------------------------
# Generate SQL
# ---------------------------------------------------------------------------
sql_file = os.path.join(tmpdir, "merge.sql")

with open(sql_file, "w") as f:
    f.write("-- Auto-generated merge SQL\n")
    f.write("-- Generated by merge_claude_data.sh\n")
    f.write("BEGIN;\n\n")

    # Entity inserts
    if entities_to_insert:
        f.write("-- =============================================\n")
        f.write("-- ENTITY PROFILES\n")
        f.write("-- =============================================\n\n")
        for e in entities_to_insert:
            # Escape single quotes in data
            data_escaped = e["data"].replace("'", "''")
            name_escaped = e["canonical_name"].replace("'", "''")
            # Use DO NOTHING to handle both id and name uniqueness conflicts safely
            # Check by lowercase name first to avoid unique index violation
            f.write(f"INSERT INTO entity_profiles (id, data, canonical_name, entity_type, version, completeness_score, created_at, updated_at)\n")
            f.write(f"SELECT '{e['id']}', '{data_escaped}'::jsonb, '{name_escaped}', '{e['entity_type']}', {e['version']}, {e['completeness_score']}, NOW(), NOW()\n")
            f.write(f"WHERE NOT EXISTS (SELECT 1 FROM entity_profiles WHERE LOWER(canonical_name) = LOWER('{name_escaped}'))\n")
            f.write(f"  AND NOT EXISTS (SELECT 1 FROM entity_profiles WHERE id = '{e['id']}');\n\n")

    # Entity summary updates
    if entities_to_update_summary:
        f.write("-- =============================================\n")
        f.write("-- ENTITY SUMMARY UPDATES\n")
        f.write("-- =============================================\n\n")
        for main_id, summary in entities_to_update_summary:
            # json.dumps produces a valid JSON string value (with quotes),
            # then we SQL-escape the whole thing for embedding in the query
            json_value = json.dumps(summary)  # e.g. "The African Union..."
            json_value_sql = json_value.replace("'", "''")
            f.write(f"UPDATE entity_profiles SET\n")
            f.write(f"  data = jsonb_set(data, '{{summary}}', '{json_value_sql}'::jsonb),\n")
            f.write(f"  updated_at = NOW()\n")
            f.write(f"WHERE id = '{main_id}' AND (data->>'summary' IS NULL OR data->>'summary' = '');\n\n")

    # Event inserts
    if events_to_insert:
        f.write("-- =============================================\n")
        f.write("-- EVENTS\n")
        f.write("-- =============================================\n\n")
        for e in events_to_insert:
            data_escaped = e["data"].replace("'", "''")
            title_escaped = e["title"].replace("'", "''")
            source_url_escaped = e["source_url"].replace("'", "''")
            ts = f"'{e['event_timestamp']}'" if e["event_timestamp"] and e["event_timestamp"] != "None" else "NULL"
            f.write(f"INSERT INTO events (id, data, title, source_id, source_url, category, event_timestamp, language, confidence, created_at, updated_at)\n")
            f.write(f"VALUES ('{e['id']}', '{data_escaped}'::jsonb, '{title_escaped}', NULL, '{source_url_escaped}', '{e['category']}', {ts}, '{e['language']}', {e['confidence']}, NOW(), NOW())\n")
            f.write(f"ON CONFLICT (id) DO NOTHING;\n\n")

    # Link inserts
    if links_to_insert:
        f.write("-- =============================================\n")
        f.write("-- EVENT-ENTITY LINKS\n")
        f.write("-- =============================================\n\n")
        for link in links_to_insert:
            f.write(f"INSERT INTO event_entity_links (event_id, entity_id, role, confidence, created_at)\n")
            f.write(f"VALUES ('{link['event_id']}', '{link['entity_id']}', '{link['role']}', {link['confidence']}, NOW())\n")
            f.write(f"ON CONFLICT (event_id, entity_id, role) DO NOTHING;\n\n")

    f.write("COMMIT;\n")

print(f"\nSQL written to: {sql_file}")
PYTHON_SCRIPT

# ---------------------------------------------------------------------------
# Step 3: Apply or dry-run
# ---------------------------------------------------------------------------
SQL_FILE="$TMPDIR/merge.sql"

if [[ ! -f "$SQL_FILE" ]]; then
    echo "ERROR: SQL file was not generated."
    exit 1
fi

SQL_SIZE=$(wc -l < "$SQL_FILE")
echo ""
echo "Generated SQL: $SQL_SIZE lines"

if [[ "$APPLY" == "true" ]]; then
    echo ""
    echo "=== APPLYING MERGE ==="
    echo ""

    # Copy SQL into container and execute
    docker cp "$SQL_FILE" "$MAIN_CONTAINER:/tmp/merge.sql"
    docker exec "$MAIN_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -f /tmp/merge.sql
    docker exec "$MAIN_CONTAINER" rm -f /tmp/merge.sql

    echo ""
    echo "=== POST-MERGE COUNTS ==="
    new_entities=$(main_psql "SELECT count(*) FROM entity_profiles;")
    new_events=$(main_psql "SELECT count(*) FROM events;")
    new_links=$(main_psql "SELECT count(*) FROM event_entity_links;")
    echo "  Main entities: $main_count -> $new_entities"
    echo "  Main events:   $main_ev -> $new_events"
    echo "  Main links:    $main_links -> $new_links"
    echo ""
    echo "=== MERGE COMPLETE ==="
else
    echo ""
    echo "*** DRY RUN — no changes made ***"
    echo "Review the plan above, then run:"
    echo "  ./scripts/merge_claude_data.sh --apply"
    echo ""
    echo "To inspect the generated SQL:"
    echo "  cat $SQL_FILE"
fi
