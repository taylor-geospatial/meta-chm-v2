"""Resumable shard worker for the z14 raster-PMTiles build.

The 178k populated source COGs are split into N contiguous shards. Each SLURM array task
builds ONE shard into its own MBTiles file and writes a ``.done`` sentinel on success.
Re-running the array skips shards whose ``.done`` exists, so a failure only re-does the
incomplete shards (each ~minutes), never the whole pipeline. Shards are disjoint at z10+
(each source tile owns distinct web tiles), so the final merge is a plain concatenation.

    python scripts/tiles_shard.py --shard 7 --n-shards 1024 --zmax 14 --out-dir out/tiles_shards
"""

import argparse
import io
import os
import sqlite3
import time
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import rasterio
from PIL import Image
from rasterio.enums import Resampling

from chm_zarr import SRC_BUCKET, SRC_PREFIX
from chm_zarr.quadkey import quadkey_to_tile

os.environ.update(
    AWS_NO_SIGN_REQUEST="YES",
    AWS_REGION="us-east-1",
    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    VSI_CACHE="TRUE",
    GDAL_HTTP_MULTIPLEX="YES",
    GDAL_HTTP_MAX_RETRY="5",  # ride out transient S3 read hiccups
    GDAL_HTTP_RETRY_DELAY="2",
)
Z_NATIVE = 10


def _build_lut() -> np.ndarray:
    stops = [
        (0, (0, 0, 0, 0)),
        (1, (255, 255, 229, 255)),
        (5, (217, 240, 163, 255)),
        (10, (120, 198, 121, 255)),
        (20, (35, 132, 67, 255)),
        (30, (0, 104, 55, 255)),
        (40, (0, 69, 41, 255)),
    ]
    lut = np.zeros((256, 4), dtype=np.uint8)
    xs = [s[0] for s in stops]
    for ch in range(4):
        lut[:41, ch] = np.interp(np.arange(41), xs, [s[1][ch] for s in stops]).astype(np.uint8)
    lut[41:] = lut[40]
    lut[0] = (0, 0, 0, 0)
    return lut


LUT = _build_lut()


def _source_tiles(zmax: int, qk: str):
    """Yield (z, x_xyz, y_xyz, webp_bytes) web tiles z10..zmax for one source COG."""
    url = f"s3://{SRC_BUCKET}/{SRC_PREFIX}/chm/{qk}.tif"
    x10, y10, _ = quadkey_to_tile(qk)
    with rasterio.open(url) as ds:
        for z in range(Z_NATIVE, zmax + 1):
            s = 1 << (z - Z_NATIVE)
            grid = ds.read(1, out_shape=(256 * s, 256 * s), resampling=Resampling.average)
            for j in range(s):
                for i in range(s):
                    block = grid[j * 256 : (j + 1) * 256, i * 256 : (i + 1) * 256]
                    if block.max() == 0:
                        continue
                    buf = io.BytesIO()
                    Image.fromarray(LUT[block], "RGBA").save(buf, "WEBP", quality=80, method=4)
                    yield z, x10 * s + i, y10 * s + j, buf.getvalue()


def _init_mbtiles(con: sqlite3.Connection, zmax: int) -> None:
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("CREATE TABLE metadata (name text, value text)")
    con.execute(
        "CREATE TABLE tiles (zoom_level integer, tile_column integer, tile_row integer, tile_data blob)"
    )
    con.executemany(
        "INSERT INTO metadata VALUES (?,?)",
        [
            ("name", "meta-chm-v2"),
            ("format", "webp"),
            ("minzoom", str(Z_NATIVE)),
            ("maxzoom", str(zmax)),
            ("type", "overlay"),
        ],
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int, required=True)
    ap.add_argument("--n-shards", type=int, required=True)
    ap.add_argument("--zmax", type=int, default=14)
    ap.add_argument("--tiles-parquet", default="out/tiles.parquet")
    ap.add_argument("--out-dir", default="out/tiles_shards")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    done = out_dir / f"shard_{args.shard:05d}.done"
    mbt = out_dir / f"shard_{args.shard:05d}.mbtiles"
    tmp = out_dir / f"shard_{args.shard:05d}.mbtiles.tmp"

    if done.exists():
        print(f"shard {args.shard}: already done, skipping")
        return

    qks = pq.read_table(args.tiles_parquet, columns=["quadkey"])["quadkey"].to_pylist()
    qks.sort()
    mine = qks[args.shard :: args.n_shards]  # strided => each shard spread across the globe
    print(f"shard {args.shard}/{args.n_shards}: {len(mine)} source tiles, z10..z{args.zmax}")

    tmp.unlink(missing_ok=True)  # discard any partial from a previous killed run
    con = sqlite3.connect(tmp)
    _init_mbtiles(con, args.zmax)
    t0 = time.time()
    n = 0
    for k, qk in enumerate(mine):
        rows = [
            (z, x, (1 << z) - 1 - y, sqlite3.Binary(data))  # MBTiles uses TMS (flipped) row
            for z, x, y, data in _source_tiles(args.zmax, qk)
        ]
        con.executemany("INSERT INTO tiles VALUES (?,?,?,?)", rows)
        n += len(rows)
        if (k + 1) % 50 == 0:
            con.commit()
    con.commit()
    con.execute("CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)")
    con.commit()
    con.close()

    tmp.replace(mbt)  # atomic: only a complete shard appears as shard_*.mbtiles
    done.write_text(f"{len(mine)} tiles_src, {n} web_tiles, {time.time() - t0:.0f}s\n")
    print(f"shard {args.shard}: {n:,} web tiles in {time.time() - t0:.0f}s -> {mbt.name}")


if __name__ == "__main__":
    main()
