"""Expire old snapshots + garbage-collect a local Icechunk store (drops transaction logs
and overwritten manifests no longer reachable from the latest snapshot)."""

import sys
from datetime import UTC, datetime

import icechunk as ic

from chm_zarr import SRC_BUCKET

path = sys.argv[1]
prefix = f"s3://{SRC_BUCKET}/"
cfg = ic.RepositoryConfig.default()
cfg.set_virtual_chunk_container(
    ic.VirtualChunkContainer(prefix, ic.s3_store(region="us-east-1", anonymous=True))
)
repo = ic.Repository.open(
    ic.local_filesystem_storage(path),
    config=cfg,
    authorize_virtual_chunk_access=ic.containers_credentials(
        {prefix: ic.s3_anonymous_credentials()}
    ),
)
now = datetime.now(UTC)
expired = repo.expire_snapshots(older_than=now)
print(f"expired {len(expired)} snapshots")
summary = repo.garbage_collect(delete_object_older_than=now)
print("GC summary:", summary)
