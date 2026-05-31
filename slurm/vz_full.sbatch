#!/bin/bash
#SBATCH --job-name=vz_full
#SBATCH --partition=cpu
#SBATCH --account=bgtj-tgirails
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=64
#SBATCH --mem=460G
#SBATCH --time=08:00:00
#SBATCH --output=logs/%x_%j.out
#SBATCH --error=logs/%x_%j.err

cd "$SLURM_SUBMIT_DIR"
echo "host=$(hostname) cpus=$SLURM_CPUS_PER_TASK mem=$SLURM_MEM_PER_NODE start=$(date -Is)"
uv run chm-build-virtual-zarr \
  --tiles-parquet out/tiles.parquet \
  --out out/chm.zarr.icechunk \
  --workers 64 \
  --min-level 1
echo "exit=$? end=$(date -Is)"
