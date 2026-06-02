#!/bin/bash
#SBATCH --job-name=acq_dates
#SBATCH --partition=cpu_amd
#SBATCH --account=bgtj-tgirails
#SBATCH --array=0-999%1000
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=16g
#SBATCH --time=00:30:00
#SBATCH --output=logs/acq_%A_%a.out
#SBATCH --error=logs/acq_%A_%a.err
cd "$SLURM_SUBMIT_DIR"
uv run python scripts/acq_dates_shard.py --shard "$SLURM_ARRAY_TASK_ID" --n-shards 1000 \
  --tiles-parquet out/tiles.parquet --out-dir out/acq_shards --workers 16
