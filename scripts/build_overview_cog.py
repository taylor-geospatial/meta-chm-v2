"""Build a downsampled global canopy-height overview COG (EPSG:3857) for web display.

Each of the 213k source COGs is a z10 Web-Mercator quadkey tile. We read each one's
coarse overview decimated to TILE_PX x TILE_PX and place it into a single global mosaic of
1024*TILE_PX per side, then write a single-band uint8 COG with internal overviews. The
result is small (sparse, compressed) and CORS-hostable on source.coop, so a browser COG
reader (maplibre-cog-protocol) can stream + colorize it.

    GLOBAL_PX = 1024 * TILE_PX ; TILE_PX=64 -> 65536 px (~153 m/px at equator)
"""

import concurrent.futures as cf
import multiprocessing as mp
import sys
import time

import numpy as np
import pyarrow.parquet as pq
import rasterio
from rasterio.enums import Resampling
from tqdm import tqdm

from chm_zarr import SRC_BUCKET, SRC_PREFIX
from chm_zarr.quadkey import quadkey_to_tile

TILE_PX = 64
TILES_PER_SIDE = 1024  # z10
GLOBAL_PX = TILE_PX * TILES_PER_SIDE
R_MERC = 20037508.342789244
_ENV = {
    "AWS_NO_SIGN_REQUEST": "YES",
    "AWS_REGION": "us-east-1",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
}


def _read_tile(qk: str) -> tuple[int, int, np.ndarray] | None:
    """Read one COG decimated to TILE_PX^2; return (global_row0, global_col0, block)."""
    url = f"s3://{SRC_BUCKET}/{SRC_PREFIX}/chm/{qk}.tif"
    tx, ty, _ = quadkey_to_tile(qk)
    with rasterio.Env(**_ENV), rasterio.open(url) as ds:
        block = ds.read(1, out_shape=(TILE_PX, TILE_PX), resampling=Resampling.average)
    if not block.any():
        return None  # all-zero tile: leave as nodata
    return ty * TILE_PX, tx * TILE_PX, block


def build(tiles_parquet: str, out_path: str, workers: int = 64) -> None:
    quadkeys = pq.read_table(tiles_parquet, columns=["quadkey"])["quadkey"].to_pylist()
    print(f"{len(quadkeys):,} tiles -> global {GLOBAL_PX}x{GLOBAL_PX} overview ({TILE_PX}px/tile)")

    mosaic = np.zeros((GLOBAL_PX, GLOBAL_PX), dtype=np.uint8)
    t0 = time.time()
    ctx = mp.get_context("spawn")
    n_placed = 0
    with cf.ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
        for res in tqdm(
            ex.map(_read_tile, quadkeys, chunksize=64), total=len(quadkeys), desc="mosaic"
        ):
            if res is None:
                continue
            r0, c0, block = res
            mosaic[r0 : r0 + TILE_PX, c0 : c0 + TILE_PX] = block
            n_placed += 1
    print(f"read pass: {time.time() - t0:.1f}s, {n_placed:,} non-empty tiles, max={mosaic.max()}m")

    px = 2 * R_MERC / GLOBAL_PX
    transform = rasterio.transform.from_bounds(
        -R_MERC, -R_MERC, R_MERC, R_MERC, GLOBAL_PX, GLOBAL_PX
    )
    profile = {
        "driver": "COG",
        "dtype": "uint8",
        "count": 1,
        "width": GLOBAL_PX,
        "height": GLOBAL_PX,
        "crs": "EPSG:3857",
        "transform": transform,
        "nodata": 0,
        "compress": "deflate",
        "predictor": 2,
        "blocksize": 512,
        "overview_resampling": "average",
    }
    print(f"writing COG {out_path} (px≈{px:.1f} m)")
    with rasterio.Env(**_ENV), rasterio.open(out_path, "w", **profile) as dst:
        dst.write(mosaic, 1)
    print("done")


if __name__ == "__main__":
    build(sys.argv[1], sys.argv[2], workers=int(sys.argv[3]) if len(sys.argv) > 3 else 64)
