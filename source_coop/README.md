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

### STAC

This is a **static** STAC catalog: a `collection.json` plus a stac-geoparquet
`items.parquet` (213,109 Items). There is **no STAC API server**, so there are three
access patterns depending on your tool.

**1. Read the collection metadata (`pystac`):**

```python
import pystac
c = pystac.Collection.from_file(
    "https://data.source.coop/tge-labs/meta-chm-v2/stac/collection.json"
)
```

**2. Query the Items by space/time (stac-geoparquet — the scalable path):**

```python
# rustac gives a pystac-client-like search straight over the parquet, no server:
from rustac import DuckdbClient
client = DuckdbClient()
items = client.search(
    "https://data.source.coop/tge-labs/meta-chm-v2/stac/items.parquet",
    bbox=[-122.6, 37.6, -122.3, 37.9],
)
```

Or with DuckDB directly:

```sql
INSTALL spatial; LOAD spatial;
SELECT id, assets FROM read_parquet(
  'https://data.source.coop/tge-labs/meta-chm-v2/stac/items.parquet'
) WHERE bbox.xmin < -122.3 AND bbox.xmax > -122.6;
```

**3. `pystac-client` (requires a STAC API).** `pystac_client.Client.open()` needs a STAC
API endpoint, which a static catalog does not provide. To use it, serve `items.parquet`
behind [`stac-fastapi-geoparquet`](https://github.com/stac-utils/stac-fastapi-geoparquet)
(no database needed), then:

```python
from pystac_client import Client
client = Client.open("https://your-stac-fastapi-geoparquet-host/")
search = client.search(collections=["dinov3-global-chm-v2-ml3"], bbox=[...])
```

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
