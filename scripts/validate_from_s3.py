"""Open the published GeoZarr from source.coop ANONYMOUSLY and cross-check vs rasterio."""

import sys

import icechunk
import numpy as np
import rasterio
import xarray as xr

from chm_zarr.quadkey import quadkey_to_tile

DST_BUCKET = "us-west-2.opendata.source.coop"
ZARR_PREFIX = "tge-labs/meta-chm-v2/zarr/chm.zarr.icechunk"
COG_PREFIX = "tge-labs/meta-chm-v2/chm"

prefix = f"s3://{DST_BUCKET}/"
cfg = icechunk.RepositoryConfig.default()
cfg.set_virtual_chunk_container(
    icechunk.VirtualChunkContainer(prefix, icechunk.s3_store(region="us-west-2", anonymous=True))
)
repo = icechunk.Repository.open(
    icechunk.s3_storage(bucket=DST_BUCKET, prefix=ZARR_PREFIX, region="us-west-2", anonymous=True),
    config=cfg,
    authorize_virtual_chunk_access=icechunk.containers_credentials(
        {prefix: icechunk.s3_anonymous_credentials()}
    ),
)
dt = xr.open_datatree(repo.readonly_session("main").store, engine="zarr", consolidated=False)
print(
    "opened from source.coop | groups:",
    sorted(dt.children),
    "| multiscales levels:",
    len(dt.attrs["multiscales"][0]["datasets"]),
)

rio_env = rasterio.Env(AWS_NO_SIGN_REQUEST="YES", AWS_REGION="us-west-2")
# Probe a few quadkeys known to have data
qks = ["0013113332", "0013113333", "0013131013", "0013131031", "0013131033"]


def check(level, factor):
    node = dt[str(level)]["chm"]
    px = 32768 // factor
    ok = 0
    for qk in qks:
        tx, ty, _ = quadkey_to_tile(qk)
        y0, x0 = ty * px, tx * px
        vz = node[y0 : y0 + px, x0 : x0 + px].values
        with (
            rio_env,
            rasterio.open(f"/vsicurl/https://data.source.coop/{COG_PREFIX}/{qk}.tif") as ds,
        ):
            rio = ds.read(1, out_shape=(px, px))
        match = np.array_equal(vz, rio)
        if vz.max() > 0:
            print(f"  L{level} {qk}: {'MATCH ✓' if match else 'MISMATCH ✗'} (max={vz.max()})")
            ok += match
    return ok


print("== level 6 ==")
n6 = check(6, 64)
print("== level 1 ==")
n1 = check(1, 2)
print(
    f"\n{'PUBLIC READ OK' if n6 and n1 else 'FAILED'} — anonymous source.coop read matches rasterio (L6={n6}, L1={n1})"
)
sys.exit(0 if n6 and n1 else 2)
