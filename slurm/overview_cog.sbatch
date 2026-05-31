#!/bin/bash
#SBATCH --job-name=overview_cog
#SBATCH --partition=cpu_amd
#SBATCH --account=bgtj-tgirails
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=64
#SBATCH --mem=128G
#SBATCH --time=01:00:00
#SBATCH --output=logs/%x_%j.out
#SBATCH --error=logs/%x_%j.err
cd "$SLURM_SUBMIT_DIR"
echo "host=$(hostname) start=$(date -Is)"
uv run python scripts/build_overview_cog.py out/tiles.parquet out/chm_overview_z8.tif 64
echo "exit=$? end=$(date -Is)"
