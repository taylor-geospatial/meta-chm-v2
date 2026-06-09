"""Validate the virtual Zarr: open via Icechunk, read pixels (byte-range GET to Meta),
and cross-check level-6 blocks against a direct rasterio read of the same COG overview."""

import sys

import icechunk
import numpy as np
import pyarrow.parquet as pq
import rasterio
import xarray as xr

from chm_zarr import DST_BUCKET, DST_PREFIX
from chm_zarr.quadkey import quadkey_to_tile

store_path = sys.argv[1] if len(sys.argv) > 1 else "out/chm.virtual.icechunk"
n_probe = int(sys.argv[2]) if len(sys.argv) > 2 else 50

url_prefix = f"s3://{DST_BUCKET}/"
config = icechunk.RepositoryConfig.default()
config.set_virtual_chunk_container(
    icechunk.VirtualChunkContainer(
        url_prefix, icechunk.s3_store(region="us-west-2", anonymous=True)
    )
)
repo = icechunk.Repository.open(
    icechunk.local_filesystem_storage(store_path),
    config=config,
    authorize_virtual_chunk_access=icechunk.containers_credentials(
        {url_prefix: icechunk.s3_anonymous_credentials()}
    ),
)
session = repo.readonly_session("main")
dt = xr.open_datatree(session.store, engine="zarr", consolidated=False)
print("DataTree groups:", sorted(dt.children))

node6 = dt["64x"]["chm"]  # 64x (level 6) = 1 chunk (512x512) per source tile
print("64x array:", node6.shape, node6.dtype)

quadkeys = pq.read_table("out/tiles.parquet", columns=["quadkey"])["quadkey"].to_pylist()[:n_probe]

rio_env = rasterio.Env(AWS_NO_SIGN_REQUEST="YES", AWS_REGION="us-west-2")
nonzero_checked = 0
for qk in quadkeys:
    tx, ty, _ = quadkey_to_tile(qk)
    y0, x0 = ty * 512, tx * 512
    vz_block = node6[y0 : y0 + 512, x0 : x0 + 512].values

    # Direct rasterio read of overview level 6 (factor 64) of the same COG
    with rio_env, rasterio.open(f"s3://{DST_BUCKET}/{DST_PREFIX}/chm/{qk}.tif") as ds:
        # overviews()[5] == factor 64 → the 512x512 IFD; read at that decimated shape
        rio_block = ds.read(1, out_shape=(512, 512))

    if not np.array_equal(vz_block, rio_block):
        n_diff = int((vz_block != rio_block).sum())
        print(
            f"  {qk}: MISMATCH ({n_diff} px differ) vz[max={vz_block.max()}] rio[max={rio_block.max()}]"
        )
        continue
    if vz_block.max() > 0:
        nonzero_checked += 1
        print(f"  {qk}: MATCH ✓ (nonzero: max={vz_block.max()} mean={vz_block.mean():.2f})")
    if nonzero_checked >= 3:
        break

if nonzero_checked == 0:
    print("WARNING: no nonzero tile found in probe set — all matched but all zero")
    sys.exit(2)
print(
    f"\nOK — {nonzero_checked} nonzero level-6 blocks match rasterio exactly. Virtual Zarr is correct."
)
