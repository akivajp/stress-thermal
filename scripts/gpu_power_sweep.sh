#!/usr/bin/env bash
# GPU の電力上限を段階的に変えながら行列積の負荷 (scripts/gpu_load.py) をかけ、
# 性能 (TFLOPS)・電力・温度・クロックを記録する。終了時 (中断を含む) は元の電力上限に戻す。
# 電力上限の変更に sudo nvidia-smi -pl を使う (パスワードなしで実行できる設定が要る)。
set -euo pipefail

usage() {
  cat <<EOF
usage: $(basename "$0") [-d DEVICE] [-t SECONDS] [-o OUT_DIR] [WATTS...]

  -d DEVICE   対象の GPU 番号 (既定: 0)
  -t SECONDS  1段あたりの負荷時間 (既定: 120)
  -o OUT_DIR  出力先 (既定: runs/<日時>-gpu-power-sweep)
  WATTS       試す電力上限 (既定: 450 400 350 300 250 200)

出力: OUT_DIR/run.log (経過)、samples.csv (1秒ごとの nvidia-smi)、load-<W>.log、summary.md
EOF
}

case "${1:-}" in -h|--help) usage; exit 0 ;; esac
cd "$(dirname "${BASH_SOURCE[0]}")/.."
DEV=0; SECS=120; OUT=""
while getopts "d:t:o:h" opt; do
  case "$opt" in
    d) DEV="$OPTARG" ;; t) SECS="$OPTARG" ;; o) OUT="$OPTARG" ;;
    h) usage; exit 0 ;; *) usage; exit 2 ;;
  esac
done
shift $((OPTIND - 1))
WATTS=("$@"); [[ ${#WATTS[@]} -eq 0 ]] && WATTS=(450 400 350 300 250 200)
OUT="${OUT:-runs/$(date +%Y%m%d-%H%M%S)-gpu-power-sweep}"
mkdir -p "$OUT"
LOG="$OUT/run.log"
log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

# 元の電力上限を覚えておき、終了時に戻す
ORIG=$(nvidia-smi -i "$DEV" --query-gpu=power.limit --format=csv,noheader,nounits | awk '{printf "%d", $1}')
SAMPLER=""
cleanup() {
  [[ -n "$SAMPLER" ]] && kill "$SAMPLER" 2>/dev/null || true
  sudo -n nvidia-smi -i "$DEV" -pl "$ORIG" >/dev/null && log "電力上限を ${ORIG}W に戻しました"
}
trap cleanup EXIT

log "GPU$DEV: $(nvidia-smi -i "$DEV" --query-gpu=name,driver_version --format=csv,noheader) / 元の電力上限 ${ORIG}W / 各 ${SECS}秒 / ${WATTS[*]}W"

# 1秒ごとに nvidia-smi を記録する (列: 時刻, 電力上限, 電力, 温度, SM クロック, メモリクロック, ファン, 使用率)
nvidia-smi -i "$DEV" --query-gpu=timestamp,power.limit,power.draw,temperature.gpu,clocks.sm,clocks.mem,fan.speed,utilization.gpu \
  --format=csv,nounits -l 1 > "$OUT/samples.csv" &
SAMPLER=$!

echo "| 電力上限 | TFLOPS（後半の平均） | 実際の電力 | 温度（最高） | SM クロック | ファン |" > "$OUT/summary.md"
echo "|---:|---:|---:|---:|---:|---:|" >> "$OUT/summary.md"
for W in "${WATTS[@]}"; do
  sudo -n nvidia-smi -i "$DEV" -pl "$W" >/dev/null
  log "開始: ${W}W"
  t0=$(date +%s)
  uv run --extra gpu python scripts/gpu_load.py --device "$DEV" --duration "$SECS" > "$OUT/load-$W.log" 2>&1
  t1=$(date +%s)
  # 性能は gpu_load.py が30秒ごとに出す TFLOPS の後半 (最初の1回を除く) の平均
  tf=$(grep -oE '[0-9.]+ TFLOPS' "$OUT/load-$W.log" | awk 'NR>1{s+=$1;n++} END{if(n) printf "%.1f", s/n; else print "-"}')
  # 同じ時間帯の nvidia-smi の記録を集計 (負荷の後半)
  stats=$(awk -F', ' -v a="$(( (t0 + t1) / 2 ))" -v b="$t1" 'NR>1 {
      cmd="date -d \"" $1 "\" +%s"; cmd | getline ts; close(cmd);
      if (ts>=a && ts<=b) { p+=$3; c+=$5; f+=$7; n++; if ($4>tmax) tmax=$4 } }
    END { if (n) printf "%.0fW | %d℃ | %.0fMHz | %.0f%%", p/n, tmax, c/n, f/n; else print "- | - | - | -" }' "$OUT/samples.csv")
  log "終了: ${W}W → ${tf} TFLOPS | ${stats}"
  echo "| ${W}W | ${tf} | ${stats} |" >> "$OUT/summary.md"
done
log "集計: $OUT/summary.md"
cat "$OUT/summary.md"
