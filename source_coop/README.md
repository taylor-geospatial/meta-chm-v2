# Meta CHM v2 (ml3) — Cloud-Native Companion

A cloud-native repackaging of Meta's **DINOv3 Global Canopy Height Map v2 (ml3)**:
a global ~1.19 m canopy-height raster (213,109 Web-Mercator tiles, ~23.8 TB of COGs).

This package does **not** copy the 23.8 TB of pixels. Instead it adds the three
things the original distribution lacks for streaming + analysis, and references
Meta's COGs in place:

```
s3://us-west-2.opendata.source.coop/tge-labs/meta-chm-v2/
├── tiles.parquet            # GeoParquet 1.1 tile index (213,109 rows, bbox-indexed)
├── stac/
│   ├── collection.json      # STAC Collection (CC-BY-4.0)
│   ├── items.parquet        # stac-geoparquet — one Item per tile, assets → Meta's S3
│   └── items_sample/        # ~200 sample STAC Item JSONs (spec inspection)
├── zarr/
│   └── chm.zarr.icechunk    # VirtualiZarr GeoZarr (Icechunk), multiscales L1–L6,
│                            #   zero-copy byte-range refs into Meta's COGs
├── README.md
└── LICENSE
```

## What each artifact is for

| Artifact                 | Use it for                                                                                                                                                                                                              |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tiles.parquet`          | Fast spatial lookup of which tile covers an AOI; drives maplibre/deck.gl tile loaders and DuckDB spatial queries. Replaces the original 56 MB `tiles.geojson`.                                                          |
| `stac/`                  | Discovery + access via STAC tooling (pystac, stac-fastapi, TiTiler, odc-stac). Asset hrefs point at Meta's COGs (`s3://dataforgood-fb-data/...`).                                                                       |
| `zarr/chm.zarr.icechunk` | `xarray`/`dask` analytics and Zarr-native multiscale visualization (Earthmover Arraylake, ZarrViz). Opens as a 6-level pyramid; reads translate to byte-range GETs against Meta's COGs — no pixels are duplicated here. |

## CRS & grid

- **EPSG:3857** (Web Mercator) — matches the source; zero reprojection for web maps.
- 10-character Bing/Microsoft quadkey tile grid (zoom 10). Native pixel ≈ 1.19 m at the equator.
- Pixel values are **canopy height in meters** (uint8, 0–255). `0` is both true-zero and
    no-data (the source sets no explicit nodata mask).

## Quickstart

### Tile index (DuckDB)

```sql
INSTALL spatial; LOAD spatial;
SELECT quadkey, tile_size_bytes
FROM 's3://us-west-2.opendata.source.coop/tge-labs/meta-chm-v2/tiles.parquet'
WHERE bbox_3857.minx < 1000000 AND bbox_3857.maxx > 0;  -- AOI filter
```

### STAC — serverless, cloud-native search

This is a **static** STAC catalog: a `collection.json` plus a stac-geoparquet
`items.parquet` (213,109 Items). **No STAC API server is required.** The parquet is
written in 54 quadkey-sorted (Z-order) row groups with per-group bbox statistics, so a
spatial query reads only the matching byte ranges over HTTP — e.g. a city-scale bbox
touches ~3 of 54 row groups, skipping ~94% of the file. Point any of these tools straight
at the public URL:

```
https://data.source.coop/tge-labs/meta-chm-v2/stac/items.parquet
```

**Collection metadata (`pystac`):**

```python
import pystac
c = pystac.Collection.from_file(
    "https://data.source.coop/tge-labs/meta-chm-v2/stac/collection.json"
)
```

**Search the Items — pick your tool, all stream + prune row groups:**

```python
# rustac — a pystac-client-like search, in-process, no server
from rustac import DuckdbClient
items = DuckdbClient().search(ITEMS_URL, bbox=[13.0, 52.0, 13.4, 52.3], max_items=100)
```

```sql
-- DuckDB (httpfs range reads + row-group pruning)
INSTALL spatial; LOAD spatial;
SELECT id, assets FROM read_parquet('https://data.source.coop/tge-labs/meta-chm-v2/stac/items.parquet')
WHERE bbox.xmin <= 13.4 AND bbox.xmax >= 13.0 AND bbox.ymin <= 52.3 AND bbox.ymax >= 52.0;
```

```python
# pyarrow.dataset — predicate pushdown over the remote file
import pyarrow.dataset as ds, pyarrow.compute as pc
t = ds.dataset(ITEMS_URL, format="parquet").to_table(
    filter=(pc.field("bbox", "xmin") <= 13.4) & (pc.field("bbox", "xmax") >= 13.0)
)
```

Also works: **geopandas** `read_parquet(..., bbox=...)`, **Polars** `scan_parquet`, and the
**stac-geoparquet** library. For teams that specifically need the `pystac-client` API
(`Client.open(...).search(...)`), serve `items.parquet` behind
[`stac-fastapi-geoparquet`](https://github.com/stac-utils/stac-fastapi-geoparquet) — no
database required.

**Search → xarray (`odc.stac`):** turn found Items into a lazily-loaded, dask-backed
`xarray` cube reading the COGs directly (EPSG:3857, no reprojection):

```python
import os, pystac, odc.stac
from rustac import DuckdbClient

os.environ.update(AWS_NO_SIGN_REQUEST="YES", AWS_REGION="us-east-1")  # Meta's COGs are anonymous
bbox = [13.30, 52.45, 13.45, 52.55]
items = [pystac.Item.from_dict(d) for d in DuckdbClient().search(ITEMS_URL, bbox=bbox)]
chm = odc.stac.load(items, bands=["chm"], bbox=bbox, resolution=10, chunks={"x": 2048, "y": 2048})
heights = chm["chm"].isel(time=0).compute()  # canopy height in meters
```

See `examples/search_and_read.py` and `examples/odc_stac_load.py` in the
[chm-zarr](https://github.com/isaaccorley/chm-zarr) repo.

### Virtual GeoZarr (xarray + Icechunk)

```python
import icechunk, xarray as xr
prefix = "s3://dataforgood-fb-data/"
cfg = icechunk.RepositoryConfig.default()
cfg.set_virtual_chunk_container(
    icechunk.VirtualChunkContainer(prefix, icechunk.s3_store(region="us-east-1", anonymous=True))
)
repo = icechunk.Repository.open(
    icechunk.s3_storage(
        bucket="us-west-2.opendata.source.coop",
        prefix="tge-labs/meta-chm-v2/zarr/chm.zarr.icechunk",
        region="us-west-2", anonymous=True,
    ),
    config=cfg,
    authorize_virtual_chunk_access=icechunk.containers_credentials(
        {prefix: icechunk.s3_anonymous_credentials()}
    ),
)
dt = xr.open_datatree(repo.readonly_session("main").store, engine="zarr", consolidated=False)
chm_l3 = dt["3"]["chm"]   # ~9.6 m/px level
```

## Provenance & license

CC-BY-4.0. Underlying data © Meta / Data for Good; repackaging by Taylor
Geospatial Engine Labs. See `LICENSE`. Cite Tolan et al. (2024),
https://arxiv.org/abs/2304.07213.

The VirtualiZarr store contains only chunk references (offsets/lengths) into
Meta's public COGs — if Meta's bucket moves, the Zarr reads break by design.
