# Connect your own BORG to ChatGPT on the web

The public repository distributes software. Installing it creates your own BORG;
adding an MCP URL to ChatGPT connects to an already running BORG and does not install
a computer fleet. Your machine must be awake and its services running.

## Configure your Cloudflare account

1. Complete the local installation and check `borg doctor`.
2. In your own Cloudflare account, create a locally managed tunnel and DNS hostname.
   Keep its private tunnel credential JSON under `BORG_HOME/cloudflare` with mode0600.
   Use the bundled cloudflared executable under `BORG_HOME/runtime/cloudflared`.
   Follow Cloudflare's [locally managed tunnel guide](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/create-local-tunnel/).
3. Create a Cloudflare Access MCP server application for that hostname and enable
   Managed OAuth. Restrict the policy to your exact sign-in email. Record the Access
   issuer URL and application audience. BORG's gateway validates the Access JWT's
   issuer, audience and owner email. See [Managed OAuth](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/managed-oauth/).
4. Configure BORG with those non-secret identifiers and the private credential path:

```sh
"$HOME/.borg/bin/borg" web \
  --public-url https://borg.example.com \
  --issuer YOUR_ACCESS_ISSUER \
  --audience YOUR_APPLICATION_AUDIENCE \
  --owner-email you@example.com \
  --tunnel-credentials "$HOME/.borg/cloudflare/credentials.json"
"$HOME/.borg/bin/borg" stop watchdog
"$HOME/.borg/bin/borg" start gateway tunnel watchdog
```

Replace every example value. The command creates this home's gateway, tunnel and
watchdog configuration; it does not create Cloudflare account resources or DNS.
The root hostname deliberately returns404. The MCP endpoint is `/mcp`; unauthenticated
requests should encounter authentication rather than expose tools.

## Add the ChatGPT app

Use the custom MCP app controls available to your ChatGPT account/workspace. Enter
`https://borg.example.com/mcp` using your actual hostname, select OAuth, complete your own Access sign-in, scan the
tools and enable the actions you intend to use. Test `borg_status` in a fresh chat,
then one reversible action such as creating and reading a disposable file.

ChatGPT plan, workspace and action controls determine which MCP operations the client
can use. BORG does not override those controls. Consult OpenAI's current
[developer-mode and MCP app guide](https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt).

When server tool definitions change, refresh and review the app's actions where your
workspace supports it. New actions may arrive disabled. Some published app types
require recreating and republishing the app. Existing chats may retain cached tool
schemas; verify the current list from a new chat. These update requirements belong
to ChatGPT and can change independently of the BORG server.

## Diagnose a connection

- Local `borg call borg_status` fails: inspect local connector and memory logs first.
- Local calls work but `/mcp` returns404: check hostname, DNS route, tunnel ingress and
  the exact `/mcp` path. The hostname's bare `/` is intentionally not an MCP page.
- OAuth sign-in fails: check the Access application, issuer, audience and exact email.
- A connection later expires: check your provider's refresh-token configuration and
  reauthenticate through the client when needed.
- A tool is absent or its inputs are rejected: compare `borg tools` with the client's
  refreshed actions before changing the server.

`borg doctor` reports local gateway/tunnel health separately from actual web-client
OAuth acceptance. Keep this installation's credentials private; never share a live
owner connector as a public installer.
