#!/usr/bin/env python3
"""Dump OpenSearch indices to NDJSON files via scroll API.

Usage: os_dump.py <base_url> <output_dir> [index_prefix]
  base_url:     e.g. http://localhost:9200
  output_dir:   directory to write .ndjson files
  index_prefix: only dump indices matching this prefix (default: "legba-")
"""
import json
import os
import sys
import urllib.request
import urllib.error

def request_json(url, data=None, method=None):
    """Make an HTTP request and return parsed JSON."""
    if data is not None:
        data = json.dumps(data).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        print(f"  WARNING: request failed: {url} — {e}", file=sys.stderr)
        return None

def list_indices(base_url, prefix):
    """Get index names matching prefix."""
    try:
        req = urllib.request.Request(f"{base_url}/_cat/indices?h=index&format=json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return sorted(
            entry["index"] for entry in data
            if entry["index"].startswith(prefix)
        )
    except Exception as e:
        print(f"  WARNING: could not list indices: {e}", file=sys.stderr)
        return []

def dump_index(base_url, index, outfile):
    """Scroll through an index and write all docs as NDJSON."""
    # Initial search
    resp = request_json(
        f"{base_url}/{index}/_search?scroll=5m&size=500",
        data={"query": {"match_all": {}}},
    )
    if not resp:
        return 0

    scroll_id = resp.get("_scroll_id")
    hits = resp.get("hits", {}).get("hits", [])
    total_val = resp.get("hits", {}).get("total", {})
    total = total_val.get("value", 0) if isinstance(total_val, dict) else total_val
    written = 0

    with open(outfile, "w") as f:
        # Write first batch
        for hit in hits:
            f.write(json.dumps(hit) + "\n")
            written += 1

        # Scroll remaining
        while scroll_id and written < total:
            resp = request_json(
                f"{base_url}/_search/scroll",
                data={"scroll": "5m", "scroll_id": scroll_id},
            )
            if not resp:
                break
            hits = resp.get("hits", {}).get("hits", [])
            if not hits:
                break
            scroll_id = resp.get("_scroll_id", scroll_id)
            for hit in hits:
                f.write(json.dumps(hit) + "\n")
                written += 1

    # Clear scroll
    if scroll_id:
        try:
            request_json(
                f"{base_url}/_search/scroll",
                data={"scroll_id": scroll_id},
                method="DELETE",
            )
        except Exception:
            pass

    return written

def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    base_url = sys.argv[1].rstrip("/")
    output_dir = sys.argv[2]
    prefix = sys.argv[3] if len(sys.argv) > 3 else "legba-"

    os.makedirs(output_dir, exist_ok=True)

    indices = list_indices(base_url, prefix)
    if not indices:
        print(f"  No indices matching '{prefix}' found at {base_url}")
        return

    for idx in indices:
        outfile = os.path.join(output_dir, f"{idx}.ndjson")
        count = dump_index(base_url, idx, outfile)
        size = os.path.getsize(outfile)
        size_h = f"{size/1024:.0f}K" if size < 1048576 else f"{size/1048576:.1f}M"
        print(f"  {idx}: {count} docs ({size_h})")

if __name__ == "__main__":
    main()
