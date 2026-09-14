# Native BORG connector

This directory contains the authenticated MCP adapter and optional Cloudflare OAuth
gateway. The independent installer configures it under `BORG_HOME/borg-context` with
new private credentials, explicit project roots and native computer/browser tools.

The tool surface includes scoped memory and graph recall, files, processes, durable
jobs, browser automation, operating-system UI, explicitly registered SSH hosts and
value-blind credential handles. Desktop Commander is not a dependency.

Run `borg tools` for the actual current schemas, and `borg call borg_status` to check
native component status. Web clients connect to the owner's `/mcp` endpoint; local
clients use a generated stdio proxy without a bearer value in their client config.

Writes have durable operation receipts. A lost response is not evidence that a write
failed; inspect its receipt before retrying. Blocking credential and receipt I/O runs
off the MCP event loop. Receipt finalizers serialize terminal state publication, and
the watchdog uses a single instance lock and bounded recovery attempts.

Configuration and service commands are described in [INSTALL.md](../docs/INSTALL.md).
For an owner's Cloudflare/ChatGPT connection, follow [WEB.md](../docs/WEB.md). Optional
SSH and credential registries start empty and must be configured for that owner.
