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

Remote clients share a stateless MCP endpoint without a single-client connection
limit. Independent file/process operations and browser sessions run concurrently.
Calls on the same file, process or browser session coordinate through resource
locks. Native desktop interfaces share one physical keyboard, mouse and focus.
Disconnecting a caller retains its resource locks until native execution ends;
cancelled work still waiting in the queue does not start. Shell commands still
require work ownership because their filesystem side effects cannot be inferred.
Directory moves coordinate with descendant access. Creating new paths briefly
reserves their containing namespace; existing unrelated files remain parallel.
Aliases use observed directory entries and inodes, rechecked after queueing.

Set `computer.concurrency` to tune host work capacity: `max_in_flight` defaults to
64, `queue_limit` to 256, and `wait_seconds` to 30. These bound active and queued
operations, not connected clients. Size them against measured host capacity.
The counters cover native execution admission; authentication, receipt persistence
and filesystem identity preflight occur before that admission.
`borg_status.concurrency` reports capacity, active/waiting work and rejections;
status and memory calls remain outside this queue. Process input is limited to
256,000 bytes with a one-second write deadline. A timed-out input may have sent a
prefix: inspect the process and operation receipt before retrying. Process output
stops at EOF and leaves bytes beyond its response limit for the next read.
The connector accepts legacy ChatGPT `origin` metadata without changing caller
authority. Command calls honor an installed POSIX `shell` and optional timing;
output calls accept `length` and `offset` in bytes. Omit the offset to continue,
or pass a retained offset to replay output. Responses include `next_offset` and
`retained_from`; each process retains at most 2 MB and each read returns at most
256,000 bytes. EOF closes finished pipes. Exact text edits support an explicit
`expected_replacements` count. Unsupported document or URL options fail before
editing; an old client schema does not imply that those capabilities are present.
Directory listings traverse only the requested depth and return at most 1,000
entries. Searches stop after 10,000 scanned entries, 500 matches or five seconds;
`scan_truncated` distinguishes partial scans from exhausted results. Content scans
read at most 2 MB per file and report `content_truncated_files`. Narrow the root or
use the owner's indexed search tools when a scan is incomplete.

Configuration and service commands are described in [INSTALL.md](../docs/INSTALL.md).
For an owner's Cloudflare/ChatGPT connection, follow [WEB.md](../docs/WEB.md). Optional
SSH and credential registries start empty and must be configured for that owner.
