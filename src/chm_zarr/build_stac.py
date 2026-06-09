"""Build STAC collection + Items + stac-geoparquet from tiles.parquet.

Outputs:
    out/stac/collection.json
    out/stac/items.parquet        # canonical stac-geoparquet, one row per Item (213k rows)
    out/stac/items_sample/{qk[:4]}/{quadkey}.json   # ~200 sample JSON Items for inspection

Asset hrefs point at the source.coop CORS mirror of the COGs (self-contained).
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyarrow.parquet as pq
import pystac
import stac_geoparquet
from pystac.extensions.projection import ProjectionExtension
from pystac.extensions.raster import DataType, RasterBand, RasterExtension
from shapely.geometry import mapping
from tqdm import tqdm

from . import DST_HTTPS_BASE

# Catalog navigation links use the public HTTPS endpoint so HTTP clients can traverse.
DST_STAC_BASE = f"{DST_HTTPS_BASE}/stac"
DST_COLLECTION_URL = f"{DST_STAC_BASE}/collection.json"
DST_ITEMS_PQ_URL = f"{DST_STAC_BASE}/items.parquet"
DST_COG_HREF = f"{DST_HTTPS_BASE}/chm"  # CORS-enabled COG mirror on source.coop
DST_ZARR_URL = f"{DST_HTTPS_BASE}/zarr/chm.zarr.icechunk"

# Rows per parquet row group. Small enough that bbox-filtered reads prune most groups,
# large enough to keep per-group overhead low. 213k items / 4000 ≈ 53 groups.
ROW_GROUP_SIZE = 4000

COLLECTION_ID = "dinov3-global-chm-v2-ml3"
COLLECTION_TITLE = "Meta CHM v2 (DINOv3 global, ml3) — cloud-native companion"
COLLECTION_DESCRIPTION = (
    "Per-tile STAC Items for Meta's global canopy height map v2 (DINOv3-based, model ml3). "
    "The ~24 TB of source COGs are mirrored on source.coop (CORS-enabled, EPSG:3857) and the "
    "asset hrefs point there; the package also adds a GeoParquet tile index and a self-contained "
    "multiscale GeoZarr (Icechunk virtual refs, native 1.19 m down to ~76 m)."
)
TEMPORAL_EXTENT = (
    datetime(2018, 1, 1, tzinfo=UTC),
    datetime(2024, 12, 31, tzinfo=UTC),
)
R_MERC = 20037508.342789244
PX_3857_NATIVE = 2 * R_MERC / (1024 * 32768)  # ≈ 1.1943 m/px at equator


def _build_collection(spatial_bbox: list[float], temporal: tuple) -> pystac.Collection:
    coll = pystac.Collection(
        id=COLLECTION_ID,
        title=COLLECTION_TITLE,
        description=COLLECTION_DESCRIPTION,
        license="CC-BY-4.0",
        extent=pystac.Extent(
            spatial=pystac.SpatialExtent([spatial_bbox]),
            temporal=pystac.TemporalExtent([list(temporal)]),
        ),
        providers=[
            pystac.Provider(
                name="Meta AI / DataForGood",
                roles=[
                    pystac.ProviderRole.PRODUCER,
                    pystac.ProviderRole.LICENSOR,
                ],
                url="https://dataforgood.facebook.com/dfg/tools/canopy-height-maps",
            ),
            pystac.Provider(
                name="Taylor Geospatial",
                roles=[pystac.ProviderRole.PROCESSOR, pystac.ProviderRole.HOST],
                url="https://taylorgeospatial.org",
            ),
        ],
        keywords=["canopy height", "forest", "lidar", "satellite", "global", "DINOv3", "Meta"],
    )
    coll.add_link(
        pystac.Link(
            rel="cite-as",
            target="https://arxiv.org/abs/2304.07213",
            title="Tolan et al., 2024",
        )
    )
    coll.add_link(
        pystac.Link(
            rel="cite-as",
            target="https://arxiv.org/abs/2603.06382",
            title="Brandt et al., 2026 (CHMv2)",
        )
    )
    # Collection-level asset so the multiscale GeoZarr is discoverable from the catalog.
    coll.add_asset(
        "geozarr",
        pystac.Asset(
            href=DST_ZARR_URL,
            media_type="application/vnd.zarr",
            title="Multiscale GeoZarr (Icechunk, virtual)",
            roles=["data", "overview"],
            extra_fields={
                "description": (
                    "Self-contained Icechunk virtual GeoZarr; groups 1x (native 1.19 m) .. 64x. "
                    "Open anonymously with icechunk + xarray, or via the Arraylake repo "
                    "'taylor-geospatial/meta-chm-v2'."
                )
            },
        ),
    )
    return coll


def _build_item(row: dict) -> pystac.Item:
    qk = row["quadkey"]
    z, x, y = int(row["z"]), int(row["x"]), int(row["y"])
    geom = mapping(row["geometry"])
    bbox = list(row["geometry"].bounds)
    bb3857 = row["bbox_3857"]
    minx, miny, maxx, maxy = (
        bb3857["minx"],
        bb3857["miny"],
        bb3857["maxx"],
        bb3857["maxy"],
    )

    # A tile is a spatial mosaic of single-date images spanning years; we use the LATEST
    # acquisition as the tile's "as-of" observation date (a single instant). chm:acq_count
    # records how many distinct source dates the tile actually mosaics.
    dt = row["dt"].to_pydatetime()  # tz-aware datetime; fillna guarantees non-null
    item = pystac.Item(
        id=qk,
        geometry=geom,
        bbox=bbox,
        datetime=dt,
        properties={
            "tile:quadkey": qk,
            "tile:z": z,
            "tile:x": x,
            "tile:y": y,
            "chm:acq_count": int(row["acq_n"]),
        },
        collection=COLLECTION_ID,
    )

    proj = ProjectionExtension.ext(item, add_if_missing=True)
    proj.epsg = 3857
    proj.shape = [32768, 32768]
    proj.bbox = [minx, miny, maxx, maxy]
    px = (maxx - minx) / 32768
    proj.transform = [px, 0.0, minx, 0.0, -px, maxy, 0.0, 0.0, 1.0]

    asset = pystac.Asset(
        href=f"{DST_COG_HREF}/{qk}.tif",
        media_type=pystac.MediaType.COG,
        title="Canopy height (meters, uint8)",
        roles=["data"],
        extra_fields={
            "file:size": int(row["tile_size_bytes"]) if row["tile_size_bytes"] else None,
        },
    )
    item.add_asset("chm", asset)

    rast = RasterExtension.ext(item.assets["chm"], add_if_missing=True)
    rast.bands = [
        RasterBand.create(
            data_type=DataType.UINT8,
            unit="meter",
            spatial_resolution=px,
            nodata=None,
        )
    ]
    return item


def build(
    tiles_parquet: Path, out_dir: Path, acq_dates: Path | None = None, sample_json: int = 200
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"reading {tiles_parquet}")
    gdf = gpd.read_parquet(tiles_parquet)
    print(f"  {len(gdf):,} tiles")

    # Join per-tile acquisition dates (min/max/count). Tiles with no source metadata fall
    # back to the global window so a time query still includes them.
    if acq_dates is not None:
        adf = pd.read_parquet(acq_dates, columns=["quadkey", "acq_end", "acq_n"])
        adf["dt"] = pd.to_datetime(adf["acq_end"], utc=True)  # latest acquisition per tile
        gdf = gdf.merge(adf[["quadkey", "dt", "acq_n"]], on="quadkey", how="left")
    else:
        gdf["dt"] = pd.NaT
        gdf["acq_n"] = 0
    gmin = pd.Timestamp(gdf["dt"].min())
    gmax = pd.Timestamp(gdf["dt"].max())
    if pd.isna(gmin):
        gmin, gmax = pd.Timestamp(TEMPORAL_EXTENT[0]), pd.Timestamp(TEMPORAL_EXTENT[1])
    gdf["dt"] = gdf["dt"].fillna(gmax)  # undated tiles -> global latest
    gdf["acq_n"] = gdf["acq_n"].fillna(0).astype(int)
    n_dated = int((gdf["acq_n"] > 0).sum())
    print(f"  acquisition (latest per tile): {n_dated:,} dated, range {gmin.date()}..{gmax.date()}")

    total_bbox = [float(v) for v in gdf.total_bounds]
    coll = _build_collection(total_bbox, (gmin.to_pydatetime(), gmax.to_pydatetime()))
    coll.set_self_href(DST_COLLECTION_URL)
    coll.add_link(
        pystac.Link(
            rel="item",
            target=DST_ITEMS_PQ_URL,
            media_type="application/vnd.apache.parquet",
            title="STAC Items (stac-geoparquet)",
        )
    )
    coll_path = out_dir / "collection.json"
    coll_path.write_text(json.dumps(coll.to_dict(include_self_link=True), indent=2))
    print(f"wrote {coll_path}")

    sample_dir = out_dir / "items_sample"
    sample_dir.mkdir(exist_ok=True)
    written_json = 0

    item_dicts: list[dict] = []
    for row in tqdm(gdf.itertuples(index=False), total=len(gdf), desc="items"):
        rd = dict(zip(gdf.columns, row, strict=True))
        item = _build_item(rd)
        d = item.to_dict(include_self_link=False, transform_hrefs=False)
        item_dicts.append(d)
        if written_json < sample_json:
            qk = rd["quadkey"]
            shard = sample_dir / qk[:4]
            shard.mkdir(exist_ok=True)
            (shard / f"{qk}.json").write_text(json.dumps(d, separators=(",", ":")))
            written_json += 1

    items_pq = out_dir / "items.parquet"
    print(f"writing stac-geoparquet {items_pq}")
    # Items are quadkey-sorted (Bing quadkeys = Z-order curve → spatial locality). Write
    # many small row groups so each carries tight bbox min/max stats; a bbox query then
    # range-reads only the matching groups (cloud-native streaming) instead of the whole
    # file. to_parquet writes one row group per input batch, so rebatch to ROW_GROUP rows.
    table = stac_geoparquet.arrow.parse_stac_items_to_arrow(item_dicts).read_all()
    reader = table.to_reader(max_chunksize=ROW_GROUP_SIZE)
    stac_geoparquet.arrow.to_parquet(
        reader,
        items_pq,
        compression="zstd",
        compression_level=13,
    )
    n_groups = pq.ParquetFile(items_pq).metadata.num_row_groups
    print(
        f"done — {len(item_dicts):,} items in {n_groups} row groups, "
        f"{written_json} sample JSONs in {sample_dir}/"
    )
