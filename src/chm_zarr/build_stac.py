"""Build STAC collection + Items + stac-geoparquet from tiles.parquet.

Outputs:
    out/stac/collection.json
    out/stac/items.parquet        # canonical stac-geoparquet, one row per Item (213k rows)
    out/stac/items_sample/{qk[:4]}/{quadkey}.json   # ~200 sample JSON Items for inspection

Asset hrefs reference Meta's S3 bucket directly (no mirror).
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import geopandas as gpd
import pystac
import stac_geoparquet
from pystac.extensions.projection import ProjectionExtension
from pystac.extensions.raster import DataType, RasterBand, RasterExtension
from shapely.geometry import mapping
from tqdm import tqdm

from . import DST_HTTPS_BASE, SRC_COG_PREFIX

# Catalog navigation links use the public HTTPS endpoint so HTTP clients can traverse.
DST_STAC_BASE = f"{DST_HTTPS_BASE}/stac"
DST_COLLECTION_URL = f"{DST_STAC_BASE}/collection.json"
DST_ITEMS_PQ_URL = f"{DST_STAC_BASE}/items.parquet"

COLLECTION_ID = "dinov3-global-chm-v2-ml3"
COLLECTION_TITLE = "Meta CHM v2 (DINOv3 global, ml3) — cloud-native companion"
COLLECTION_DESCRIPTION = (
    "Per-tile STAC Items for Meta's global canopy height map v2 (DINOv3-based, model ml3). "
    "Asset hrefs point at Meta's public S3 bucket; this package adds a GeoParquet tile index "
    "and a VirtualiZarr GeoZarr view without remirroring the underlying ~24 TB of COGs."
)
TEMPORAL_EXTENT = (
    datetime(2018, 1, 1, tzinfo=UTC),
    datetime(2024, 12, 31, tzinfo=UTC),
)
R_MERC = 20037508.342789244
PX_3857_NATIVE = 2 * R_MERC / (1024 * 32768)  # ≈ 1.1943 m/px at equator


def _build_collection(spatial_bbox: list[float]) -> pystac.Collection:
    coll = pystac.Collection(
        id=COLLECTION_ID,
        title=COLLECTION_TITLE,
        description=COLLECTION_DESCRIPTION,
        license="CC-BY-4.0",
        extent=pystac.Extent(
            spatial=pystac.SpatialExtent([spatial_bbox]),
            temporal=pystac.TemporalExtent([list(TEMPORAL_EXTENT)]),
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
                name="Taylor Geospatial Engine Labs",
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

    item = pystac.Item(
        id=qk,
        geometry=geom,
        bbox=bbox,
        datetime=TEMPORAL_EXTENT[0],
        start_datetime=TEMPORAL_EXTENT[0],
        end_datetime=TEMPORAL_EXTENT[1],
        properties={
            "tile:quadkey": qk,
            "tile:z": z,
            "tile:x": x,
            "tile:y": y,
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
        href=f"{SRC_COG_PREFIX}/{qk}.tif",
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


def build(tiles_parquet: Path, out_dir: Path, sample_json: int = 200) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"reading {tiles_parquet}")
    gdf = gpd.read_parquet(tiles_parquet)
    print(f"  {len(gdf):,} tiles")

    total_bbox = [float(v) for v in gdf.total_bounds]
    coll = _build_collection(total_bbox)
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
    rb = stac_geoparquet.arrow.parse_stac_items_to_arrow(item_dicts)
    stac_geoparquet.arrow.to_parquet(
        rb,
        items_pq,
        compression="zstd",
        compression_level=13,
    )
    print(f"done — {len(item_dicts):,} items, {written_json} sample JSONs in {sample_dir}/")
