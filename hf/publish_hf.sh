#!/bin/bash
# Upload the three adapters to an explicitly selected Hugging Face namespace.
# Requires the owner's normal `hf auth login`. Never run during installation.
set -eu
if [[ $# != 1 || ! $1 =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
  echo "Usage: $0 YOUR_HF_NAMESPACE" >&2
  exit 2
fi
borg_hf_namespace=$1
borg_hf_cli=${BORG_HF_BIN:-hf}
"$borg_hf_cli" auth whoami >/dev/null 2>&1 || { echo "NOT_LOGGED_IN"; exit 3; }
borg_source=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
for spec in \
  "borg-graphiti-extraction-qwen3-1.7b:graphiti-extraction-qwen3-1.7b" \
  "borg-graphiti-extraction-qwen3-4b:graphiti-extraction-qwen3-4b" \
  "borg-capture-extraction-qwen3-4b:capture-extraction-qwen3-4b"; do
  REPO="$borg_hf_namespace/${spec%%:*}"; DIR="$borg_source/adapters/${spec##*:}"
  [ -f "$DIR/adapters.safetensors" ] || { echo "missing weights: $DIR"; exit 4; }
  "$borg_hf_cli" repo create "$REPO" --repo-type model -y 2>/dev/null || true
  "$borg_hf_cli" upload "$REPO" "$DIR" . --repo-type model --commit-message "Borg clean adapter release"
  echo "uploaded $REPO"
done
echo "HF_PUBLISH_DONE"
