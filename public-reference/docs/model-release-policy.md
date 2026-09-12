# Model and adapter release gates

Code release, model redistribution, training permission, privacy, and production promotion are separate decisions. A single `clean_for_publish` boolean cannot establish all five.

## Required immutable manifest

Every proposed checkpoint records: artifact SHA-256; adapter and base identifiers; immutable base revision and tokenizer/chat-template hashes; conversion/quantization; model role and supported call shapes; training recipe; corpus version and rights ledger; teacher provider/model and applicable contract; privacy-evaluation version; semantic-quality metrics; reproducible canary evidence; and approval states with source references.

A model tag or display name is insufficient. Separate `catalogued`, `loaded`, `tested`, and `promoted`. A server's model list does not prove that a particular adapter was loaded. Multiple checkpoint folders do not necessarily represent different model designs.

## Gate 1: input and teacher rights

Default `training_allowed` and `redistribution_allowed` to false until evidenced. A user agreeing to memory capture has not automatically agreed to training or public model release. Track customer permission, proprietary documents, human contributions, public dataset terms, and teacher-output restrictions. Keep rights records for every corpus component and train/validation/test split.

Prefer an initial product that does not train on customer data. For later training, use explicitly permitted teachers and inputs, with dataset quarantine and a signed, purpose-specific agreement. Local generation does not itself clear third-party model or input rights.

## Gate 2: privacy

Scrub before training, not only at response time. Combine sensitive-term and credential scans with adversarial extraction tests, provenance inspection, and held-out evaluations. Record sample counts and limitations. Zero findings in a small generation sample are evidence about that sample, not a proof of absence or a complete privacy guarantee.

Do not publish raw training pairs, private registries used to detect leakage, operational transcripts, or realistic customer examples. Prefer wholly synthetic public fixtures. Weights are excluded from this architecture capsule.

## Gate 3: semantic quality

Measure supported claims, unsupported facts, omissions, contradictions, entity disambiguation, temporal validity, scope assignment, and sensitivity leakage. JSON validity and presence of source-reference fields are necessary formatting checks but do not measure whether the source supports the claim.

For paraphrased fact extraction, exact-string Jaccard can be misleading. Use source-grounded semantic evaluation with blinded adjudication and report disagreement. For graph extraction, evaluate all internal call shapes, not only the external-looking entity/relation schema.

## Gate 4: exact deployment canary

Compare the exact candidate checkpoint to the incumbent on unchanged inputs through the real serving and storage interfaces, using isolated disposable scopes. Verify loaded hashes, tokenization, resource contention, timeout behavior, retries, scope failures, and rollback. A different checkpoint's canary is not transferable. Passing a benchmark qualifies a candidate for canary, not for promotion.

## Existing research artifacts

The surrounding historical repository contains three adapter cards. They describe identifier-scrubbed training, small memorization probes and held-out format/quality results, while explicitly stating that the three released checkpoints have not passed a production promotion canary. The capture card also leaves semantic content fidelity unresolved. Those statements must remain visible wherever the artifacts are discussed.

This reference does not perform new model inference, extraction attacks, training, or legal clearance on those weights. Do not bundle them into a commercial installer on the strength of the repository's top-level license or a historical privacy label. Keep research downloads separate until the above gates are satisfied.
