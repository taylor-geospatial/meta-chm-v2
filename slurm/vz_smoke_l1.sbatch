#!/bin/bash
#SBATCH --job-name=vz_smoke_l1
#SBATCH --partition=cpu_amd
#SBATCH --account=bgtj-tgirails
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=120G
#SBATCH --time=00:15:00
#SBATCH --output=logs/%x_%j.out
#SBATCH --error=logs/%x_%j.err
cd "$SLURM_SUBMIT_DIR"
echo "host=$(hostname)"
uv run chm-build-virtual-zarr --tiles-parquet out/tiles.parquet --out out/chm.smoke_l1.icechunk --workers 16 --limit 30 --min-level 1
echo "exit=$?"
