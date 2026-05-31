import shutil
import sys
import tempfile

import icechunk
import zarr
from zarr.codecs import BytesCodec
from zarr.codecs.numcodecs import Zlib

from chm_zarr import SRC_BUCKET, SRC_PREFIX

batch_n = int(sys.argv[1])
url = f"s3://{SRC_BUCKET}/{SRC_PREFIX}/chm/0013113321.tif"
print(
    f"url len = {len(url)}, batch = {batch_n:,}, est bytes = {len(url) * batch_n / 1e9:.2f}e9 (2^31={2**31 / 1e9:.2f}e9)"
)

d = tempfile.mkdtemp()
try:
    prefix = f"s3://{SRC_BUCKET}/"
    cfg = icechunk.RepositoryConfig.default()
    cfg.set_virtual_chunk_container(
        icechunk.VirtualChunkContainer(
            prefix, icechunk.s3_store(region="us-east-1", anonymous=True)
        )
    )
    repo = icechunk.Repository.create(
        icechunk.local_filesystem_storage(d),
        config=cfg,
        authorize_virtual_chunk_access=icechunk.containers_credentials(
            {prefix: icechunk.s3_anonymous_credentials()}
        ),
    )
    s = repo.writable_session("main")
    root = zarr.group(store=s.store, overwrite=True)
    g = root.create_group("1")
    npx = 16777216
    g.create_array(
        "chm",
        shape=(npx, npx),
        chunks=(512, 512),
        dtype="uint8",
        fill_value=0,
        serializer=BytesCodec(),
        compressors=[Zlib()],
        dimension_names=("y", "x"),
    )
    s.commit("structure")
    s = repo.writable_session("main")
    # synthetic refs along the diagonal (valid distinct indices), realistic url+offset+length
    specs = [
        icechunk.VirtualChunkSpec(
            index=(i % 32768, (i * 7) % 32768), location=url, offset=1024 + i, length=277
        )
        for i in range(batch_n)
    ]
    print(f"built {len(specs):,} specs; calling set_virtual_refs...")
    s.store.set_virtual_refs("/1/chm", specs, validate_containers=False)
    s.commit(f"batch {batch_n}")
    print(f"OK — committed {batch_n:,} refs with no panic")
except BaseException as e:
    print(f"FAILED at batch {batch_n:,}: {type(e).__name__}: {str(e)[:120]}")
finally:
    shutil.rmtree(d, ignore_errors=True)
