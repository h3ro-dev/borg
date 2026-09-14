# Independent BORG coordination

This package installs the native Agent Inbox Hub for one BORG owner. It keeps
the supplied `comms.hub` Store, HTTP transport, authenticated principals,
grants, message leases, request receipts, discoveries, and versioned
assignments. It does not call or fall back to another Inbox.

The default installation is local-only:

- source: `$BORG_HOME/app/coordination`
- durable Hub state and private clients: `$BORG_HOME/coordination/data`
- owner config: `$BORG_HOME/coordination/config.json`
- lifecycle descriptor: `$BORG_HOME/coordination/service.json`
- owner-authored policy: `$BORG_HOME/coordination/owner-policy.md`
- upstream Beads project: `$BORG_HOME/beads`
- bind: `127.0.0.1` on the selected port

The core uses only the Python standard library. Fleet context, estate,
browser, and desktop providers are disabled by default; their supplied source
is retained as an explicit embedding surface and never activates without
owner configuration.

See [INTEGRATION.md](INTEGRATION.md) for exact commands and
[PROVENANCE.md](PROVENANCE.md) for the source boundary.
