#!/bin/bash
# Resume an explicitly configured LoRA run after the known recoverable Metal
# command-buffer failure. This script is inert unless the new owner opts in.
set -u

if [ "${BORG_TRAINING_OPT_IN:-0}" != "1" ]; then
  echo "training disabled: set BORG_TRAINING_OPT_IN=1 with explicit private paths" >&2
  exit 2
fi

: "${BORG_TRAINING_ROOT:?set an absolute private BORG_TRAINING_ROOT}"
: "${BORG_BASE_MODEL:?set BORG_BASE_MODEL explicitly}"
: "${BORG_RESUME_ADAPTER:?set BORG_RESUME_ADAPTER relative to BORG_TRAINING_ROOT}"
: "${BORG_OUTPUT_DIR:?set BORG_OUTPUT_DIR relative to BORG_TRAINING_ROOT}"

case "$BORG_TRAINING_ROOT" in
  /*) ;;
  *) echo "BORG_TRAINING_ROOT must be absolute" >&2; exit 2 ;;
esac

cd "$BORG_TRAINING_ROOT" || exit 1
PYTHON_BIN="${BORG_TRAINING_PYTHON:-$BORG_TRAINING_ROOT/venv/bin/python}"
RESUME="$BORG_RESUME_ADAPTER"
OUT="$BORG_OUTPUT_DIR"
LOG="${BORG_TRAINING_LOG:-$OUT.log}"

for attempt in 1 2 3 4; do
  [ -f "$OUT/adapters.safetensors" ] && RESUME="$OUT/adapters.safetensors"
  "$PYTHON_BIN" -m mlx_lm lora \
    --model "$BORG_BASE_MODEL" \
    --train --data "${BORG_DATASET_DIR:-data}" \
    --resume-adapter-file "$RESUME" \
    --iters "${BORG_TRAINING_ITERS:-2400}" --batch-size 4 --max-seq-length 4096 --num-layers 16 \
    --learning-rate 8e-6 --steps-per-report 20 --steps-per-eval 300 \
    --val-batches 25 --save-every 300 \
    --adapter-path "$OUT" \
    --grad-checkpoint >> "$LOG" 2>&1
  code=$?
  if [ "$code" -eq 0 ]; then
    echo "training finished clean on attempt $attempt" >> "$LOG"
    break
  fi
  if ! grep -q "kIOGPUCommandBufferCallbackErrorInnocentVictim\|Command buffer execution failed" "$LOG"; then
    echo "training exited $code with a non-recoverable error; not restarting" >> "$LOG"
    break
  fi
  echo "recoverable GPU failure on attempt $attempt; resuming latest checkpoint" >> "$LOG"
  latest=$(find "$OUT" -maxdepth 1 -type f -name '*_adapters.safetensors' -print 2>/dev/null | sort | tail -1)
  [ -n "$latest" ] && cp "$latest" "$OUT/adapters.safetensors"
  sleep 30
done
