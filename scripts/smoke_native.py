"""Smoke-test the native-inclusive (min_level=0) build + 1x..64x group naming.

Builds a slice with native, checks the group names, and reads a native (1x) chunk back through
Icechunk vs rasterio (no resampling) byte-exact. Run on a node:
    srun -p cpu_amd -A bgtj-tgirails -c 8 --mem 64G -t 20:00 uv run python scripts/smoke_native.py
"""

import os

os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")
os.environ.setdefault("AWS_REGION", "us-west-2")

import shutil
from pathlib import Path

import icechunk
import numpy as np
import rasterio
import zarr
from rasterio.windows import Window

from chm_zarr import DST_BUCKET, DST_PREFIX
from chm_zarr.build_virtual_zarr import build
from chm_zarr.quadkey import quadkey_to_tile

OUT = Path("out/smoke_native.icechunk")
EXPECTED = ["16x", "1x", "2x", "32x", "4x", "64x", "8x"]  # sorted() order


def main() -> int:
    build(Path("out/tiles.parquet"), OUT, workers=8, limit=300, min_level=0)  # include native
    prefix = f"s3://{DST_BUCKET}/"
    repo = icechunk.Repository.open(
        icechunk.local_filesystem_storage(str(OUT)),
        authorize_virtual_chunk_access=icechunk.containers_credentials(
            {prefix: icechunk.s3_anonymous_credentials()}
        ),
    )
    root = zarr.open_group(repo.readonly_session("main").store, mode="r")
    names = sorted(root.group_keys())
    print("group names:", names)

    qk = "0013131031"
    tx, ty, _ = quadkey_to_tile(qk)
    r = c = 32  # center block of the tile's 64x64 native block grid
    gy, gx = ty * 64 + r, tx * 64 + c
    ice = np.asarray(root["1x/chm"][gy * 512 : gy * 512 + 512, gx * 512 : gx * 512 + 512])
    with rasterio.open(f"/vsicurl/https://data.source.coop/{DST_PREFIX}/chm/{qk}.tif") as ds:
        ras = ds.read(1, window=Window(c * 512, r * 512, 512, 512))  # native, no resample

    match = np.array_equal(ice, ras)
    print(
        f"native(1x) block: ice nonzero={int((ice > 0).sum())} max={int(ice.max())} "
        f"ras max={int(ras.max())} match={match}"
    )
    shutil.rmtree(OUT, ignore_errors=True)
    return 0 if (match and ice.max() > 0 and names == EXPECTED) else 1


if __name__ == "__main__":
    raise SystemExit(main())
