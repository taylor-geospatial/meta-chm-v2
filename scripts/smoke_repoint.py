"""Smoke-test the source.coop re-pointed virtual-zarr build.

Builds a handful of tiles (level 6 only), then reads one tile's chunk back THROUGH Icechunk
(resolving the virtual ref from source.coop) and compares byte-exact to rasterio reading the
same COG overview directly. Confirms the re-pointed refs resolve correctly. Run on a node:
    srun -p cpu_amd -A bgtj-tgirails -c 8 --mem 32G -t 20:00 uv run python scripts/smoke_repoint.py
"""

import os

os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")
os.environ.setdefault("AWS_REGION", "us-west-2")

from pathlib import Path

import icechunk
import numpy as np
import pyarrow.parquet as pq
import rasterio
import zarr

from chm_zarr import DST_BUCKET, DST_PREFIX
from chm_zarr.build_virtual_zarr import build
from chm_zarr.quadkey import quadkey_to_tile

OUT = Path("out/smoke_repoint.icechunk")
LEVEL = 6  # coarsest: 1 block (512px) per z10 tile


# MUST guard: build() uses a spawn ProcessPool which re-imports __main__ in each worker.
def main() -> int:
    import shutil

    qks = pq.read_table("out/tiles.parquet", columns=["quadkey"])["quadkey"].to_pylist()[:300]
    build(Path("out/tiles.parquet"), OUT, workers=8, limit=len(qks), min_level=LEVEL)

    url_prefix = f"s3://{DST_BUCKET}/"
    repo = icechunk.Repository.open(
        icechunk.local_filesystem_storage(str(OUT)),
        authorize_virtual_chunk_access=icechunk.containers_credentials(
            {url_prefix: icechunk.s3_anonymous_credentials()}
        ),
    )
    root = zarr.open_group(repo.readonly_session("main").store, mode="r")
    arr = root[f"{LEVEL}/chm"]

    # Find a tile with real canopy (max>0) so the check distinguishes a resolved ref from a
    # failed one (failed -> fill_value 0, which an all-zero ocean tile can't tell apart).
    for qk in qks:
        url = f"/vsicurl/https://data.source.coop/{DST_PREFIX}/chm/{qk}.tif"
        with rasterio.open(url) as ds:
            ras = ds.read(1, out_shape=(512, 512))
        if ras.max() > 0:
            tx, ty, _ = quadkey_to_tile(qk)
            ice = np.asarray(arr[ty * 512 : ty * 512 + 512, tx * 512 : tx * 512 + 512])
            match = np.array_equal(ice, ras)
            print(f"\nquadkey={qk} tile=({tx},{ty})  (first non-empty tile)")
            print(
                f"icechunk via source.coop ref: nonzero={int((ice > 0).sum())}/{ice.size}, max={ice.max()}"
            )
            print(
                f"rasterio direct overview:      nonzero={int((ras > 0).sum())}/{ras.size}, max={ras.max()}"
            )
            print(f"BYTE-EXACT MATCH: {match}")
            shutil.rmtree(OUT, ignore_errors=True)
            return 0 if (match and ice.max() > 0) else 1
    shutil.rmtree(OUT, ignore_errors=True)
    print("no non-empty tile found in sample — inconclusive")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
