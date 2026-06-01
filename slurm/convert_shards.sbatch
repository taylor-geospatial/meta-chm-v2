#!/bin/bash
#SBATCH --job-name=chm_conv
#SBATCH --partition=cpu_amd
#SBATCH --account=bgtj-tgirails
#SBATCH --array=0-999%288
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=8g
#SBATCH --time=00:20:00
#SBATCH --output=logs/conv_%A_%a.out
#SBATCH --error=logs/conv_%A_%a.err
# --mem=8g caps ~32 tasks/node on 256GB nodes, keeping per-node /tmp (64G tmpfs) usage safe.
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd "$SLURM_SUBMIT_DIR"
s=$(printf "shard_%05d" "$SLURM_ARRAY_TASK_ID")
out_mb="out/tiles_shards/$s.mbtiles"; out_pm="out/tiles_shards/$s.pmtiles"
[ -f "$out_pm" ] && { echo "skip $s"; exit 0; }
[ -f "$out_mb" ] || { echo "MISSING $out_mb"; exit 1; }
tmp="${TMPDIR:-/tmp}/${s}.$$"
cp "$out_mb" "$tmp.mbtiles"                      # /u -> RAM (sequential)
pmtiles convert "$tmp.mbtiles" "$tmp.pmtiles"    # random reads hit RAM, not network
mv "$tmp.pmtiles" "$out_pm.tmp" && mv "$out_pm.tmp" "$out_pm"   # RAM -> /u (sequential)
rm -f "$tmp.mbtiles" "$tmp.pmtiles"
echo "done $s"
