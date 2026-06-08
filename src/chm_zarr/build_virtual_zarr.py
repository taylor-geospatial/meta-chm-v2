"""Build a multiscales VirtualiZarr GeoZarr store from the source.coop COGs (zero-copy).

Refs point at the public source.coop mirror (`s3://us-west-2.opendata.source.coop/...`), so
the store is self-contained — it does not depend on Meta's bucket for chunk bytes.

Per-tile step: fetch ONLY the COG header in a single bulk range GET, then run
`kerchunk.tiff.tiff_to_zarr` on those bytes locally. kerchunk emits a 7-level Zarr-v2
group with chunk references for all 7 TIFF IFDs (native + 6 overviews); we keep its
proven page-selection logic (the COGs have 14 raw tifffile pages — two pyramids — so
hand-indexing would corrupt the mosaic). We re-key tile-local `(level, r, c)` into the
global EPSG:3857 zoom-10 mosaic and combine into one Icechunk store with `multiscales`.

The single-GET-per-COG matters: kerchunk's own S3 reader does dozens of tiny range
reads per file to walk the TIFF tag chain, which at cross-region latency caps throughput
at ~7 tiles/s. One bulk GET + local parse is faster, but `tifffile` parsing holds the
GIL — so we use PROCESSES (not threads): each worker GETs its header, parses, and
re-keys its chunks to global indices in parallel, returning a compact flat list. The
main process only merges. Spawn context so obstore's Rust client inits cleanly per proc.

Global per-side sizes:
    level 0: 33,554,432 px (= 1024 tiles x 32768)   chunks: 65,536 x 65,536
    level 1: 16,777,216                              32,768 x 32,768  ...  level 6: 524,288
"""

import concurrent.futures as cf
import multiprocessing as mp
import time
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import zarr

import fsspec
import kerchunk.tiff
import numpy as np
import obstore
import pyarrow.parquet as pq
from tqdm import tqdm

from . import DST_BUCKET, DST_PREFIX
from .quadkey import quadkey_to_tile

TILE_PX = 32768
BLOCK_PX = 512
N_LEVELS = 7
Z_NATIVE = 10
GLOBAL_TILES_PER_SIDE = 1 << Z_NATIVE  # 1024
R_MERC = 20037508.342789244
# COG header (all IFDs + tile offset/bytecount tables) lives in the first bytes of the
# file. 256KB suffices on these tiles; 1MB is a safe margin and still < the ~1.9MB min
# COG size, so the range is always valid. Verified: truncated parse == full-file parse.
HEADER_BYTES = 1 << 20

