#!/usr/bin/env bash
# Verbatim mirror of Meta CHM v2 ml3 COGs -> source.coop (adds CORS + range on the true
# pixels). Reads Meta ANONYMOUSLY (signed reads are denied) and writes source.coop SIGNED,
# so it uses two rclone remotes (see ~/.config/rclone/rclone.conf): meta (env_auth=false ->
# anonymous) + sourcecoop (env_auth=true -> AWS_* from the job env via `sbatch --export`).
# Resumable: --size-only skips objects already present with a matching size, so re-running
# with fresh creds continues where it left off.
#SBATCH --job-name=chm-mirror
#SBATCH --account=bgtj-tgirails
#SBATCH --partition=cpu_amd
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=24
#SBATCH --mem=128G
#SBATCH --time=24:00:00
#SBATCH --output=/u/isaaccorley/github/chm-zarr/logs/mirror_%j.out
set -uo pipefail

RC="$HOME/.local/bin/rclone"
SRC="meta:dataforgood-fb-data/forests/v2/global/dinov3_global_chm_v2_ml3/chm"
DST="sourcecoop:us-west-2.opendata.source.coop/tge-labs/meta-chm-v2/chm"

echo "[mirror] start $(date -u) on $(hostname)"
aws sts get-caller-identity >/dev/null 2>&1 || { echo "[mirror] FATAL: creds invalid/expired"; exit 1; }

"$RC" copy "$SRC" "$DST" \
  --size-only \
  --transfers 96 --checkers 256 \
  --s3-no-check-bucket \
  --s3-upload-concurrency 4 --s3-chunk-size 32M \
  --multi-thread-streams 4 \
  --retries 3 --low-level-retries 10 \
  --stats 60s --stats-one-line -v
rc=$?
echo "[mirror] rclone exit=$rc done $(date -u)"
exit $rc
