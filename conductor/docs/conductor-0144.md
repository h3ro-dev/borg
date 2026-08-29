# Conductor compatibility with Codex CLI 0.144.x

Schema source: `codex-cli 0.144.3 app-server generate-json-schema
--experimental`, inspected on 2026-07-13. This is source/test readiness, not a
live app-server canary.

## Protocol adjustments

- After the `initialize` response resolves, the client sends the JSON-RPC
  notification `initialized` before making any other request.
- Current `item/commandExecution/requestApproval` and
  `item/fileChange/requestApproval` callbacks receive
  `{ "decision": "decline" }`.
- Legacy `execCommandApproval` and `applyPatchApproval` callbacks retain
  `{ "decision": "denied" }` for older app-server compatibility.
- Additional-permission requests receive an empty, turn-scoped permission
  profile. MCP elicitations receive `action: "decline"`. Other server requests
  fail closed with JSON-RPC `-32601`; `currentTime/read` is the sole
  non-interactive exception and returns whole Unix seconds. The conductor does
  not fabricate user input, credentials, attestations, or tool results.

## Log boundary

New log directories and all log files use owner-only modes (`0700` and `0600`).
Persistent thread logs contain lifecycle metadata only. Prompts, commands,
diffs, tool payloads, parse-error content, app-server stderr content, and raw
approval parameters are not persisted. Full notifications remain available
only in the bounded in-memory ring exposed on loopback.

## Required live canary

Before this is called operational on another machine:

1. Confirm that machine's selected `CODEX_BIN` reports a compatible 0.144.x
   version and uses machine-local authentication.
2. Start one isolated conductor instance on an unused loopback port and a new,
   empty log directory. Do not reuse or copy a task database.
3. Prove `initialize` then `initialized` ordering and successful `thread/list`.
4. Run one disposable read-only thread and verify its start/turn/completion
   lifecycle without granting an approval.
5. If a controlled approval callback is exercised, prove `decline` on the
   current callback. Treat any unexpected server-request method as a canary
   failure until a schema-valid, fail-closed handler exists.
6. Verify log directory/file modes and scan the persisted logs for sentinel
   prompt, command, diff, and secret-like values; none may be present.
7. Stop the isolated instance and prove its child process and listener exited.

Legacy 0.137 behavior is unit-covered but is not live-canary verified by this
change.
