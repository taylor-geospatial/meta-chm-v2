"""Resumable shard worker: extract per-tile acquisition dates from Meta's metadata GeoJSONs.

Each `metadata/{quadkey}.geojson` has an `acq_date` per source-image polygon (a tile is a
mosaic of acquisitions). We only need the dates, not the heavy polygon geometry, so we
fetch the raw bytes and regex out the `acq_date` strings — no JSON/geometry parse. Per tile
we record min/max/count. Output one parquet per shard (+ .done sentinel) so a failed array
task only re-does its slice.

    python scripts/acq_dates_shard.py --shard 7 --n-shards 1000 --out-dir out/acq_shards
"""

import argparse
import concurrent.futures as cf
import re
from pathlib import Path

import obstore
import pyarrow as pa
import pyarrow.parquet as pq

from chm_zarr import SRC_BUCKET, SRC_PREFIX

META_PREFIX = f"{SRC_PREFIX}/metadata"
DATE_RE = re.compile(rb'"acq_date"\s*:\s*"(\d{4}-\d{2}-\d{2})"')
_STORE = obstore.store.S3Store(bucket=SRC_BUCKET, region="us-east-1", skip_signature=True)


def _dates_for(qk: str) -> tuple[str, str | None, str | None, int]:
    """Return (quadkey, min_date, max_date, n_unique_dates); (qk, None, None, 0) if absent."""
    try:
        raw = bytes(obstore.get(_STORE, f"{META_PREFIX}/{qk}.geojson").bytes())
    except FileNotFoundError:
        return qk, None, None, 0
    dates = sorted({m.decode() for m in DATE_RE.findall(raw)})
    if not dates:
        return qk, None, None, 0
    return qk, dates[0], dates[-1], len(dates)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int, required=True)
    ap.add_argument("--n-shards", type=int, required=True)
    ap.add_argument("--tiles-parquet", default="out/tiles.parquet")
    ap.add_argument("--out-dir", default="out/acq_shards")
    ap.add_argument("--workers", type=int, default=32)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    done = out_dir / f"shard_{args.shard:05d}.done"
    out = out_dir / f"shard_{args.shard:05d}.parquet"
    if done.exists():
        print(f"shard {args.shard}: already done")
        return

    qks = pq.read_table(args.tiles_parquet, columns=["quadkey"])["quadkey"].to_pylist()
    qks.sort()
    mine = qks[args.shard :: args.n_shards]
    print(f"shard {args.shard}/{args.n_shards}: {len(mine)} tiles")

    rows = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        rows = list(ex.map(_dates_for, mine))

    tbl = pa.table(
        {
            "quadkey": pa.array([r[0] for r in rows], pa.string()),
            "acq_start": pa.array([r[1] for r in rows], pa.string()),
            "acq_end": pa.array([r[2] for r in rows], pa.string()),
            "acq_n": pa.array([r[3] for r in rows], pa.int32()),
        }
    )
    tmp = out.with_suffix(".parquet.tmp")
    pq.write_table(tbl, tmp, compression="zstd")
    tmp.replace(out)
    n_dated = sum(1 for r in rows if r[1])
    done.write_text(f"{len(mine)} tiles, {n_dated} with dates\n")
    print(f"shard {args.shard}: {n_dated}/{len(mine)} tiles have dates -> {out.name}")


if __name__ == "__main__":
    main()
