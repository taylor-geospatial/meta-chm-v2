# meta-chm-v2

Tooling to rebuild Meta's **DINOv3 Global Canopy Height Map v2 (ml3)** as a
cloud-native package and publish it to [source.coop](https://source.coop), without
copying the ~24 TB of source rasters.

**Live tile browser:** <https://research.taylorgeospatial.org/meta-chm-v2/> — a
fully static (serverless) map that filters the STAC GeoParquet in-browser with
duckdb-wasm and links direct COG downloads. Source in [`web/`](web/).

The source dataset (`s3://dataforgood-fb-data/forests/v2/global/dinov3_global_chm_v2_ml3/`)
is 213,109 Web-Mercator COG tiles plus a 56 MB `tiles.geojson` and 208k tiny per-tile
metadata GeoJSONs — good rasters, poor cloud-native ergonomics. This repo produces a thin
companion package that references those COGs in place:

- **`tiles.parquet`** — GeoParquet 1.1 tile index (bbox-covered) replacing `tiles.geojson`
- **STAC** — collection + `items.parquet` (stac-geoparquet), asset hrefs → Meta's COGs
- **GeoZarr** — an [Icechunk](https://icechunk.io) store with a 6-level multiscales pyramid
    (levels 1–6) built from the COGs' internal overviews via
    [kerchunk](https://fsspec.github.io/kerchunk/) + [VirtualiZarr](https://virtualizarr.readthedocs.io),
    holding **only virtual byte-range references** (290.9M chunks, ~3.6 GB, zero pixels copied)

Published at `s3://us-west-2.opendata.source.coop/tge-labs/meta-chm-v2/` under CC-BY-4.0
(data © Meta / Data for Good; see `source_coop/`).

## Layout

```
src/chm_zarr/
  build_tiles.py          # tiles.parquet from tiles.geojson + S3 HEADs
  build_stac.py           # STAC collection + stac-geoparquet items
  build_virtual_zarr.py   # kerchunk COGs -> multiscales Icechunk GeoZarr
  quadkey.py              # quadkey <-> tile/lonlat/3857 helpers
  cli.py                  # chm-build-{tiles,stac,virtual-zarr}
slurm/                    # sbatch scripts (build runs on a CPU node, never the login node)
scripts/                  # validation + GC + ad-hoc icechunk-limit probes
source_coop/              # README.md + LICENSE published to the bucket root
```

## Usage

```bash
uv sync
uv run chm-build-tiles --tiles-geojson tiles.geojson --out out/tiles.parquet
uv run chm-build-stac  --tiles-parquet out/tiles.parquet --out-dir out/stac
# GeoZarr build is heavy (290M refs); run on a CPU node, not the login node:
sbatch slurm/vz_full.sbatch
```

The GeoZarr build needs a large-memory CPU node (~110 GB peak, ~30 min on 64 cores).
See `scripts/validate_from_s3.py` to verify a published store reads correctly (anonymously).

## Build notes

- Header parse uses one bulk range GET per COG fed to kerchunk via an in-memory file
    (kerchunk's own S3 reader does dozens of tiny cross-region reads — ~20× slower).
- Parsing is GIL-bound, so workers are processes (`ProcessPoolExecutor`, spawn).
- Icechunk serializes each manifest as a FlatBuffer capped at 2³¹ bytes (~40M refs), so the
    build configures manifest splitting (2048² shards) and commits refs in 20M batches.
