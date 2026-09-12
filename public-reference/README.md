# BORG: public architecture reference

**Release type: architecture, contracts, and release tooling. Not a complete installer or a production service.**

BORG connects agent clients to shared, source-linked memory and separately authorized work execution. This reference describes how those parts fit together without distributing an operator's private installation.

The package contains no memory database, transcripts, training examples from real users, model weights, account lists, credentials, or deployment configuration. The examples are synthetic. The release builder uses an exact file allowlist; it does not copy a working directory or Git history.

## Start here

- [System architecture](docs/architecture.md): components, boundaries, and data flow.
- [Client integrations](docs/integrations.md): Claude, Codex, Grok, ChatGPT, and conductor contracts.
- [Product design](docs/productization.md): self-hosted service offering and delivery gates.
- [Licensing review](docs/licensing.md): code, models, provider contracts, and naming.
- [Model release policy](docs/model-release-policy.md): independent privacy, rights, and quality gates.
- [Security](docs/security.md): trust, isolation, retention, and recovery.

## Validate and export

Python 3.11 or newer; standard library only. Run from this directory:

```sh
python3 -m unittest discover -s tests -v
python3 tools/build_capsule.py --check
python3 tools/build_capsule.py --output ./borg-architecture.zip
```

Inspect the generated archive and checksums before distributing it. Optional `--deny-file /private/review-terms.txt` screens operator-specific terms without printing matches. A passing pattern scan is not a guarantee that arbitrary prose is public, and it says nothing about model memorization or the rest of a repository.

## Relationship to the repository

The surrounding repository contains historical implementation snapshots and research adapters. Those files are **not** included by this package's allowlist. Some historical entrypoints depend on modules not present in the published snapshot. The reference does not certify that snapshot as reproducible, privacy-cleared, or commercially licensed as a whole.

Current installations can be ahead of published source. Porting them requires configuration extraction, provenance review, tests, and a clean-machine acceptance run. Do not copy an operator's runtime folder into a release.

This reference preserves the project's MIT notice. Third-party software, service subscriptions, model weights, trademarks, and private data are not relicensed by that notice. See the licensing review before building a commercial distribution.
