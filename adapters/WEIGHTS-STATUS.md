# Adapter weights — clean retrain in progress

The owner approved the recommended path (2026-08-29): all three adapters are being
retrained on **identifier-scrubbed** pairs (client names, domains, contact names, phone
numbers, and token-shaped strings replaced with synthetic stand-ins before training —
42,748 replacements across the graphiti corpus, 17,157 across the capture corpus, zero
JSON breakage). Each clean adapter is re-examined on the scrubbed held-out set and must
pass an adversarial memorization probe — including a screen against the estate's own
sensitive-term registry, with zero hits required — before the weights land here and on
Hugging Face. The originals' full evaluation history stays documented in the cards and
the cost-audit paper.
