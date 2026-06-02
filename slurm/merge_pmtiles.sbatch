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
T="${TMPDIR:-/tmp}"
echo "host=$(hostname) start=$(date -Is)"
echo "=== convert z0-8 overview tiles -> pmtiles (tmpfs) ==="
cp out/tiles_lo.mbtiles "$T/lo.mbtiles"
pmtiles convert "$T/lo.mbtiles" "$T/tiles_lo.pmtiles"
cp "$T/tiles_lo.pmtiles" out/tiles_lo.pmtiles
echo "=== merge 1000 z10-14 shards + z0-8 overview -> chm_height.pmtiles ==="
rm -f out/chm_height.pmtiles
pmtiles merge out/tiles_shards/shard_*.pmtiles out/tiles_lo.pmtiles out/chm_height.pmtiles
echo "=== result ==="; ls -la out/chm_height.pmtiles
pmtiles show out/chm_height.pmtiles 2>&1 | grep -iE "tile type|bounds|zoom|tile entries|addressed" | head
echo "exit=$? end=$(date -Is)"
