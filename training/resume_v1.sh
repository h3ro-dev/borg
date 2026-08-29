#!/bin/bash
# Resume v1 (Qwen3-1.7B LoRA) from the step-900 checkpoint on studio0 and
# push it to 3,300 total iters to cement JSON format adherence (preview exam
# at step 900: 92% valid vs the 99% gate; quality already above bar at 0.58
# Jaccard). This GPU shares with the graphiti backfill and crashed once as an
# innocent victim of a Metal recovery event, so restart on that crash class,
# at most 4 times. Checkpoints every 300 keep any loss under 5 minutes.
cd $HOME/Library/Memory/graphiti/training || exit 1
RESUME=runs/qwen3-1p7b-lora-v1/adapters.safetensors
for attempt in 1 2 3 4; do
  [ -f runs/qwen3-1p7b-lora-v1b/adapters.safetensors ] && RESUME=runs/qwen3-1p7b-lora-v1b/adapters.safetensors
  ./venv/bin/python -m mlx_lm lora \
    --model mlx-community/Qwen3-1.7B-4bit \
    --train --data data \
    --resume-adapter-file "$RESUME" \
    --iters 2400 --batch-size 4 --max-seq-length 4096 --num-layers 16 \
    --learning-rate 8e-6 --steps-per-report 20 --steps-per-eval 300 \
    --val-batches 25 --save-every 300 \
    --adapter-path runs/qwen3-1p7b-lora-v1b \
    --grad-checkpoint >> runs/qwen3-1p7b-lora-v1b.log 2>&1
  code=$?
  if [ $code -eq 0 ]; then
    echo "v1b finished clean on attempt $attempt" >> runs/qwen3-1p7b-lora-v1b.log
    break
  fi
  if ! grep -q "kIOGPUCommandBufferCallbackErrorInnocentVictim\|Command buffer execution failed" runs/qwen3-1p7b-lora-v1b.log; then
    echo "v1b exited $code with a non-GPU-recovery error; not restarting" >> runs/qwen3-1p7b-lora-v1b.log
    break
  fi
  echo "v1b GPU-recovery crash on attempt $attempt; resuming from latest checkpoint" >> runs/qwen3-1p7b-lora-v1b.log
  latest=$(ls -t runs/qwen3-1p7b-lora-v1b/*_adapters.safetensors 2>/dev/null | head -1)
  [ -n "$latest" ] && cp "$latest" runs/qwen3-1p7b-lora-v1b/adapters.safetensors
  sleep 30
done
