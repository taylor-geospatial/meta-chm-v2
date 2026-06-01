#!/bin/bash
#SBATCH --job-name=chm_tiles
#SBATCH --partition=cpu_amd
#SBATCH --account=bgtj-tgirails
#SBATCH --array=0-999%1000
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=4g
#SBATCH --time=00:40:00
#SBATCH --output=logs/tiles_%A_%a.out
#SBATCH --error=logs/tiles_%A_%a.err
cd "$SLURM_SUBMIT_DIR"
uv run python scripts/tiles_shard.py \
  --shard "$SLURM_ARRAY_TASK_ID" --n-shards 1000 --zmax 14 \
  --tiles-parquet out/tiles.parquet --out-dir out/tiles_shards
