import shutil
import sys
import tempfile

import icechunk as ic
import zarr
from zarr.codecs import BytesCodec
from zarr.codecs.numcodecs import Zlib

from chm_zarr import SRC_BUCKET, SRC_PREFIX

npx, n_refs, split = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
grid = npx // 512
url = f"s3://{SRC_BUCKET}/{SRC_PREFIX}/chm/0013113321.tif"
print(f"grid={grid}x{grid}, refs={n_refs:,}, split={split} (max {split * split:,}/manifest)")
d = tempfile.mkdtemp()
try:
    prefix = f"s3://{SRC_BUCKET}/"
    cfg = ic.RepositoryConfig.default()
    cfg.set_virtual_chunk_container(
        ic.VirtualChunkContainer(prefix, ic.s3_store(region="us-east-1", anonymous=True))
    )
    splitting = ic.ManifestSplittingConfig.from_dict(
        {ic.ManifestSplitCondition.AnyArray(): {ic.ManifestSplitDimCondition.Any(): split}}
    )
    cfg.manifest = ic.ManifestConfig(splitting=splitting)
    repo = ic.Repository.create(
        ic.local_filesystem_storage(d),
        config=cfg,
        authorize_virtual_chunk_access=ic.containers_credentials(
            {prefix: ic.s3_anonymous_credentials()}
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
    specs = [
        ic.VirtualChunkSpec(index=(i // grid, i % grid), location=url, offset=1024 + i, length=277)
        for i in range(n_refs)
    ]
    s.store.set_virtual_refs("/L/chm", specs, validate_containers=False)
    s.commit(f"{n_refs} refs")
    print(f"OK — committed {n_refs:,} refs WITH splitting={split}")
    # read back one ref to confirm manifests are readable
    ro = repo.readonly_session("main")
    import xarray as xr

    dt = xr.open_datatree(ro.store, engine="zarr", consolidated=False)
    v = dt["L"]["chm"][0:512, 0:512].values
    print(f"read-back OK: block shape {v.shape}, max={v.max()}")
except BaseException as e:
    print(f"FAILED: {type(e).__name__}: {str(e)[:120]}")
finally:
    shutil.rmtree(d, ignore_errors=True)
