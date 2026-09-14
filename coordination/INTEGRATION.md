# Root integration contract

There are no deviations from the requested root layout or bootstrap signature.

## Bootstrap

The source tree must already exist at `$BORG_HOME/app/coordination`, and the
BORG Python interpreter must exist at `$BORG_HOME/mem0/venv/bin/python`.

```sh
"$BORG_HOME/mem0/venv/bin/python" \
  "$BORG_HOME/app/coordination/bin/borg-coordination" \
  bootstrap --home "$BORG_HOME" --owner OWNER_SLUG --port PORT
```

Bootstrap is idempotent for the same home, owner, port, and pinned policy. It
creates an owner credential plus a distinct `OWNER_SLUG-connector` identity by
calling the native Hub enrollment contract. Output contains paths, IDs, and
grant receipts only; secret values remain in mode `0600` files.

`$BORG_HOME/coordination/service.json` is mode `0600` and has this shape:

```json
{
  "args": [
    "/absolute/BORG_HOME/mem0/venv/bin/python",
    "/absolute/BORG_HOME/app/coordination/comms/bin/inbox",
    "serve",
    "--state-dir",
    "/absolute/BORG_HOME/coordination/data",
    "--host",
    "127.0.0.1",
    "--port",
    "8795"
  ],
  "env": {
    "BORG_HOME": "/absolute/BORG_HOME",
    "BORG_COORDINATION_CONFIG": "/absolute/BORG_HOME/coordination/config.json",
    "PYTHONPATH": "/absolute/BORG_HOME/app/coordination"
  }
}
```

Every `env` value is a non-secret absolute path. A native lifecycle manager may
execute `args` with those environment additions, or root may use the blocking
start command:

```sh
"$BORG_HOME/mem0/venv/bin/python" \
  "$BORG_HOME/app/coordination/bin/borg-coordination" \
  start --home "$BORG_HOME"
```

## Python dependencies

The core lock is intentionally empty because the native Hub/bootstrap uses
only the CPython 3.10+ standard library. Root can still run the uniform install
step safely:

```sh
"$BORG_HOME/mem0/venv/bin/python" -m pip install \
  --requirement "$BORG_HOME/app/coordination/requirements-core.lock"
```

`requirements-optional-fleet.lock` is a fully version-pinned closure for the
retained desktop integration, resolved on CPython 3.14/macOS. Do not install it
unless that owner explicitly enables and configures the optional integration.

## Authenticated readiness

After the service starts, this command first checks native `/health`, then uses
the private owner client to call native `authorize` for `owner.read` on `/`:

```sh
"$BORG_HOME/mem0/venv/bin/python" \
  "$BORG_HOME/app/coordination/bin/borg-coordination" \
  status --home "$BORG_HOME"
```

Exit `0` plus `{"status":"ready",...}` proves process readiness and owner
authentication. Exit `1` is not ready. The probe prints no credential value or
credential path.

## Agent enrollment

This creates a distinct private client and an explicit, non-delegable native
grant. Repeat `--grant-action` to replace the standard messaging/discovery/read
set.

```sh
"$BORG_HOME/mem0/venv/bin/python" \
  "$BORG_HOME/app/coordination/bin/borg-coordination" \
  enroll --home "$BORG_HOME" --agent AGENT_ID \
  --runtime RUNTIME --machine MACHINE
```

Use the returned `client_config` with the unchanged native CLI:

```sh
"$BORG_HOME/mem0/venv/bin/python" \
  "$BORG_HOME/app/coordination/comms/bin/inbox" \
  call --config CLIENT_CONFIG --operation messages.poll \
  --params-file PARAMS_JSON --request-id REQUEST_UUID
```

Native mutation request IDs are idempotency keys. A delivery lease ID returned
by `messages.poll` is required while acknowledging a leased delivery.

## Beads install and init

The supported upstream release is `gastownhall/beads` v1.2.2, commit
`6c124203e771433a3550c348771a5b5e27fd3c21`. Its Go module path remains
`github.com/steveyegge/beads`. An embedded-Dolt-capable pinned install is:

```sh
CGO_ENABLED=1 GOFLAGS=-tags=gms_pure_go \
  go install github.com/steveyegge/beads/cmd/bd@v1.2.2
bd --version
```

The exact init argv and working directory can be read without mutation:

```sh
"$BORG_HOME/mem0/venv/bin/python" \
  "$BORG_HOME/app/coordination/bin/borg-coordination" \
  beads-command --home "$BORG_HOME"
```

Run the validated native init through the wrapper:

```sh
"$BORG_HOME/mem0/venv/bin/python" \
  "$BORG_HOME/app/coordination/bin/borg-coordination" \
  init-beads --home "$BORG_HOME"
```

This checks exactly `bd 1.2.2`, creates `$BORG_HOME/beads` privately, and runs
`bd init --non-interactive --init-if-missing --skip-agents --skip-hooks
--prefix OWNER_SLUG` with that directory as `cwd`. The `cwd` is necessary:
native `bd -C` refuses a fresh directory before a project exists. It writes the
native local `config.yaml` before initialization: upstream 1.2.2 otherwise ignores
an empty `BEADS_DIR` and may select an ancestor workspace. Beads/Dolt environment
overrides are cleared, `BEADS_DIR` is explicit, and embedded mode is selected.
The operating-system home and existing workspaces remain unchanged. Successful
initialization must produce local metadata, project identity and embedded storage.
The full installer adds `BORG_HOME/bin/bd`; this command selects that installation
and rejects redirected or foreign storage before invoking the upstream executable.

## Owner policy

Bootstrap preserves a pre-authored
`$BORG_HOME/coordination/owner-policy.md`; otherwise it creates a generic local
template. The package contains no shared estate policy. After editing, accept
the exact local bytes explicitly:

```sh
"$BORG_HOME/mem0/venv/bin/python" \
  "$BORG_HOME/app/coordination/bin/borg-coordination" \
  policy-pin --home "$BORG_HOME"
```

## Recovery

Stop the lifecycle process, preserve `$BORG_HOME/coordination/data`, and restore
the prior source plus its prior `service.json`. The SQLite database, hash-only
credential registry, client secret files, and outboxes are the durable state.
Do not regenerate identities to recover a stopped service. Re-run bootstrap
only for the same owner/port/policy; conflicting owner, port, or unpinned policy
changes fail closed.
