"""Read-only onboarding observations; commands are instructions, never executed.

The caller supplies a validated borg-install/v1 document (normally config.load).
Only a fixed set of non-secret metadata files beneath that home is inspected.
A configured receipt or account pin never proves current provider/service readiness.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

from installer import blueprint, config

SCHEMA = "borg-onboarding/v1"
MAX_METADATA_BYTES = 65536


def _metadata(root: Path, relative: str, expected: type = dict) -> tuple[object, str]:
    """Bounded, no-follow descriptor walk; never open credentials or arbitrary paths."""
    directory = None
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        parts = Path(relative).parts
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or info.st_mode & 0o077
                    or info.st_size > MAX_METADATA_BYTES):
                return expected(), "unsafe_or_oversized"
            data = stream.read(MAX_METADATA_BYTES + 1)
        if len(data) > MAX_METADATA_BYTES:
            return expected(), "unsafe_or_oversized"
        value = json.loads(data)
        return (value, "observed") if isinstance(value, expected) else (expected(), "invalid")
    except FileNotFoundError:
        return expected(), "missing"
    except (OSError, ValueError, RecursionError):
        return expected(), "unreadable_or_invalid"
    finally:
        if directory is not None:
            os.close(directory)


def _file(root: Path, relative: str, *, executable: bool = False) -> bool:
    path = root / relative
    try:
        # No redirected directories and no filesystem search outside this home.
        for parent in [path, *path.parents]:
            if parent == root:
                break
            if parent.is_symlink():
                return False
        info = path.stat()
        return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and not info.st_mode & 0o022 and (not executable or os.access(path, os.X_OK)))
    except OSError:
        return False


def _command(argv: list[str], purpose: str, *, requires: list[str] | None = None,
             env: dict | None = None, cwd: str | None = None) -> dict:
    return {"argv": argv, "env": env or {}, "cwd": cwd, "purpose": purpose,
            "requires": requires or []}


def _step(id: str, title: str, state: str, observed: dict, missing: list[str],
          commands: list[dict], limits: list[str], *, required: bool = True) -> dict:
    return {"id": id, "title": title, "required": required, "state": state,
            "ready": False if state in {"missing", "incomplete", "unsupported"} else None,
            "observed": observed, "missing_requirements": missing,
            "commands": commands, "evidence_limits": limits}


def plan(doc: dict) -> dict:
    """Return ordered JSON-serializable steps without login, subprocesses or network.

    `ready: null` means not verified; false means an observed setup gap. No step
    asserts live readiness from files. Optional steps never block the core plan.
    Unknown metadata keys and all raw provider/account fields are omitted.
    """
    if doc.get("schema") != config.SCHEMA:
        raise ValueError("Expected a borg-install/v1 configuration")
    config.validate(doc)
    root = config.absolute_root(doc["home"])
    conductor, conductor_read = _metadata(root, "conductors/config.json")
    receipt, receipt_read = _metadata(root, "conductors/primary/borg-client-receipt.json")
    prepared, prepared_read = _metadata(root, "models/mlx/prepared.json")
    fleet, hosts_read = _metadata(root, "borg-context/fleet.json")
    hosts = fleet.get("hosts", []) if fleet.get("schema") == "borg-fleet/v1" else []
    hosts = hosts if isinstance(hosts, list) else []
    conductor_matches = (conductor.get("borgHome") == str(root)
                         and conductor.get("owner") == doc["owner"]
                         and conductor.get("instance_id") == doc["instance_id"]
                         and conductor.get("schemaVersion") == 1)
    if not conductor_matches:
        conductor = {}
    lanes = conductor.get("conductors", [])
    lanes = [row for row in lanes if isinstance(row, dict)] if isinstance(lanes, list) else []
    primary = next((row for row in lanes if row.get("id") == "primary"), {})
    profile = str(root / "conductors/primary/profile")
    primary_matches = primary.get("codexHome") == profile
    pin = primary.get("accountPin")
    pin_present = primary_matches and isinstance(pin, str) and bool(re.fullmatch(r"[0-9a-f]{64}", pin))
    launcher = _file(root, "bin/borg", executable=True)
    source = _file(root, "app/borg.py")
    installed = launcher and source
    borg = str(root / "bin/borg")

    def command(*args: str, purpose: str, requires: list[str] | None = None) -> dict:
        return _command([borg, *args, "--home", str(root)], purpose,
                        requires=requires if requires is not None else ["installation"])

    install_command = _command(["./install.sh", "--home", str(root), "--owner", doc["owner"]],
        "Complete installation from the same source release checkout",
        requires=["Run in the release checkout containing install.sh; preserve any existing home"])
    if doc.get("blueprint"):
        install_command["argv"] += ["--blueprint", str(root / "blueprint.json"), "--machine", doc["blueprint"]["machine_id"]]
    steps = [_step("installation", "Install an independent BORG", "present" if installed else "missing",
        {"launcher_present": launcher, "application_present": source},
        [] if installed else ["Complete the pinned installer in a private owner-controlled home"],
        [install_command], ["File presence is not runtime integrity, model installation or service health proof.",
        "Apple Silicon macOS has native acceptance; Linux and Intel macOS remain unverified; Windows is unsupported."])]
    steps.append(_step("local-services", "Verify local services and models", "not_checked", {},
        ["Live authenticated service, storage, model and capture checks"],
        [command("doctor", purpose="Check the live installation"), install_command],
        ["No service or model is started or probed by this plan.",
         "For --no-start installations, rerun the same installer to pull models and initialize stores; start alone is insufficient."]))
    steps.append(_step("codex-login", "Sign in with your own Codex account",
        "configured" if pin_present else "incomplete", {"primary_profile_matches": primary_matches,
        "account_pin_present": pin_present, "authentication": "not_checked"},
        ["Verify the native account still matches its pin"] if pin_present else ["Native login and account identity pin"],
        [command("auth", "codex", purpose="Interactive native login, then pin this lane's account",
                 requires=["installation", "local-services", "Your own provider account and interactive sign-in"]),
         command("doctor", purpose="Verify authentication and the account pin")],
        ["No credential file or account identity is read or returned.", "A stored pin does not prove current login or allowance."]))
    client_configured = (receipt.get("state") == "configured" and receipt.get("profile") == profile
                         and receipt.get("hooks") == 4 and receipt.get("native_trust_verified") is True
                         and receipt.get("mcp_server") == "borg"
                         and receipt.get("account_credentials_imported") is False)
    steps.append(_step("codex-client", "Connect Codex to BORG tools and capture",
        "configured" if client_configured else "incomplete",
        {"receipt": receipt_read, "matching_client_receipt": client_configured},
        ["Verify current MCP connection and native hook trust"] if client_configured else ["Installer-created client configuration and four trusted lifecycle hooks"],
        [command("tools", purpose="Read the live BORG tool schemas"),
         command("doctor", purpose="Verify native hook registration and trust"), install_command],
        ["The receipt describes an earlier configuration action, not today's hook or MCP readiness.",
         "Installer client setup targets only conductors/primary/profile, never a shared global profile."]))
    native_commands = []
    runtime = conductor.get("runtime", {})
    node = runtime.get("nodeBin") if isinstance(runtime, dict) else None
    if isinstance(node, str):
        node_path = Path(node)
        if node_path.is_absolute() and node_path.is_relative_to(root / "runtime") and ".." not in node_path.parts:
            if _file(root, str(node_path.relative_to(root)), executable=True):
                for args, purpose in [(["status"], "Check the configured conductor"),
                                      (["auth", "status", "--lane", "primary"], "Check native account and pin without displaying credentials")]:
                    native_commands.append(_command([node, str(root / "app/conductor/borg-conductor.mjs"),
                        *args, "--config", str(root / "conductors/config.json")], purpose,
                        env={"BORG_HOME": str(root), "PATH": str(node_path.parent) + ":/usr/bin:/bin"},
                        requires=["installation", "local-services"]))
                steps[3]["commands"].append(_command(
                    [str(root / "runtime/npm/node_modules/.bin/codex")],
                    "Open Codex with this installation's client configuration",
                    env={"CODEX_HOME": profile, "PATH": str(node_path.parent) + ":/usr/bin:/bin"},
                    cwd=str(root), requires=["installation", "codex-login"]))
    steps.append(_step("codex-conductor", "Verify the Codex conductor", "configured" if primary_matches else "incomplete",
        {"metadata": conductor_read, "instance_matches": conductor_matches,
         "primary_profile_matches": primary_matches, "native_runtime_command_available": bool(native_commands)},
        ["Live app-server initialization and current account/allowance admission"],
        native_commands or [command("doctor", purpose="Check conductor and provider state")],
        ["Configured lanes are not running agents. A listening port alone does not prove readiness.",
         "Routing also requires fresh machine capacity, claims and provider allowance; see conductor/INTEGRATION.md."]))
    providers = conductor.get("providers", {})
    providers = providers if isinstance(providers, dict) else {}
    for provider in ["claude", "grok"]:
        row = providers.get(provider, {})
        enabled = isinstance(row, dict) and row.get("enabled") is True
        missing = (["Install and authenticate your own Claude Code CLI", "Configure an explicit provider binary and isolated launch environment",
                    "Native thread status, mid-turn steering and provider allowance routing are not implemented"] if provider == "claude" else
                   ["Supply your own compatible Grok CLI and complete its native login", "Configure explicit grokBin, grokHome, expectedVersion, readinessMarkerPath, statePath and loopback port",
                    "No supported installer login or readiness-marker provisioning command is supplied"])
        provider_profile = str(root / "providers" / provider / "profile")
        provider_env = {"CLAUDE_CONFIG_DIR" if provider == "claude" else "GROK_HOME": provider_profile}
        provider_commands = [
            _command(["mkdir", "-p", "-m", "700", provider_profile], "Create a dedicated private provider profile",
                     requires=["Owner-controlled BORG home; profile path must not contain symlinks"]),
            _command([provider, *(["auth", "login"] if provider == "claude" else ["login", "--oauth"])],
                     "Sign in through your provider's native interactive flow", env=provider_env, cwd=str(root),
                     requires=["Install your own compatible provider CLI on PATH", "Dedicated private profile", "Your own provider account"]),
            _command([provider, "mcp", "add", "--scope", "user", "--transport", "stdio", "borg", "--",
                      borg, "mcp-stdio", "--home", str(root)],
                     "Add BORG tools in the dedicated provider profile", env=provider_env, cwd=str(root),
                     requires=["installation", "Dedicated private profile", "Verify these flags with your installed CLI help"])]
        provider_commands.append(_command([provider, *(["auth", "status"] if provider == "claude" else ["mcp", "doctor"])],
            "Inspect native provider status; then test BORG tools in that client", env=provider_env, cwd=str(root),
            requires=["Complete native login and client setup"]))
        steps.append(_step(provider, "Set up optional " + provider.capitalize(), "manual_setup_required",
            {"enabled_in_config": enabled, "authentication": "not_checked"}, missing, provider_commands,
            ["borg auth supports codex only; no provider login is performed by onboarding.",
             "Enabled configuration is not proof of a working provider. See docs/SETUP.md for the supplied source boundary."], required=False))
    rows = prepared.get("adapters", [])
    prepared_count = sum(isinstance(row, dict) and row.get("state") == "prepared_for_canary"
                         and row.get("active") is False for row in rows) if isinstance(rows, list) else 0
    steps.append(_step("adapters", "Inspect optional inactive model adapters", "prepared" if prepared_count else "inactive",
        {"preparation_receipt": prepared_read, "prepared_receipt_count": prepared_count, "activation": "not_verified"},
        ["Apple Silicon macOS for MLX preparation", "Base-model and adapter integrity checks followed by compatibility and extraction canaries"],
        [command("adapters", "list", purpose="Verify included adapter weights"),
         command("adapters", "prepare", "all", purpose="Download pinned MLX bases for canaries; does not activate adapters",
                 requires=["installation", "Apple Silicon macOS", "Disk and network capacity for model downloads"])],
        ["Preparation receipts are not current weight integrity or extraction quality proof.",
         "Historical training revisions are unknown; preparation never promotes an adapter."], required=False))
    external = doc.get("external_access", {})
    web_enabled = isinstance(external, dict) and external.get("enabled") is True
    steps.append(_step("web-connector", "Connect an optional web client", "configured" if web_enabled else "not_configured",
        {"enabled_in_config": web_enabled, "oauth_acceptance": "not_checked"},
        ["Your own Cloudflare tunnel, DNS and Access Managed OAuth application", "Actual web-client sign-in and tool acceptance"],
        [command("web", "--public-url", "<PUBLIC_HTTPS_URL>", "--issuer", "<ACCESS_ISSUER>",
                 "--audience", "<ACCESS_AUDIENCE>", "--owner-email", "<OWNER_EMAIL>",
                 "--tunnel-credentials", str(root / "cloudflare/credentials.json"),
                 purpose="Configure this owner's web connector",
                 requires=["local-services", "Replace all <...> identifiers; supply your own private tunnel credential file"]),
         command("stop", "watchdog", purpose="Reload watchdog configuration after web setup"),
         command("start", "gateway", "tunnel", "watchdog", purpose="Start optional web services"),
         command("doctor", purpose="Check local gateway and tunnel health")],
        ["No tunnel credential is read. Local service health cannot prove web-client OAuth acceptance.",
         "Web access connects to an existing installation; it does not install or discover machines."], required=False))
    host_count = sum(isinstance(row, dict) and row.get("enabled", True) is not False for row in hosts)
    steps.append(_step("own-machines", "Add only your own machines", "configured" if host_count else "not_configured",
        {"host_registry": hosts_read, "enabled_host_entries": host_count},
        ["Resident BORG connector on each target, trusted SSH alias and explicit installation identity pin",
         "Fresh target capacity and a verified operation receipt"],
        [command("fleet", "list", purpose="Inspect enrolled target configuration"),
         command("tools", "fleet_", purpose="Discover native multi-machine tools"),
         command("call", "fleet_hosts", purpose="Verify target identities and discover actual capabilities")],
        ["Registry entries are not connectivity or capacity proof. No SSH request is made by this plan.",
         "Use fleet_tools and fleet_call with an explicit host ID; native paths and session IDs belong to that target.",
         "SSH job registration and conductor machine/account lane registration are separate."], required=False))
    steps.append(_step("os-permissions", "Allow the OS features you choose to use", "not_checked", {},
        ["Grant macOS Accessibility, Screen Recording or Automation only when the chosen operation requests it"],
        [command("tools", "ui_", purpose="Inspect supported OS operations")],
        ["Headless browser success does not prove OS UI permissions; native permissions require an owner session."], required=False))
    selection = blueprint.selected_machine(doc)
    if selection is not None:
        chosen = set(selection["components"])
        mapping = {"codex-login": "codex", "codex-client": "codex", "codex-conductor": "codex",
                   "claude": "claude", "grok": "grok", "adapters": "adapters",
                   "own-machines": "fleet", "os-permissions": "desktop"}
        steps = [step for step in steps if step["id"] not in mapping or mapping[step["id"]] in chosen]
        summary = blueprint.describe(selection, blueprint.catalog())
        for step in steps:
            if step["id"] == "local-services":
                step["title"] = "Verify selected local services"
                step["observed"] = {"selected_services": summary["services"]}
                step["missing_requirements"] = ["Run native readiness checks for every selected local service"]
                step["evidence_limits"] += summary["warnings"]
            if step["id"] == "codex-client" and not blueprint.full(doc):
                matched = (receipt.get("state") == "configured" and receipt.get("profile") == profile
                           and receipt.get("hooks") == 0 and receipt.get("mcp_server") == "borg"
                           and receipt.get("account_credentials_imported") is False)
                step.update(title="Connect Codex to BORG tools", state="configured" if matched else "incomplete",
                            observed={"receipt": receipt_read, "matching_client_receipt": matched},
                            missing_requirements=["Verify live BORG MCP configuration; tools nodes have no memory capture hooks"],
                            ready=None if matched else False)
                step["commands"] = [command("doctor", purpose="Verify selected services and Codex MCP configuration"), install_command]
            if step["id"] in {"claude", "grok"}:
                step["required"] = True
        for item in summary["setup"]:
            steps.append(_step("selected-" + item["id"], "Set up selected " + item["id"],
                "manual_setup_required", {"catalog_status": item["status"], "readiness": "not_verified"},
                item["steps"], [], item["limitations"] + ["Documentation: " + item["docs"],
                "Selection is not proof of installed provider binaries, authentication, deployment or runtime readiness."]))
    return {"schema": SCHEMA, "state": "action_required" if any(step["required"] and step["ready"] is False for step in steps)
            else "verification_required", "ready": None, "steps": steps,
            "evidence_limits": ["Read-only metadata snapshot; no commands, network requests, login or profile writes performed.",
                                "Only selected non-secret facts are emitted. Run the indicated native checks for current readiness."]}
