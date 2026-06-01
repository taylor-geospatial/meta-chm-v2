#!/bin/bash
#SBATCH --job-name=chm_pmerge
#SBATCH --partition=cpu_amd
#SBATCH --account=bgtj-tgirails
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH --time=03:00:00
#SBATCH --output=logs/%x_%j.out
#SBATCH --error=logs/%x_%j.err
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd "$SLURM_SUBMIT_DIR"
echo "host=$(hostname) start=$(date -Is) inputs=$(ls out/tiles_shards/shard_*.pmtiles | wc -l)"
rm -f out/chm_z14.pmtiles
pmtiles merge out/tiles_shards/shard_*.pmtiles out/chm_z14.pmtiles
echo "=== result ==="; ls -la out/chm_z14.pmtiles
pmtiles show out/chm_z14.pmtiles 2>&1 | head -20
echo "exit=$? end=$(date -Is)"
