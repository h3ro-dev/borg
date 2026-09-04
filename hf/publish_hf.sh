#!/bin/bash
# Uploads the three clean adapters to Hugging Face under the logged-in account.
# Requires: hf auth login (one time, by the owner). Safe to re-run.
set -e
HF=/Users/utlyze/Library/Python/3.9/bin/hf
$HF auth whoami 2>/dev/null | grep -qv "Not logged in" || { echo "NOT_LOGGED_IN"; exit 3; }
B=/Users/utlyze/Projects/borg
for spec in \
  "borg-graphiti-extraction-qwen3-1.7b:graphiti-extraction-qwen3-1.7b" \
  "borg-graphiti-extraction-qwen3-4b:graphiti-extraction-qwen3-4b" \
  "borg-capture-extraction-qwen3-4b:capture-extraction-qwen3-4b"; do
  REPO="${spec%%:*}"; DIR="$B/adapters/${spec##*:}"
  [ -f "$DIR/adapters.safetensors" ] || { echo "missing weights: $DIR"; exit 4; }
  $HF repo create "$REPO" --repo-type model -y 2>/dev/null || true
  $HF upload "$REPO" "$DIR" . --repo-type model --commit-message "Borg clean adapter release"
  echo "uploaded $REPO"
done
echo "HF_PUBLISH_DONE"
