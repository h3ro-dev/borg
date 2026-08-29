# Adapter weights — arriving

The three adapter cards in this directory are final. The `.safetensors` weight files land
as soon as the pre-release memorization review completes (LoRA weights can regurgitate
training identifiers under adversarial prompting; ours are trained on private operational
data, so the weights ship only after that review closes). The machinery that trains
identical adapters from *your* data is already fully present in `training/`.
