"""Cloud-native rebuild of Meta CHM v2 ml3."""

SRC_BUCKET = "dataforgood-fb-data"
SRC_PREFIX = "forests/v2/global/dinov3_global_chm_v2_ml3"
SRC_TILES_GEOJSON = f"s3://{SRC_BUCKET}/{SRC_PREFIX}/tiles.geojson"
SRC_COG_PREFIX = f"s3://{SRC_BUCKET}/{SRC_PREFIX}/chm"
SRC_META_PREFIX = f"s3://{SRC_BUCKET}/{SRC_PREFIX}/metadata"

DST_BUCKET = "us-west-2.opendata.source.coop"
DST_PREFIX = "tge-labs/meta-chm-v2"
DST_BASE = f"s3://{DST_BUCKET}/{DST_PREFIX}"
