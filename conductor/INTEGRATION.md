# Root installer integration

The installer supplies the runtime and this source tree. The bootstrap command
creates only instance-owned configuration, policies, private router state, the
dedicated primary profile, and its logs. It never performs provider login.

Runtime, profile, logs, worktree, and `BORG_HOME` paths below are canonical
absolute paths. The sole relative argument allowed is the documented
`--config conductors/config.json`, resolved beneath canonical `BORG_HOME`.

## Installed layout

```text
$BORG_HOME/
  config.json                         # root borg-install/v1; never modified here
  app/conductor/
  runtime/<node-tar-directory>/bin/node
  runtime/npm/node_modules/.bin/codex
  conductors/config.json              # conductor-owned schema
  conductors/primary/profile/
    .conductor/http-token             # generated bearer credential, mode 0600
  conductors/primary/logs/
  policies/SEAT-RULES.md
  policies/LEAD-RULES.md
  private/router/
```

## 1. Idempotent bootstrap

The installer must pass every identity, port, and provider runtime path
explicitly. An identical rerun succeeds without replacing an owner-edited
policy or account pin. A conflicting owner, UUID, port, runtime, app, profile,
or logs path fails without overwriting `conductors/config.json`. Bootstrap
never reads or writes the root-owned `$BORG_HOME/config.json` document.

```sh
BORG_HOME=/absolute/path/to/borg
OWNER_ID=new-owner
INSTANCE_ID=2e2f5e46-c196-44df-a7fd-dd509a1b1486
CONDUCTOR_PORT=4747

NODE_BIN="$BORG_HOME/runtime/node-v24.21.0/bin/node"

"$NODE_BIN" \
  "$BORG_HOME/app/conductor/borg-conductor.mjs" bootstrap \
  --borg-home "$BORG_HOME" \
  --config conductors/config.json \
  --owner "$OWNER_ID" \
  --instance-id "$INSTANCE_ID" \
  --port "$CONDUCTOR_PORT" \
  --node-bin "$NODE_BIN" \
  --codex-bin "$BORG_HOME/runtime/npm/node_modules/.bin/codex"
```

The result reports `accountAuthenticated: false`. Bootstrap writes
`conductors/config.json` mode `0600`; the instance directories are mode
`0700`. `--node-bin` is the actual executable inside the installed runtime
tar directory, not the directory itself. When either supplied runtime path
already exists, bootstrap requires it to be an executable file.

Validate the generated config:

```sh
BORG_HOME=/absolute/path/to/borg \
  /absolute/path/to/borg/runtime/node-v24.21.0/bin/node \
  "$BORG_HOME/app/conductor/borg-conductor.mjs" config \
  --config conductors/config.json
```

## 2. Exact startup

Preferred root-callable start contract:

```sh
BORG_HOME=/absolute/path/to/borg \
  exec /absolute/path/to/borg/runtime/node-v24.21.0/bin/node \
  /absolute/path/to/borg/app/conductor/borg-conductor.mjs start \
  --config conductors/config.json \
  --lane primary
```

Equivalent direct conductor contract, useful for a service definition:

```sh
BORG_HOME=/absolute/path/to/borg \
CONDUCTOR_PORT=4747 \
CODEX_HOME=/absolute/path/to/borg/conductors/primary/profile \
CODEX_BIN=/absolute/path/to/borg/runtime/npm/node_modules/.bin/codex \
CONDUCTOR_LOGS=/absolute/path/to/borg/conductors/primary/logs \
  exec /absolute/path/to/borg/runtime/node-v24.21.0/bin/node \
  /absolute/path/to/borg/app/conductor/conductor.mjs
```

The root installer owns service management. Readiness is the interaction-backed
`GET /status` response with `ok: true`, the configured port and the exact
configured `codexHome`; a listening process alone is not readiness proof. The
shipped CLI reads the profile-local bearer token automatically. External local
supervisors may use token-free `GET /healthz` only for process liveness; it is
not conductor initialization or provider readiness proof.

## 3. Status and native authentication

```sh
BORG_HOME=/absolute/path/to/borg \
  "$BORG_HOME/runtime/node-v24.21.0/bin/node" "$BORG_HOME/app/conductor/borg-conductor.mjs" \
  status --config conductors/config.json

BORG_HOME=/absolute/path/to/borg \
  "$BORG_HOME/runtime/node-v24.21.0/bin/node" "$BORG_HOME/app/conductor/borg-conductor.mjs" \
  auth login --config conductors/config.json --lane primary

BORG_HOME=/absolute/path/to/borg \
  "$BORG_HOME/runtime/node-v24.21.0/bin/node" "$BORG_HOME/app/conductor/borg-conductor.mjs" \
  auth pin --config conductors/config.json --lane primary

BORG_HOME=/absolute/path/to/borg \
  "$BORG_HOME/runtime/node-v24.21.0/bin/node" "$BORG_HOME/app/conductor/borg-conductor.mjs" \
  auth status --config conductors/config.json --lane primary
```

`auth login` invokes the configured native Codex binary with only that lane's
dedicated `CODEX_HOME`. `auth pin` stores a one-way digest of the observed
account identity; it does not store or print authentication material.

## 4. Rank and route

```sh
BORG_HOME=/absolute/path/to/borg \
  "$BORG_HOME/runtime/node-v24.21.0/bin/node" "$BORG_HOME/app/conductor/borg-conductor.mjs" \
  rank --config conductors/config.json --capability tools --model gpt-5.6-sol

BORG_HOME=/absolute/path/to/borg \
  "$BORG_HOME/runtime/node-v24.21.0/bin/node" "$BORG_HOME/app/conductor/borg-conductor.mjs" \
  route --config conductors/config.json \
  --cwd /absolute/path/to/worktree \
  --prompt-file /absolute/path/to/brief.md \
  --work-id eco-example.1 \
  --role leaf \
  --model gpt-5.6-sol \
  --effort high
```

`rank` never dispatches work, but it reconciles and may archive private receipt
evidence before scanning claims. `route` is the only dispatch entrypoint; it
writes private intent/receipt evidence and then uses the existing conductor
thread and turn protocol. Root should never dispatch directly to a port when
fleet admission or duplicate protection is required.

## Multi-machine or multi-account extension

Add machines and conductor lanes to `conductors/config.json`; do not edit
source. Each
lane needs a distinct absolute `codexHome`, loopback endpoint, account profile
name, private logs path, and native account pin. A remote machine requires an
absolute configured capacity/claims command that returns current JSON. If any
required observation or provider capability is unavailable, admission fails
with its evidence code.

Recovery: stop the service, preserve `$BORG_HOME/config.json`,
`$BORG_HOME/conductors/config.json`, `policies/`,
`conductors/*/profile`, `conductors/*/logs`, and `private/router`, restore the
prior `app/conductor` tree, and start it with the same direct environment.
