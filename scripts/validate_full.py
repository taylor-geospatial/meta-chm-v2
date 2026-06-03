"""Validate the FULL virtual Zarr: level-6 cross-check + level-1 (split-manifest) read."""

import sys

import icechunk
import numpy as np
import pyarrow.parquet as pq
import rasterio
import xarray as xr

from chm_zarr import DST_BUCKET, DST_PREFIX
from chm_zarr.quadkey import quadkey_to_tile

store_path = sys.argv[1]
prefix = f"s3://{DST_BUCKET}/"
cfg = icechunk.RepositoryConfig.default()
cfg.set_virtual_chunk_container(
    icechunk.VirtualChunkContainer(prefix, icechunk.s3_store(region="us-west-2", anonymous=True))
)
repo = icechunk.Repository.open(
    icechunk.local_filesystem_storage(store_path),
    config=cfg,
    authorize_virtual_chunk_access=icechunk.containers_credentials(
        {prefix: icechunk.s3_anonymous_credentials()}
    ),
)
dt = xr.open_datatree(repo.readonly_session("main").store, engine="zarr", consolidated=False)
print(
    "groups:",
    sorted(dt.children),
    "| multiscales levels:",
    len(dt.attrs["multiscales"][0]["datasets"]),
)

rio_env = rasterio.Env(AWS_NO_SIGN_REQUEST="YES", AWS_REGION="us-west-2")
quadkeys = pq.read_table("out/tiles.parquet", columns=["quadkey"])["quadkey"].to_pylist()


def check(level, factor, qks, want=3):
    """Cross-check virtual-zarr level vs rasterio overview at `factor` for `want` nonzero tiles."""
    node = dt[str(level)]["chm"]
    px = 32768 // factor  # tile px at this level
    ok = 0
    for qk in qks:
        tx, ty, _ = quadkey_to_tile(qk)
        y0, x0 = ty * px, tx * px
        vz = node[y0 : y0 + px, x0 : x0 + px].values
        with (
            rio_env,
            rasterio.open(f"/vsicurl/https://data.source.coop/{DST_PREFIX}/chm/{qk}.tif") as ds,
        ):
            rio = ds.read(1, out_shape=(px, px))
        if not np.array_equal(vz, rio):
            print(f"  L{level} {qk}: MISMATCH ({int((vz != rio).sum())} px)")
            continue
        if vz.max() > 0:
            ok += 1
            print(f"  L{level} {qk}: MATCH ✓ (max={vz.max()} mean={vz.mean():.2f})")
        if ok >= want:
            break
    return ok


print("\n== level 6 (factor 64, 512px) ==")
n6 = check(6, 64, quadkeys[:50])
print("\n== level 1 (factor 2, 16384px — split-manifest level) ==")
n1 = check(1, 2, quadkeys[:50])
print(f"\nlevel6 matches={n6}, level1 matches={n1}")
sys.exit(0 if n6 and n1 else 2)
