#!/bin/bash
# Explicit, owner-configured evaluation, dataset, and LoRA chain. Nothing is
# read or written unless the new owner opts in and supplies every data source.
set -u

if [ "${BORG_TRAINING_OPT_IN:-0}" != "1" ]; then
  echo "training disabled: set BORG_TRAINING_OPT_IN=1 with explicit private paths" >&2
  exit 2
fi

: "${BORG_TRAINING_ROOT:?set an absolute private BORG_TRAINING_ROOT}"
: "${BORG_BASE_MODEL:?set BORG_BASE_MODEL explicitly}"
: "${BORG_CAPTURE_ADAPTER:?set BORG_CAPTURE_ADAPTER explicitly}"
: "${BORG_EXAM_DATA:?set BORG_EXAM_DATA explicitly}"
: "${BORG_PAIR_SOURCE:?set BORG_PAIR_SOURCE explicitly}"
: "${BORG_BASE_CORPUS:?set BORG_BASE_CORPUS explicitly}"

case "$BORG_TRAINING_ROOT" in
  /*) ;;
  *) echo "BORG_TRAINING_ROOT must be absolute" >&2; exit 2 ;;
esac

cd "$BORG_TRAINING_ROOT" || exit 1
PYTHON_BIN="${BORG_TRAINING_PYTHON:-$BORG_TRAINING_ROOT/venv/bin/python}"
LOG="${BORG_CHAIN_LOG:-runs/v3-chain.log}"
SHIM_DATA="${BORG_SHIM_DATASET_DIR:-data-v3-shim}"
MIXED_DATA="${BORG_MIXED_DATASET_DIR:-data-v3}"
OUT="${BORG_OUTPUT_DIR:-runs/graph-lora-v3}"
TLOG="${BORG_TRAINING_LOG:-$OUT.log}"

mkdir -p "$(dirname "$LOG")" "$MIXED_DATA" "$OUT"
echo "STAGE exam-start $(date '+%F %T')" >> "$LOG"

"$PYTHON_BIN" eval/capture_exam.py \
  --model "$BORG_BASE_MODEL" \
  --adapter "$BORG_CAPTURE_ADAPTER" \
  --data "$BORG_EXAM_DATA" \
  --limit "${BORG_EXAM_LIMIT:-300}" --max-tokens "${BORG_EXAM_MAX_TOKENS:-1400}" \
  --out "${BORG_EXAM_REPORT:-eval/exam-capture.json}" \
  --save-raw "${BORG_EXAM_RAW_DIR:-eval/raw-capture}" \
  >> "${BORG_EXAM_LOG:-runs/exam-capture.log}" 2>&1
echo "STAGE exam-done rc=$? $(date '+%F %T')" >> "$LOG"

echo "STAGE v3-build-start $(date '+%F %T')" >> "$LOG"
"$PYTHON_BIN" build_v3_dataset.py \
  --pairs "$BORG_PAIR_SOURCE" \
  --out "$SHIM_DATA" >> "$LOG" 2>&1

BORG_SHIM_DATA="$SHIM_DATA" BORG_MIXED_DATA="$MIXED_DATA" \
BORG_BASE_CORPUS="$BORG_BASE_CORPUS" "$PYTHON_BIN" - >> "$LOG" 2>&1 <<'PY'
import json
import os
from pathlib import Path
import random
import shutil

shim_root = Path(os.environ["BORG_SHIM_DATA"])
mixed_root = Path(os.environ["BORG_MIXED_DATA"])
base_source = Path(os.environ["BORG_BASE_CORPUS"])
shim = [json.loads(line) for line in (shim_root / "train.jsonl").open() if line.strip()]
dedupe = [row for row in shim if "resolve_edge" in json.dumps(row)[:400]
          or "duplicate_facts" in row["messages"][1]["content"][:200]]
base = [json.loads(line) for line in base_source.open() if line.strip()]
mix = base + shim + dedupe
random.Random(13).shuffle(mix)
with (mixed_root / "train.jsonl").open("w") as handle:
    for row in mix:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
shim_valid = [json.loads(line) for line in (shim_root / "valid.jsonl").open() if line.strip()]
base_valid_source = base_source.with_name("valid.jsonl")
base_valid = [json.loads(line) for line in base_valid_source.open() if line.strip()]
with (mixed_root / "valid.jsonl").open("w") as handle:
    for row in shim_valid + base_valid:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
shutil.copy(shim_root / "test.jsonl", mixed_root / "test.jsonl")
print(f"v3 mix: {len(mix)} train, {len(shim_valid) + len(base_valid)} valid")
PY
echo "STAGE v3-build-done rc=$? $(date '+%F %T')" >> "$LOG"

echo "STAGE v3-train-start $(date '+%F %T')" >> "$LOG"
RESUME=""
for attempt in 1 2 3 4 5 6; do
  latest=$(find "$OUT" -maxdepth 1 -type f -name '*_adapters.safetensors' -print 2>/dev/null | sort | tail -1)
  if [ -n "$latest" ]; then
    cp "$latest" "$OUT/adapters.safetensors"
    RESUME="$OUT/adapters.safetensors"
  fi
  resume_args=()
  [ -n "$RESUME" ] && resume_args=(--resume-adapter-file "$RESUME")
  "$PYTHON_BIN" -m mlx_lm lora \
    --model "$BORG_BASE_MODEL" \
    --train --data "$MIXED_DATA" "${resume_args[@]}" \
    --iters "${BORG_TRAINING_ITERS:-5000}" --batch-size 4 --max-seq-length 4096 --num-layers 16 \
    --learning-rate 1e-5 --steps-per-report 20 --steps-per-eval 300 \
    --val-batches 25 --save-every 200 \
    --adapter-path "$OUT" \
    --grad-checkpoint >> "$TLOG" 2>&1
  code=$?
  if [ "$code" -eq 0 ]; then
    echo "training finished clean on attempt $attempt" >> "$TLOG"
    break
  fi
  if ! tail -30 "$TLOG" | grep -q "InnocentVictim\|Command buffer execution failed"; then
    echo "training exited $code with a non-recoverable error; not restarting" >> "$TLOG"
    break
  fi
  echo "recoverable GPU failure on attempt $attempt; resuming" >> "$TLOG"
  sleep 45
done
echo "STAGE v3-train-done $(date '+%F %T')" >> "$LOG"
