# meta-chm-v2

Tooling to rebuild Meta's **DINOv3 Global Canopy Height Map v2 (ml3)** as a
cloud-native package and publish it to [source.coop](https://source.coop), without
copying the ~24 TB of source rasters.

**Live analysis map:** <https://research.taylorgeospatial.org/meta-chm-v2/> — a fully
static (serverless) viewer: raw canopy height streamed as PMTiles over an EOX Sentinel-2
basemap, colorized and measured live in the browser (height thresholds, classes, forest
mask, per-view forest metrics) with no server. Source in [`app/`](app/) — see [`app/README.md`](app/README.md) for dev/build.

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

## Streaming the data (no download)

Every asset is cloud-native and read directly over HTTPS or S3 — no STAC API server, no
mirroring. Asset hrefs point back at Meta's COGs (`s3://dataforgood-fb-data/...`), which are
**anonymous in us-east-1**.

| Asset              | URL                                                                               | Read with                           |
| ------------------ | --------------------------------------------------------------------------------- | ----------------------------------- |
| Tile index         | `…/meta-chm-v2/tiles.parquet`                                                     | geopandas, duckdb                   |
| STAC items         | `…/meta-chm-v2/stac/items.parquet`                                                | rustac, odc.stac, stackstac, pystac |
| STAC collection    | `…/meta-chm-v2/stac/collection.json`                                              | pystac                              |
| GeoZarr (Icechunk) | `s3://us-west-2.opendata.source.coop/tge-labs/meta-chm-v2/zarr/chm.zarr.icechunk` | xarray + icechunk                   |
| Web tiles          | `…/meta-chm-v2/pmtiles/chm_height.pmtiles`                                        | maplibre-gl, pmtiles                |

HTTPS base: `https://data.source.coop/tge-labs/meta-chm-v2/`. Each STAC item also carries a
`datetime` (the tile's latest source acquisition) and `chm:acq_count` for spatiotemporal use.

> ⚠️ **nodata gotcha:** the COGs have **no nodata value** — `0` means *both* "0 m canopy" and
> "no data". True nodata lives in a per-dataset **mask band**, not in the pixel values. To tell
> bare ground from gaps/ocean, read masked (`ds.read(1, masked=True)` or `ds.read_masks(1)`);
> `arr == 0` alone conflates the two.

Runnable versions of the first two snippets are in [`examples/`](examples/) — run them with
`uv run --group examples python examples/<name>.py`.

**STAC search → windowed COG read** (rustac runs the search in-process; no server):

```python
import rasterio
from rasterio.enums import Resampling
from rustac import DuckdbClient

ITEMS = "https://data.source.coop/tge-labs/meta-chm-v2/stac/items.parquet"
items = DuckdbClient().search(ITEMS, collections=["dinov3-global-chm-v2-ml3"],
                              bbox=[13.0, 52.0, 13.4, 52.3], max_items=50)  # Berlin
href = items[0]["assets"]["chm"]["href"]                                   # s3://…/{quadkey}.tif
with rasterio.Env(AWS_NO_SIGN_REQUEST="YES", AWS_REGION="us-east-1"), rasterio.open(href) as ds:
    arr = ds.read(1, out_shape=(1024, 1024), resampling=Resampling.average)  # decimated, no full read
```

**STAC → lazy xarray cube** (odc.stac, native EPSG:3857, dask-backed):

```python
import os, odc.stac, pystac
from rustac import DuckdbClient

os.environ.update(AWS_NO_SIGN_REQUEST="YES", AWS_REGION="us-east-1")
ITEMS = "https://data.source.coop/tge-labs/meta-chm-v2/stac/items.parquet"
bbox = [13.30, 52.45, 13.45, 52.55]
items = [pystac.Item.from_dict(d) for d in DuckdbClient().search(ITEMS, bbox=bbox, max_items=25)]
chm = odc.stac.load(items, bands=["chm"], bbox=bbox, resolution=10,
                    chunks={"x": 2048, "y": 2048})        # reads stream on .compute()
heights = chm["chm"].isel(time=0).compute()
```

(`stackstac.stack(items)` and `pystac-client` work too — items carry the proj + raster
extensions.)

**Tile index only** (skip STAC; pick COGs by geometry):

```python
import fsspec, geopandas as gpd, rasterio
from rasterio.enums import Resampling

# fsspec wrapper: pyarrow won't open an https:// parquet directly
with fsspec.open("https://data.source.coop/tge-labs/meta-chm-v2/tiles.parquet") as f:
    tiles = gpd.read_parquet(f)
hit = tiles.cx[13.3:13.45, 52.45:52.55]                   # spatial filter (lon/lat)
with rasterio.Env(AWS_NO_SIGN_REQUEST="YES", AWS_REGION="us-east-1"), \
     rasterio.open(hit.iloc[0].cog_url) as ds:             # cog_url -> Meta's COG
    arr = ds.read(1, out_shape=(1024, 1024), resampling=Resampling.average)
```

**GeoZarr pyramid** (xarray, anonymous Icechunk; only the touched chunks' byte-ranges move):

```python
import icechunk, xarray as xr

src = "s3://dataforgood-fb-data/"
cfg = icechunk.RepositoryConfig.default()
cfg.set_virtual_chunk_container(
    icechunk.VirtualChunkContainer(src, icechunk.s3_store(region="us-east-1", anonymous=True)))
repo = icechunk.Repository.open(
    icechunk.s3_storage(bucket="us-west-2.opendata.source.coop",
                        prefix="tge-labs/meta-chm-v2/zarr/chm.zarr.icechunk",
                        region="us-west-2", anonymous=True),
    config=cfg,
    authorize_virtual_chunk_access=icechunk.containers_credentials(
        {src: icechunk.s3_anonymous_credentials()}))
dt = xr.open_datatree(repo.readonly_session("main").store, engine="zarr", consolidated=False)
chm = dt["1"]["chm"]                 # level L = 2^L downsample; "1" ≈ 2.4 m/px … "6" ≈ 76 m/px
print(chm.shape)                     # full global pyramid level (EPSG:3857)
sub = chm[y0:y0 + 2048, x0:x0 + 2048]  # index by pixel; only these chunks' byte-ranges stream
```

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
