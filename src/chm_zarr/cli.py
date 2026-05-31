"""CLI entry points (one Click group per deliverable)."""

from pathlib import Path

import click


@click.command("build-tiles")
@click.option(
    "--tiles-geojson",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Local copy of source tiles.geojson",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path("out/tiles.parquet"),
)
@click.option("--head-concurrency", type=int, default=256)
@click.option("--limit", type=int, default=None, help="Process only first N tiles (smoke test)")
def build_tiles(
    tiles_geojson: Path, out_path: Path, head_concurrency: int, limit: int | None
) -> None:
    from .build_tiles import build

    build(tiles_geojson, out_path, head_concurrency=head_concurrency, limit=limit)


@click.command("build-stac")
@click.option(
    "--tiles-parquet",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--out-dir", type=click.Path(file_okay=False, path_type=Path), default=Path("out/stac")
)
def build_stac(tiles_parquet: Path, out_dir: Path) -> None:
    from .build_stac import build

    build(tiles_parquet, out_dir)


@click.command("build-virtual-zarr")
@click.option(
    "--tiles-parquet",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(path_type=Path),
    default=Path("out/chm.virtual.icechunk"),
)
@click.option("--workers", type=int, default=32)
@click.option("--limit", type=int, default=None)
@click.option("--min-level", type=int, default=1, help="Lowest pyramid level (0=native 1.19m)")
def build_virtual_zarr(
    tiles_parquet: Path, out_path: Path, workers: int, limit: int | None, min_level: int
) -> None:
    from .build_virtual_zarr import build

    build(tiles_parquet, out_path, workers=workers, limit=limit, min_level=min_level)
