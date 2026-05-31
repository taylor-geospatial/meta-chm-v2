#!/bin/bash
#SBATCH --job-name=vz_smoke
#SBATCH --partition=cpu_amd
#SBATCH --account=bgtj-tgirails
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH --time=00:20:00
#SBATCH --output=logs/%x_%j.out
#SBATCH --error=logs/%x_%j.err

cd "$SLURM_SUBMIT_DIR"
echo "host=$(hostname) cpus=$SLURM_CPUS_PER_TASK"
uv run chm-build-virtual-zarr \
  --tiles-parquet out/tiles.parquet \
  --out out/chm.virtual.icechunk \
  --workers 16 \
  --limit 50
echo "exit=$?"
