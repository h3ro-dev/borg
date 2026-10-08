These operator-reviewed patch templates apply to existing site-owned software,
not to the portable BORG installation. Keep the rendered patch, exact preimages,
SHA256 before/after, tests and undo commands in that site's private repair record.

For `20261008-worker-binding-and-backlog.patch`, substitute `@WORKER_HOME@`,
`@RETIRED_HOME@`, `@WORKER_INSTANCE@`, `@RETIRED_INSTANCE@` and `@OWNER@` from
already verified fleet identities. This changes no identities or grants. Verify
the old binding before applying; preserve the old mailbox and pending-read evidence.

Run `python -B -m unittest discover -s ops/tests -v` with
`BORG_SITE_PATCH_FIXTURE` pointing at staged `dot_inbox_bridge.py`,
`mailbox_guard.py` and `live.py`, and the corresponding `BORG_SITE_WORKER_HOME`,
`BORG_SITE_RETIRED_HOME`, `BORG_SITE_WORKER_INSTANCE`,
`BORG_SITE_RETIRED_INSTANCE`, `BORG_SITE_OWNER` environment values. Tests mock
all fleet calls and execute the real backlog readers against a synthetic home.
