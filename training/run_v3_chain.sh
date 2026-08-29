#!/bin/bash
# Morning chain 2026-08-29, launchd-owned end to end:
#   1) capture student exam (300 held-out pairs)
#   2) v3 dataset: shim shape-pairs + original graffiti corpus, dedupe-edge x2
#   3) v3 LoRA training with the GPU-crash auto-resume supervisor
# Each stage logs a STAGE line; watchers key on those.
set -u
cd $HOME/Library/Memory/graphiti/training || exit 1
LOG=runs/v3-chain.log
echo "STAGE exam-start $(date '+%F %T')" >> "$LOG"

./venv/bin/python eval/capture_exam.py \
  --model mlx-community/Qwen3-4B-Instruct-2507-4bit \
  --adapter runs/capture-4b-lora-v1 \
  --data $HOME/Library/Memory/mem0/data/capture-training/test.jsonl \
  --limit 300 --max-tokens 1400 \
  --out eval/exam-capture-v1.json --save-raw eval/raw-capture-v1 \
  >> runs/exam-capture-v1.log 2>&1
echo "STAGE exam-done rc=$? $(date '+%F %T')" >> "$LOG"

echo "STAGE v3-build-start $(date '+%F %T')" >> "$LOG"
python3 build_v3_dataset.py --out data-v3-shim >> "$LOG" 2>&1
./venv/bin/python - >> "$LOG" 2>&1 <<'EOF'
import json, random
out = {}
shim = [json.loads(l) for l in open('data-v3-shim/train.jsonl') if l.strip()]
dedupe = [r for r in shim if 'resolve_edge' in json.dumps(r)[:400] or 'duplicate_facts' in r['messages'][1]['content'][:200]]
base = [json.loads(l) for l in open('data/train.jsonl') if l.strip()]
mix = base + shim + dedupe  # dedupe-edge weighted x2 via re-append
random.Random(13).shuffle(mix)
with open('data-v3/train.jsonl', 'w') as f:
    for r in mix: f.write(json.dumps(r, ensure_ascii=False) + '\n')
sv = [json.loads(l) for l in open('data-v3-shim/valid.jsonl') if l.strip()]
bv = [json.loads(l) for l in open('data/valid.jsonl') if l.strip()]
with open('data-v3/valid.jsonl', 'w') as f:
    for r in sv + bv: f.write(json.dumps(r, ensure_ascii=False) + '\n')
import shutil; shutil.copy('data-v3-shim/test.jsonl', 'data-v3/test.jsonl')
print(f"v3 mix: {len(mix)} train ({len(base)} base + {len(shim)} shim + {len(dedupe)} dedupe-x2), {len(sv)+len(bv)} valid")
EOF
echo "STAGE v3-build-done rc=$? $(date '+%F %T')" >> "$LOG"

echo "STAGE v3-train-start $(date '+%F %T')" >> "$LOG"
OUT=runs/graphiti-4b-lora-v3
TLOG=runs/graphiti-4b-lora-v3.log
mkdir -p "$OUT"
RESUME=""
for attempt in 1 2 3 4 5 6; do
  latest=$(ls -t "$OUT"/*_adapters.safetensors 2>/dev/null | head -1)
  if [ -n "$latest" ]; then
    cp "$latest" "$OUT/adapters.safetensors"
    RESUME="--resume-adapter-file $OUT/adapters.safetensors"
  fi
  ./venv/bin/python -m mlx_lm lora \
    --model mlx-community/Qwen3-4B-Instruct-2507-4bit \
    --train --data data-v3 $RESUME \
    --iters 5000 --batch-size 4 --max-seq-length 4096 --num-layers 16 \
    --learning-rate 1e-5 --steps-per-report 20 --steps-per-eval 300 \
    --val-batches 25 --save-every 200 \
    --adapter-path "$OUT" \
    --grad-checkpoint >> "$TLOG" 2>&1
  code=$?
  if [ $code -eq 0 ]; then echo "v3 finished clean on attempt $attempt" >> "$TLOG"; break; fi
  if ! grep -q "InnocentVictim\|Command buffer execution failed" <(tail -30 "$TLOG"); then
    echo "v3 exited $code with a non-GPU-recovery error; not restarting" >> "$TLOG"; break
  fi
  echo "v3 GPU-recovery crash on attempt $attempt; resuming" >> "$TLOG"; sleep 45
done
echo "STAGE v3-train-done $(date '+%F %T')" >> "$LOG"
