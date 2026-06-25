# UI v4 basemap (self-hosted PMTiles)

The World map (`/world`) draws its basemap from a single self-hosted PMTiles file
placed here as **`basemap.pmtiles`** — no external tile server, no API key. Caddy
serves it at `/tiles/basemap.pmtiles` via HTTP **range requests**: clients fetch
only the byte-ranges of tiles currently in view, never the whole file. Until the
file is present, the map falls back to the external demotiles style (flagged).

## Getting a reduced-zoom world build (single-digit GB — our use is world→country→city)

Install the `pmtiles` CLI (https://github.com/protomaps/go-pmtiles), then either:

- **Subset a planet build to lower max-zoom** (smaller file):

      pmtiles extract https://build.protomaps.com/<YYYYMMDD>.pmtiles basemap.pmtiles --maxzoom=10

- or download a prebuilt reduced extract from Protomaps' builds.

Place `basemap.pmtiles` in this directory (or point `LEGBA_BASEMAP_DIR` elsewhere),
then republish Caddy:

      docker compose up -d --force-recreate legba-caddy

The minimal dark style (`src/lib/basemap.ts`) renders land/water/boundaries from
the `earth`/`water`/`boundaries` source-layers; the World map layers labels
(self-hosted glyphs) on top. Upgrade to a fuller (higher max-zoom) build later if
street-level detail is wanted.

Map data © OpenStreetMap contributors (ODbL) — attribution shown on the map.
