#!/bin/bash
#SBATCH --job-name=chm_merge
#SBATCH --partition=cpu_amd
#SBATCH --account=bgtj-tgirails
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=96G
#SBATCH --time=06:00:00
#SBATCH --output=logs/%x_%j.out
#SBATCH --error=logs/%x_%j.err
set -euo pipefail
cd "$SLURM_SUBMIT_DIR"
export PATH="$HOME/.local/bin:$PATH"
echo "host=$(hostname) start=$(date -Is)"
echo "=== merge shards -> mbtiles ==="
uv run python scripts/merge_tiles.py --shards-dir out/tiles_shards --out out/chm_z14.mbtiles --zmax 14
echo "=== pmtiles convert ==="
pmtiles version
rm -f out/chm_z14.pmtiles
pmtiles convert out/chm_z14.mbtiles out/chm_z14.pmtiles
echo "=== result ==="
ls -la out/chm_z14.pmtiles
pmtiles show out/chm_z14.pmtiles 2>&1 | head -25
echo "exit=$? end=$(date -Is)"
