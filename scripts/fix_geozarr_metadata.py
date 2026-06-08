"""Patch an existing Icechunk store's GeoZarr metadata in place — separate the multiscales,
CRS (proj/CF), and spatial-transform (GDAL GeoTransform) conventions instead of conflating
them. Only attrs + tiny per-level `spatial_ref` aux variables change; the 290M virtual chunk
refs are untouched, so the commit is a small metadata delta.

    uv run python scripts/fix_geozarr_metadata.py out/chm.zarr.icechunk
"""

import sys

import icechunk
import zarr

from chm_zarr import DST_BUCKET
from chm_zarr.build_virtual_zarr import apply_geozarr_metadata

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
session = repo.writable_session("main")
root = zarr.open_group(session.store, mode="r+")
for stale in ("geozarr_spec_version",):  # drop the old conflated attrs
    if stale in root.attrs:
        del root.attrs[stale]

levels = sorted(int(k) for k in root.group_keys())
print("levels:", levels)
apply_geozarr_metadata(root, levels)
snap = session.commit("fix GeoZarr metadata: separate multiscales / CRS / GeoTransform conventions")
print("committed:", snap)
print("multiscales:", dict(root.attrs)["multiscales"])
