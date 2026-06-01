"""A torchgeo ``RasterDataset`` for Meta CHM v2 ml3 backed by the published GeoParquet.

The dataset index is built directly from ``tiles.parquet`` (each row already carries the
tile's EPSG:3857 bbox + COG URL), so instantiation opens **zero** COGs — it does not
download anything. Patches are streamed on demand: torchgeo opens each intersecting COG
with rasterio over GDAL ``/vsicurl`` (HTTPS range reads against Meta's public bucket) and
reads just the requested window.

Requires the optional ``torchgeo`` dependency group::

    uv run --group torchgeo python -c "from chm_zarr.torchgeo_dataset import MetaCHMv2"
"""

from collections.abc import Callable

import fsspec
import pandas as pd
import pyarrow.parquet as pq
from geopandas import GeoDataFrame
from rasterio.crs import CRS
from shapely.geometry import box
from torchgeo.datasets import RasterDataset

# Public, anonymous endpoints — no AWS credentials needed.
TILES_PARQUET = "https://data.source.coop/tge-labs/meta-chm-v2/tiles.parquet"
S3_PREFIX = "s3://dataforgood-fb-data/"
HTTPS_PREFIX = "https://dataforgood-fb-data.s3.amazonaws.com/"
NATIVE_RES = 1.194329  # metres per pixel at the equator (EPSG:3857)


class MetaCHMv2(RasterDataset):
    """Global canopy height (Meta CHM v2, ml3) streamed from cloud COGs via a GeoParquet index."""

    is_image = True
    all_bands = ("chm",)

    def __init__(
        self,
        tiles_parquet: str = TILES_PARQUET,
        transforms: Callable[[dict], dict] | None = None,
        cache: bool = True,
        res: float = NATIVE_RES,
    ) -> None:
        self.transforms = transforms
        self.cache = cache
        self.time_series = False
        self.paths = tiles_parquet
        self.bands = self.all_bands
        self.band_indexes = None
        self._crs = CRS.from_epsg(3857)
        self._res = (res, res)

        # Read only the columns we need (fsspec handles https + local); COG URLs become
        # https so GDAL /vsicurl streams windows. The index parquet is small (~6 MB).
        with fsspec.open(tiles_parquet, "rb") as f:
            tbl = pq.read_table(f, columns=["bbox_3857", "cog_url"])
        bboxes = tbl["bbox_3857"].to_pylist()
        filepaths = [u.replace(S3_PREFIX, HTTPS_PREFIX) for u in tbl["cog_url"].to_pylist()]
        geometries = [box(b["minx"], b["miny"], b["maxx"], b["maxy"]) for b in bboxes]

        # CHM v2 has no per-tile acquisition date here, so all tiles span the full interval.
        n = len(filepaths)
        interval = pd.IntervalIndex.from_arrays(
            [pd.Timestamp.min] * n, [pd.Timestamp.max] * n, closed="both"
        )
        self.index = GeoDataFrame(
            {"filepath": filepaths}, index=interval, geometry=geometries, crs="EPSG:3857"
        )
