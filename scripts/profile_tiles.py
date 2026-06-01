"""Profile raster-tile generation for the z14 PMTiles build.

Each source COG is a slippy z10 tile, so web tiles z10..ZMAX partition per source tile.
For a random sample of source tiles we generate every web tile (read via rio-tiler ->
colorize -> WebP), skipping empty ones, and measure throughput + bytes to project the
full 178k-tile job across N cores.

    python scripts/profile_tiles.py --workers 112 --sample 1500 --zmax 14
"""

import argparse
import concurrent.futures as cf
import io
import multiprocessing as mp
import os
import time

import numpy as np
import pyarrow.parquet as pq
import rasterio
from PIL import Image
from rasterio.enums import Resampling

from chm_zarr import SRC_BUCKET, SRC_PREFIX

os.environ.update(
    AWS_NO_SIGN_REQUEST="YES",
    AWS_REGION="us-east-1",
    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    VSI_CACHE="TRUE",
    GDAL_HTTP_MULTIPLEX="YES",
)

Z_NATIVE = 10
N_POPULATED = 178_143  # non-empty source tiles (from the overview build)


def _build_lut() -> np.ndarray:
    """256x4 uint8 RGBA LUT. 0 -> transparent; 1..40 m -> YlGn ramp; >40 clamped."""
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
        ys = [s[1][ch] for s in stops]
        lut[: 40 + 1, ch] = np.interp(np.arange(41), xs, ys).astype(np.uint8)
    lut[41:] = lut[40]
    lut[0] = (0, 0, 0, 0)
    return lut


LUT = _build_lut()


def _tiles_for(qk: str, zmax: int) -> tuple[int, int, int]:
    """Generate all web tiles z10..zmax for one source COG. Returns (n_tiles, n_bytes, n_empty).

    A source z10 quadkey IS the slippy z10 tile, so web tiles at zoom z partition it into a
    (2^(z-10))^2 grid. Read each zoom's full grid ONCE from the matching COG overview, then
    slice 256x256 blocks in-memory — 5 reads/source-tile instead of 341 per-tile warps.
    """
    url = f"s3://{SRC_BUCKET}/{SRC_PREFIX}/chm/{qk}.tif"
    n, nbytes, empty = 0, 0, 0
    with rasterio.open(url) as ds:
        for z in range(Z_NATIVE, zmax + 1):
            s = 1 << (z - Z_NATIVE)
            px = 256 * s
            grid = ds.read(1, out_shape=(px, px), resampling=Resampling.average)
            for j in range(s):  # row = y (north->south), matches web tile y
                for i in range(s):  # col = x (west->east)
                    block = grid[j * 256 : (j + 1) * 256, i * 256 : (i + 1) * 256]
                    if block.max() == 0:
                        empty += 1
                        continue
                    rgba = LUT[block]  # (256,256,4)
                    buf = io.BytesIO()
                    Image.fromarray(rgba, "RGBA").save(buf, "WEBP", quality=80, method=4)
                    nbytes += buf.tell()
                    n += 1
    return n, nbytes, empty


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=112)
    ap.add_argument("--sample", type=int, default=1500)
    ap.add_argument("--zmax", type=int, default=14)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    qks = pq.read_table("out/tiles.parquet", columns=["quadkey"])["quadkey"].to_pylist()
    rng = np.random.default_rng(args.seed)
    sample = [qks[i] for i in rng.choice(len(qks), size=min(args.sample, len(qks)), replace=False)]
    print(f"profiling {len(sample)} source tiles, z10..z{args.zmax}, workers={args.workers}")

    ctx = mp.get_context("spawn")
    t0 = time.time()
    tot_tiles = tot_bytes = tot_empty = 0
    with cf.ProcessPoolExecutor(max_workers=args.workers, mp_context=ctx) as ex:
        for n, b, e in ex.map(_tiles_for, sample, [args.zmax] * len(sample), chunksize=4):
            tot_tiles += n
            tot_bytes += b
            tot_empty += e
    dt = time.time() - t0

    src_per_s = len(sample) / dt
    web_per_s = (tot_tiles + tot_empty) / dt
    avg_kb = tot_bytes / tot_tiles / 1024 if tot_tiles else 0
    print(f"\n--- measured ({args.workers} cores) ---")
    print(f"wall: {dt:.1f}s | source tiles/s: {src_per_s:.1f} | web tiles/s: {web_per_s:,.0f}")
    print(f"non-empty: {tot_tiles:,} | empty(skipped): {tot_empty:,} | avg {avg_kb:.1f} KB/tile")

    # project to all populated source tiles
    bytes_per_src = tot_bytes / len(sample)
    tiles_per_src = tot_tiles / len(sample)
    proj_size = bytes_per_src * N_POPULATED
    proj_tiles = tiles_per_src * N_POPULATED
    print(f"\n--- projected full z{args.zmax} build ({N_POPULATED:,} source tiles) ---")
    print(
        f"total non-empty web tiles: {proj_tiles / 1e6:.1f} M | PMTiles size: {proj_size / 1e9:.1f} GB"
    )
    per_core = src_per_s / args.workers  # scale measured throughput to a per-core rate
    for cores in (112, 224, 448, 896):
        secs = N_POPULATED / (per_core * cores)
        print(
            f"  {cores:>4} cores: {secs / 3600:.2f} h  (~{cores // 112} cpu_amd nodes, assumes linear scaling)"
        )


if __name__ == "__main__":
    main()
