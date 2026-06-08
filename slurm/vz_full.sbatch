#!/bin/bash
#SBATCH --job-name=vz_full
#SBATCH --partition=cpu_amd
#SBATCH --account=bgtj-tgirails
#SBATCH --exclude=rails08
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=48
#SBATCH --mem=200G
#SBATCH --time=08:00:00
#SBATCH --output=logs/%x_%j.out
#SBATCH --error=logs/%x_%j.err

# min-level 0 = include native (1.19 m) -> groups 1x..64x. Compact-array accumulation keeps
# RAM ~50-80 GB (all levels incl. native), so this fits a 256 GB cpu_amd node.
cd "$SLURM_SUBMIT_DIR"
echo "host=$(hostname) cpus=$SLURM_CPUS_PER_TASK mem=$SLURM_MEM_PER_NODE start=$(date -Is)"
uv run chm-build-virtual-zarr \
  --tiles-parquet out/tiles.parquet \
  --out out/chm.zarr.icechunk \
  --workers 48 \
  --min-level 0
echo "exit=$? end=$(date -Is)"
