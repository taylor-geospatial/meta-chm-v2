#!/usr/bin/env bash
# Sharded verbatim mirror of Meta CHM v2 ml3 COGs -> source.coop (CORS on the true pixels).
# Array task i owns a disjoint 2-digit quadkey prefix (base-4: 00..33 = 16 shards), so nodes
# never copy the same object. --size-only makes it resumable (skips what an earlier run did).
# Reads Meta ANONYMOUSLY (signed reads denied) + writes source.coop SIGNED via two rclone
# remotes (~/.config/rclone/rclone.conf). Submit with: export AWS_* ; sbatch --export=ALL ...
#SBATCH --job-name=chm-mirror
#SBATCH --account=bgtj-tgirails
#SBATCH --partition=cpu_amd
#SBATCH --array=0-15%6
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=24
#SBATCH --mem=160G
#SBATCH --time=24:00:00
#SBATCH --output=/u/isaaccorley/github/chm-zarr/logs/mirror_%A_%a.out
set -uo pipefail

idx=${SLURM_ARRAY_TASK_ID}
PREFIX="$(( idx / 4 ))$(( idx % 4 ))"   # 00,01,...,33  (quadkey digits are 0-3)

RC="$HOME/.local/bin/rclone"
SRC="meta:dataforgood-fb-data/forests/v2/global/dinov3_global_chm_v2_ml3/chm"
DST="sourcecoop:us-west-2.opendata.source.coop/tge-labs/meta-chm-v2/chm"

echo "[mirror] task $idx prefix=$PREFIX start $(date -u) on $(hostname)"
aws sts get-caller-identity >/dev/null 2>&1 || { echo "[mirror] FATAL: creds invalid/expired"; exit 1; }

"$RC" copy "$SRC" "$DST" \
  --include "/${PREFIX}*" \
  --size-only \
  --transfers 64 --checkers 128 \
  --s3-no-check-bucket \
  --s3-upload-concurrency 4 --s3-chunk-size 32M \
  --multi-thread-streams 4 \
  --retries 3 --low-level-retries 10 \
  --stats 60s --stats-one-line -v
rc=$?
echo "[mirror] task $idx prefix=$PREFIX rclone exit=$rc done $(date -u)"
exit $rc
