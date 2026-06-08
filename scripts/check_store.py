"""Validate a built store: group names + byte-exact reads at native (1x, windowed) and 64x.
srun ... uv run python scripts/check_store.py out/chm.zarr.icechunk
"""

import os

os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")
os.environ.setdefault("AWS_REGION", "us-west-2")

import sys

import icechunk
import numpy as np
import rasterio
import zarr
from rasterio.windows import Window

from chm_zarr import DST_BUCKET, DST_PREFIX
from chm_zarr.quadkey import quadkey_to_tile

path = sys.argv[1]
prefix = f"s3://{DST_BUCKET}/"
cfg = icechunk.RepositoryConfig.default()
cfg.set_virtual_chunk_container(
    icechunk.VirtualChunkContainer(prefix, icechunk.s3_store(region="us-west-2", anonymous=True))
)
repo = icechunk.Repository.open(
    icechunk.local_filesystem_storage(path),
    config=cfg,
    authorize_virtual_chunk_access=icechunk.containers_credentials(
        {prefix: icechunk.s3_anonymous_credentials()}
    ),
)
root = zarr.open_group(repo.readonly_session("main").store, mode="r")
print("group names:", sorted(root.group_keys()))
ms = dict(root.attrs)["multiscales"][0]
print("multiscales paths:", [d["path"] for d in ms["datasets"]], "| keys:", list(ms.keys()))

qk = "0013131031"
tx, ty, _ = quadkey_to_tile(qk)
url = f"/vsicurl/https://data.source.coop/{DST_PREFIX}/chm/{qk}.tif"
ok = True

# native 1x: center 512 block, no resample
r = c = 32
gy, gx = ty * 64 + r, tx * 64 + c
ice = np.asarray(root["1x/chm"][gy * 512 : gy * 512 + 512, gx * 512 : gx * 512 + 512])
with rasterio.open(url) as ds:
    ras = ds.read(1, window=Window(c * 512, r * 512, 512, 512))
m = np.array_equal(ice, ras) and ice.max() > 0
ok &= m
print(f"1x (native) center block: max={int(ice.max())} match={np.array_equal(ice, ras)}")

# 64x: whole tile (512 px), downsampled overview
ice = np.asarray(root["64x/chm"][ty * 512 : ty * 512 + 512, tx * 512 : tx * 512 + 512])
with rasterio.open(url) as ds:
    ras = ds.read(1, out_shape=(512, 512))
m = np.array_equal(ice, ras) and ice.max() > 0
ok &= m
print(f"64x block: max={int(ice.max())} match={np.array_equal(ice, ras)}")

print("VALID" if ok else "FAILED")
sys.exit(0 if ok else 1)