# Per-level: shape per tile in pixels, in blocks-per-tile-side, in global blocks-per-side
LEVEL_TILE_PX = [TILE_PX >> L for L in range(N_LEVELS)]  # 32768, 16384, ..., 512
LEVEL_BLOCKS_PER_TILE = [max(1, p // BLOCK_PX) for p in LEVEL_TILE_PX]  # 64,32,16,8,4,2,1
LEVEL_GLOBAL_BLOCKS = [b * GLOBAL_TILES_PER_SIDE for b in LEVEL_BLOCKS_PER_TILE]
LEVEL_GLOBAL_PX = [TILE_PX * GLOBAL_TILES_PER_SIDE >> L for L in range(N_LEVELS)]

# Module-level per-process singletons. Under spawn the module is re-imported in each
# worker, so each process gets its own obstore client + memory fs (no fork inheritance).
_STORE = obstore.store.S3Store(bucket=DST_BUCKET, region="us-west-2", skip_signature=True)
_MEMFS = fsspec.filesystem("memory")


def _kerchunk_reindex(qk: str, min_level: int) -> "tuple[str, np.ndarray]":
    """Worker: GET header → kerchunk → re-key chunks to global indices.

    Returns (qk, int64 array of shape (N, 5): columns [level, gy, gx, offset, length]) for
    levels >= min_level. Runs in a worker process so the GIL-bound tifffile parse parallelizes;
    the compact array keeps the pickled result — and the parent's accumulated RAM — small.
    """
    key = f"{DST_PREFIX}/chm/{qk}.tif"
    hdr = bytes(obstore.get(_STORE, key, options={"range": (0, HEADER_BYTES)}).bytes())
    mempath = f"/{qk}.tif"
    _MEMFS.pipe(mempath, hdr)
    try:
        refs = kerchunk.tiff.tiff_to_zarr(f"memory://{mempath}")
    finally:
        _MEMFS.rm(mempath)

    tx, ty, _ = quadkey_to_tile(qk)
    rows: list[tuple[int, int, int, int, int]] = []
    for level in range(min_level, N_LEVELS):
        b = LEVEL_BLOCKS_PER_TILE[level]
        gy0, gx0 = ty * b, tx * b
        for r in range(b):
            for c in range(b):
                ref = refs.get(f"{level}/{r}.{c}")
                if isinstance(ref, list) and len(ref) == 3:
                    rows.append((level, gy0 + r, gx0 + c, int(ref[1]), int(ref[2])))
    return qk, np.array(rows, dtype=np.int64).reshape(-1, 5)


def level_name(level: int) -> str:
    """Multiscale group name = downscale factor from native: level 0 -> '1x' (native 1.19 m),
    level 1 -> '2x', ... level 6 -> '64x'. (GeoZarr only requires multiscales to point at the
    path; this naming states each level's resolution directly.)"""
    return f"{1 << level}x"


def _geotransform(n_px: int) -> str:
    """GDAL GeoTransform for a global EPSG:3857 grid `n_px` wide: origin top-left, square pixels."""
    pix = 2 * R_MERC / n_px
    return f"{-R_MERC} {pix} 0.0 {R_MERC} 0.0 {-pix}"


def apply_geozarr_metadata(root: "zarr.Group", levels: list[int]) -> None:
    """Write the three GeoZarr conventions SEPARATELY (not conflated):
      - multiscales: a root attr listing overview group paths, coarsest->finest (paths only).
      - CRS (proj/CF): per-level `spatial_ref` grid_mapping aux variable (crs_wkt + proj:code).
      - spatial transform: a GDAL `GeoTransform` string on that same aux variable.
    Each `chm` array references its `spatial_ref` via the CF `grid_mapping` attribute.
    Group `L` holds the 2**L-times-downscaled overview of the native 1.19 m grid.
    """
    from pyproj import CRS

    wkt = CRS.from_epsg(3857).to_wkt()
    root.attrs["Conventions"] = "CF-1.10"
    root.attrs["title"] = "Meta CHM v2 ml3 — canopy height (multiscale GeoZarr)"
    # multiscales convention only — paths to the overview groups, coarsest first.
    root.attrs["multiscales"] = [
        {
            "name": "chm",
            "datasets": [
                {"path": level_name(L), "downscale_factor": 1 << L}
                for L in sorted(levels, reverse=True)
            ],
            "type": "average",
        }
    ]
    for L in levels:
        # zarr's Group.__getitem__ returns an untyped union; cast so the checker sees Group/Array.
        g = cast("zarr.Group", root[level_name(L)])
        chm = cast("zarr.Array", g["chm"])
        chm.attrs["_ARRAY_DIMENSIONS"] = ["y", "x"]
        chm.attrs["standard_name"] = "canopy_height"
        chm.attrs["long_name"] = "tree canopy height"
        chm.attrs["units"] = "m"
        chm.attrs["grid_mapping"] = "spatial_ref"  # CF: point at the CRS aux variable
        if "spatial_ref" in g:
            sr = cast("zarr.Array", g["spatial_ref"])
        else:
            sr = g.create_array("spatial_ref", shape=(), dtype="int32", fill_value=0)
        sr[...] = 0
        sr.attrs["_ARRAY_DIMENSIONS"] = []
        sr.attrs["grid_mapping_name"] = "mercator"
        sr.attrs["crs_wkt"] = wkt
        sr.attrs["spatial_ref"] = wkt  # GDAL/rioxarray compatibility
        sr.attrs["proj:code"] = "EPSG:3857"
        sr.attrs["proj:epsg"] = 3857
        sr.attrs["GeoTransform"] = _geotransform(LEVEL_GLOBAL_PX[L])


def build(
    tiles_parquet: Path,
    out_path: Path,
    workers: int = 16,
    limit: int | None = None,
    min_level: int = 1,
) -> None:
    quadkeys = pq.read_table(tiles_parquet, columns=["quadkey"])["quadkey"].to_pylist()
    if limit:
        quadkeys = quadkeys[:limit]
    print(f"{len(quadkeys):,} COGs to kerchunk → multiscales virtual Zarr")

    # min_level=0 includes native (1.19 m, ~872M refs); min_level=1 starts at the 2x overview.
    levels = list(range(min_level, N_LEVELS))
    print(f"building levels {levels} -> groups {[level_name(L) for L in levels]}")

    # Accumulate ONE compact int64 array per tile (cols: level, gy, gx, off, len). Far smaller
    # than dicts of Python tuples — ~50 GB for all levels incl. native — so it fits a normal
    # 256 GB node instead of needing a 512 GB/2 TB box.
    tiles: list[tuple[str, np.ndarray]] = []
    t0 = time.time()
    ctx = mp.get_context("spawn")
    worker = partial(_kerchunk_reindex, min_level=min_level)
    with cf.ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
        for qk, arr in tqdm(
            ex.map(worker, quadkeys, chunksize=32), total=len(quadkeys), desc="kerchunk"
        ):
            if len(arr):
                tiles.append((qk, arr))

    n_chunks = sum(len(a) for _, a in tiles)
    print(
        f"kerchunk pass: {time.time() - t0:.1f}s, {n_chunks:,} chunks across {len(tiles):,} tiles"
    )
    if not tiles:
        raise RuntimeError("no chunks parsed — nothing to write")

    import icechunk
    import zarr
    from zarr.codecs import BytesCodec
    from zarr.codecs.numcodecs import Zlib

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        import shutil

        shutil.rmtree(out_path)
    storage = icechunk.local_filesystem_storage(str(out_path))
    # Virtual chunks live in the public source.coop bucket (us-west-2); register a container
    # so Icechunk resolves `s3://us-west-2.opendata.source.coop/...` byte ranges anonymously.
    # (Self-contained: refs no longer depend on Meta's bucket.)
    url_prefix = f"s3://{DST_BUCKET}/"
    config = icechunk.RepositoryConfig.default()
    config.set_virtual_chunk_container(
        icechunk.VirtualChunkContainer(
            url_prefix, icechunk.s3_store(region="us-west-2", anonymous=True)
        )
    )
    # Icechunk serializes each manifest as a FlatBuffer (capped at 2^31 bytes ≈ ~40M refs).
    # Level 1 alone has 218M refs, so split manifests into bounded shards (2048x2048 chunks
    # -> <=4.2M refs each). Without this the array's single manifest overflows on read/commit.
    config.manifest = icechunk.ManifestConfig(
        splitting=icechunk.ManifestSplittingConfig.from_dict(
            {
                icechunk.ManifestSplitCondition.AnyArray(): {
                    icechunk.ManifestSplitDimCondition.Any(): 2048
                }
            }
        )
    )
    repo = icechunk.Repository.create(
        storage,
        config=config,
        authorize_virtual_chunk_access=icechunk.containers_credentials(
            {url_prefix: icechunk.s3_anonymous_credentials()}
        ),
    )

    # --- 1) Create the GeoZarr structure (group + per-level arrays) + GeoZarr metadata ---
    # Source COG tiles are DEFLATE/zlib compressed (no predictor/filters, per kerchunk);
    # declare the matching codec so reads decompress. Each level is its own group node
    # (different y/x sizes); GeoZarr metadata (multiscales / CRS / GeoTransform) is applied
    # by apply_geozarr_metadata as three separate conventions.
    session = repo.writable_session("main")
    root = zarr.group(store=session.store, overwrite=True)
    for L in levels:
        n_px = LEVEL_GLOBAL_PX[L]
        g = root.create_group(level_name(L))
        g.create_array(
            "chm",
            shape=(n_px, n_px),
            chunks=(BLOCK_PX, BLOCK_PX),
            dtype="uint8",
            fill_value=0,
            serializer=BytesCodec(),
            compressors=[Zlib()],
            dimension_names=("y", "x"),
        )
    apply_geozarr_metadata(root, levels)
    structure_snap = session.commit("geozarr structure (groups + arrays + GeoZarr metadata)")
    print(f"structure committed: {structure_snap}")

    # --- 2) Stream virtual chunk refs per level in <50M-ref batches, committing each ---
    # Icechunk caps a single commit near 50M chunk modifications, and a single manifest
    # FlatBuffer at ~40M refs; level 1 alone is ~218M. Manifest splitting (above) bounds
    # stored manifests; we also batch commits at 20M (verified safe) to bound the change-set.
    BATCH = 20_000_000
    for L in levels:
        arr_path = f"/{level_name(L)}/chm"
        written = 0
        batch: list[icechunk.VirtualChunkSpec] = []
        session = repo.writable_session("main")
        for qk, arr in tiles:
            sel = arr[arr[:, 0] == L]
            if not len(sel):
                continue
            loc = f"s3://{DST_BUCKET}/{DST_PREFIX}/chm/{qk}.tif"
            for gy, gx, off, ln in sel[:, 1:].tolist():
                batch.append(
                    icechunk.VirtualChunkSpec(index=[gy, gx], location=loc, offset=off, length=ln)
                )
                if len(batch) >= BATCH:
                    session.store.set_virtual_refs(arr_path, batch, validate_containers=False)
                    written += len(batch)
                    session.commit(f"{level_name(L)} virtual refs {written:,}")
                    session = repo.writable_session("main")
                    batch = []
        if batch:
            session.store.set_virtual_refs(arr_path, batch, validate_containers=False)
            written += len(batch)
            session.commit(f"{level_name(L)} virtual refs {written:,}")
        print(
            f"level {level_name(L)}: wrote {written:,} virtual refs (shape {LEVEL_GLOBAL_PX[L]} sq)"
        )

    print(f"done → {out_path}")
