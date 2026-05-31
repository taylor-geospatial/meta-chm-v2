"""Build tiles.parquet (GeoParquet 1.1) from source tiles.geojson + S3 HEAD on each COG.

Output schema (one row per quadkey):
    quadkey: str (10-char)
    z, x, y: uint16
    tile_size_bytes: int64 (S3 ContentLength of chm/{qk}.tif)
    cog_url: str (source S3 URL while we host nothing yet)
    bbox_3857: struct(minx,miny,maxx,maxy: float64)
    geometry: Polygon in EPSG:4326 (for GeoParquet spatial filtering)
"""

import asyncio
import json
import time
from pathlib import Path

import geopandas as gpd
import obstore
import pyarrow as pa
from shapely.geometry import box
from tqdm.asyncio import tqdm_asyncio

from . import SRC_BUCKET, SRC_PREFIX
from .quadkey import quadkey_to_tile, tile_to_lonlat_bbox, tile_to_mercator_bbox


async def _head_size(store: obstore.store.S3Store, key: str) -> int | None:
    try:
        meta = await store.head_async(key)
        return int(meta["size"])
    except Exception as e:
        print(f"HEAD failed for {key}: {type(e).__name__}: {e}")
        return None


async def _gather_sizes(quadkeys: list[str], concurrency: int = 256) -> list[int | None]:
    store = obstore.store.S3Store(
        bucket=SRC_BUCKET,
        region="us-east-1",
        skip_signature=True,
    )
    sem = asyncio.Semaphore(concurrency)

    async def one(qk: str) -> int | None:
        async with sem:
            return await _head_size(store, f"{SRC_PREFIX}/chm/{qk}.tif")

    return await tqdm_asyncio.gather(*(one(qk) for qk in quadkeys), desc="HEAD COGs")


def build(
    tiles_geojson_path: Path,
    out_path: Path,
    head_concurrency: int = 256,
    limit: int | None = None,
) -> None:
    t0 = time.time()
    print(f"reading {tiles_geojson_path}")
    with tiles_geojson_path.open() as f:
        gj = json.load(f)
    feats = gj["features"]
    if limit:
        feats = feats[:limit]
    quadkeys = [f["properties"]["tile"] for f in feats]
    print(f"  {len(quadkeys):,} tiles")

    print("computing tile xyz + bboxes locally")
    zs, xs, ys = [], [], []
    bboxes_4326, bboxes_3857 = [], []
    for qk in quadkeys:
        x, y, z = quadkey_to_tile(qk)
        zs.append(z)
        xs.append(x)
        ys.append(y)
        bboxes_4326.append(tile_to_lonlat_bbox(x, y, z))
        bboxes_3857.append(tile_to_mercator_bbox(x, y, z))

    print(f"HEAD-ing {len(quadkeys):,} COGs (concurrency={head_concurrency})")
    sizes = asyncio.run(_gather_sizes(quadkeys, head_concurrency))

    geoms = [box(*b) for b in bboxes_4326]
    cog_urls = [f"s3://{SRC_BUCKET}/{SRC_PREFIX}/chm/{qk}.tif" for qk in quadkeys]

    bbox_3857_struct = pa.StructArray.from_arrays(
        [pa.array([b[i] for b in bboxes_3857], pa.float64()) for i in range(4)],
        names=["minx", "miny", "maxx", "maxy"],
    )

    gdf = gpd.GeoDataFrame(
        {
            "quadkey": pa.array(quadkeys, pa.string()).to_pylist(),
            "z": pa.array(zs, pa.uint16()).to_pylist(),
            "x": pa.array(xs, pa.uint32()).to_pylist(),
            "y": pa.array(ys, pa.uint32()).to_pylist(),
            "tile_size_bytes": sizes,
            "cog_url": cog_urls,
            "bbox_3857": bbox_3857_struct.to_pylist(),
        },
        geometry=geoms,
        crs="EPSG:4326",
    )
    gdf = gdf.sort_values("quadkey").reset_index(drop=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"writing {out_path}")
    gdf.to_parquet(
        out_path,
        compression="zstd",
        compression_level=13,
        write_covering_bbox=True,
        geometry_encoding="WKB",
        schema_version="1.1.0",
        row_group_size=10_000,
    )
    dt = time.time() - t0
    n_missing = sum(1 for s in sizes if s is None)
    total_bytes = sum(s for s in sizes if s is not None)
    print(
        f"done in {dt:.1f}s — {len(quadkeys):,} rows, "
        f"{n_missing} missing HEADs, {total_bytes / 1e12:.2f} TB total tile bytes"
    )
