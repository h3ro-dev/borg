"""Generate actual runtime configuration additions, without writing live settings.

Native schemas and limits: design/NATIVE-INSTALL-SEAMS.md.
"""
import json
from pathlib import Path
import shlex


def additions(runtime, package_root, client_config, machine):
    root = Path(package_root)
    mcp_args = [str(root / "comms/bin/inbox"), "stdio", "--config", str(client_config)]
    def hook(phase, active=False):
        argv = ["python3", str(root / "comms/bin/inbox-native"), "--runtime", runtime,
                "--machine", machine, "--config", str(client_config), "--phase", phase]
        if active:
            # Scoped to evidence incorporation during a running task. The
            # 90-second fixture exceeds the old 60-second lease; 180 seconds
            # leaves headroom and bounds retry delay after a dropped consumer.
            argv += ["--active", "--lease-seconds", "180"]
        return shlex.join(argv)
    if runtime == "codex":
        blocks = ['[mcp_servers.agent-inbox]\ncommand = "python3"\nargs = ' + json.dumps(mcp_args)]
        for event, phase in (("SessionStart", "start"), ("UserPromptSubmit", "checkpoint"),
                             ("PostToolUse", "checkpoint")):
            blocks.append('[[hooks.' + event + ']]\n[[hooks.' + event + '.hooks]]\n'
                          'type = "command"\ntimeout = 8\nadditionalContextLimit = 24576\ncommand = ' +
                          json.dumps(hook(phase, active=event == "PostToolUse")))
        return {"format": "toml", "changes": {"toml_append": blocks}, "delivery": "native command hooks and MCP"}
    if runtime == "claude-code":
        hooks = {}
        for event, phase in (("SessionStart", "start"), ("SubagentStart", "start"),
                             ("UserPromptSubmit", "checkpoint"), ("PostToolUse", "checkpoint")):
            command = 'if [ -z "${GROK_HOOK_EVENT:-}" ]; then ' + hook(phase, active=event == "PostToolUse") + '; fi'
            hooks[event] = [{"hooks": [{"type": "command", "command": command, "timeout": 8}]}]
        return {"format": "json", "changes": {"hooks": hooks}, "delivery": "native command hooks; context includes own CLI connection"}
    if runtime in ("claude-desktop", "generic-mcp"):
        return {"format": "json", "changes": {"mcpServers": {"agent-inbox": {
            "command": "python3", "args": mcp_args}}}, "delivery": "MCP tools"}
    if runtime == "opencode":
        return {"format": "json", "changes": {"mcp": {"agent-inbox": {
            "type": "local", "command": ["python3"] + mcp_args, "enabled": True, "timeout": 5000}}},
                "delivery": "native MCP tools; runtime has no verified prompt hook"}
    if runtime == "grok":
        return {"format": "toml", "changes": {"toml_append": [
            '[mcp_servers.agent-inbox]\ncommand = "python3"\nargs = ' + json.dumps(mcp_args) + '\nenabled = true']},
                "delivery": "native MCP stdio; runtime discards hook context"}
    raise ValueError("No verified native configuration for " + runtime)


def rollout_plan(inventory, package_root_by_host):
    result = []
    for host in inventory:
        machine = host["machine"]
        base = Path(host["home"])
        package = package_root_by_host[machine]
        seen = set()
        for entry in host["configs"]:
            runtime, target = entry["runtime"], entry["path"]
            # A discovered profile directory is not proof of an enrolled seat.
            if runtime == "codex" and entry["sha256"] is None:
                continue
            if runtime == "grok":
                # MCP belongs in config.toml, not in another team's memory hook.
                target = str(base / ".grok/config.toml")
            if target in seen:
                continue
            seen.add(target)
            import hashlib
            principal = machine + "-" + runtime + "-" + hashlib.sha256(target.encode()).hexdigest()[:10]
            config = base / ".local/state/agent-inbox/clients" / (principal + ".json")
            result.append({"machine": machine, "runtime": runtime, "target": target,
                           "principal": principal, "client_config": str(config),
                           "baseline_sha256": entry["sha256"] if target == entry["path"] else None,
                           "baseline_state": "observed" if target == entry["path"] else "read at staging",
                           "package_root": package, "state": "PREPARED_NOT_APPLIED",
                           **additions(runtime, package, config, machine)})
    return result
