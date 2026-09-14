"""Bind an owner's Cloudflare application and tunnel to their BORG installation."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import platform
import sys
from urllib.parse import urlsplit
import uuid

from installer import config


def configure(doc: dict, *, public_url: str, issuer: str, audience: str,
              owner_email: str, tunnel_credentials: Path) -> dict:
    root = Path(doc["home"])
    credentials = config.absolute_root(tunnel_credentials)
    if not credentials.is_relative_to(root / "cloudflare"):
        raise ValueError("Use this installation's private Cloudflare credentials under BORG_HOME/cloudflare")
    native = config.read_private(credentials)
    try:
        tunnel_id = str(uuid.UUID(native["TunnelID"]))
        secret = base64.b64decode(native["TunnelSecret"], validate=True)
        if len(secret) < 32 or not isinstance(native["AccountTag"], str) or not native["AccountTag"]:
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise ValueError("The private file is not a valid owner-created Cloudflare tunnel credential") from None
    del native, secret
    gateway = {"version": 1, "public_url": public_url, "issuer": issuer, "audience": audience,
               "owner_email": owner_email, "authorization_file": str(root / "borg-context/private/authorization"),
               "upstream_url": f"http://127.0.0.1:{doc['ports']['connector']}/mcp"}
    # Validate through the same gateway implementation that will enforce OAuth.
    sys.path.insert(0, str(root / "borg-context"))
    from cloudflare_gateway import GatewaySettings
    from tempfile import NamedTemporaryFile
    directory = root / "borg-context/cloudflare"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", dir=directory) as temporary:
        json.dump(gateway, temporary)
        temporary.flush()
        GatewaySettings.load(Path(temporary.name))
    hostname = urlsplit(public_url).hostname
    tunnel = {"tunnel": tunnel_id, "credentials-file": str(credentials), "no-autoupdate": True,
              "metrics": f"127.0.0.1:{doc['ports']['tunnel_metrics']}", "loglevel": "warn",
              "ingress": [{"hostname": hostname, "path": "^/mcp$", "service": f"http://127.0.0.1:{doc['ports']['gateway']}"},
                          {"service": "http_status:404"}]}
    # All three files are inert until the owner starts the new instance services.
    for path, value in [(directory / "config.json", gateway), (root / "cloudflare/cloudflared.json", tunnel)]:
        text = json.dumps(value, indent=2) + "\n"
        if path.exists():
            if config.read_private(path) != value:
                raise ValueError("Existing web configuration differs; stop and reconcile it before replacement")
        else:
            config.write_private(path, text)
    current = config.load(root)
    external = {"enabled": True, "public_url": public_url, "tunnel_id": tunnel_id}
    if current["external_access"].get("enabled") and current["external_access"] != external:
        raise ValueError("Existing external access differs; preserve its running configuration")
    current["external_access"] = external
    config.write_private(root / "config.json", json.dumps(current, indent=2) + "\n", replace=True)
    configure_watchdog(current)
    return {"state": "configured_not_started", "url": public_url + "/mcp",
            "next_commands": [str(root / "bin/borg") + " stop watchdog",
                              str(root / "bin/borg") + " start gateway tunnel watchdog"],
            "provider_setup": "Owner must configure their DNS route and Cloudflare Access Managed OAuth application."}


def configure_watchdog(doc: dict) -> None:
    from installer.services import label
    root, ports = Path(doc["home"]), doc["ports"]
    services = {"adapter": label(doc, "connector")}
    urls = {"adapter": f"http://127.0.0.1:{ports['connector']}/mcp"}
    if doc["external_access"]["enabled"]:
        services.update(gateway=label(doc, "gateway"), tunnel=label(doc, "tunnel"))
        urls.update(gateway=f"http://127.0.0.1:{ports['gateway']}/mcp",
                    tunnel=f"http://127.0.0.1:{ports['tunnel_metrics']}/ready")
    body = {"version": 1, "service_manager": "launchd" if platform.system() == "Darwin" else "systemd",
            "services": services, "urls": urls}
    path = root / "borg-context/watchdog/config.json"
    config.write_private(path, json.dumps(body, indent=2) + "\n", replace=path.exists())
