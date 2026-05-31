import shutil
import sys
import tempfile

import icechunk
import zarr
from zarr.codecs import BytesCodec
from zarr.codecs.numcodecs import Zlib

from chm_zarr import SRC_BUCKET, SRC_PREFIX

npx = int(sys.argv[1])  # array px per side (grid = npx/512)
n_refs = int(sys.argv[2])  # distinct refs to write
grid = npx // 512
url = f"s3://{SRC_BUCKET}/{SRC_PREFIX}/chm/0013113321.tif"
print(
    f"npx={npx} grid={grid}x{grid} ({grid * grid:,} chunks, 2x={2 * grid * grid:,}, 2^31={2**31:,}); writing {n_refs:,} distinct refs"
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
    g = root.create_group("L")
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
    # DISTINCT indices: row-major walk
    specs = [
        icechunk.VirtualChunkSpec(
            index=(i // grid, i % grid), location=url, offset=1024 + i, length=277
        )
        for i in range(n_refs)
    ]
    s.store.set_virtual_refs("/L/chm", specs, validate_containers=False)
    s.commit(f"{n_refs} refs")
    print(f"OK — grid {grid}x{grid}, {n_refs:,} distinct refs committed")
except BaseException as e:
    print(f"FAILED grid {grid}x{grid}, {n_refs:,} refs: {type(e).__name__}: {str(e)[:110]}")
finally:
    shutil.rmtree(d, ignore_errors=True)
