"""Command-line and stdio adapters for the BORG coordination Inbox."""

from __future__ import annotations

import argparse
import hashlib
import json
import signal
from pathlib import Path
import subprocess
import sys
from typing import Any, Iterable, Mapping, TextIO

from .client import ClientError, InboxClient
from .policy import read_policy
from .service import (
    BROWSER_OPERATIONS,
    DEFAULT_ENDPOINT,
    DEFAULT_OWNER_ACTOR,
    DEFAULT_PORT,
    HubError,
    HubService,
    MAX_REQUEST_BYTES,
    enroll_agent,
    initialize_state,
    is_browser_operation,
    load_service_config,
)


MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_TOOL_OPERATIONS = (
    "agents.register",
    "agents.list",
    "agents.heartbeat",
    "messages.send",
    "messages.poll",
    "messages.ack",
    "messages.list",
    "messages.get",
    "discoveries.publish",
    "discoveries.search",
    "grants.issue",
    "grants.revoke",
    "grants.list",
    "grants.get",
    "credentials.register",
    "authorize",
    "assignments.assign",
    "assignments.reassign",
    "assignments.list",
    "owner.snapshot",
    "fleet.context",
    "estate.read",
)


_MCP_CONTINUITY_GUIDANCE = (
    "Information, results and side requests preserve current work. Reconcile current_assignment and later_instruction_refs before older work. "
    "Verified instructions carry scoped authority; assignments.assign/reassign alone transfer ownership after a checkpoint, without human approval. "
    "Acknowledge after incorporation with the current lease; resolution is delivery completion, not work acceptance. "
    "Return completion to the verified immediate native parent; never infer an owner from a nickname."
)


def _mcp_instructions(policy: Mapping[str, Any]) -> str:
    # Some clients append initialize.instructions to every tool description.
    # The sealed body stays available through inbox_policy and native context.
    policy_ref = (
        f"Policy {policy['expected_version']} SHA256 {policy['expected_sha256']}: {policy['state']}. "
        "If this exact policy is not already in context, read inbox_policy once and apply its verified body. "
        "If unavailable or mismatched, continue under supplied rules and report the gap. "
    )
    return (
        "Use your native client_config on Inbox calls; obtain one with inbox_checkpoint if absent. "
        + policy_ref + _MCP_CONTINUITY_GUIDANCE
        + " Poll with reconcile=true at work checkpoints. inbox_fleet_context (fleet.context) is passive, on demand, preserve_current. "
        "Reuse request_id only for an identical retry."
    )


def _operation_schema(operation: str) -> dict[str, Any]:
    text = {"type": "string", "minLength": 1}
    strings = {"type": "array", "items": text}
    page = {"limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "cursor": {"type": "string", "description": "Opaque page.next_cursor from the same query and actor."}}
    assignment = {"work_id": text, "assignee": text, "scope": text, "summary": text,
                  "grant_id": text}
    contracts = {
        "messages.send": ({
            "to": {"type": "array", "items": text, "minItems": 1, "uniqueItems": True},
            "kind": {"enum": ["information", "instruction", "request", "result"]},
            "subject": text, "body": text, "scope": text, "work_id": text,
            "reply_to": text, "artifacts": strings, "expires_at": text,
            "authority_grant_id": text,
            "to_current_owner": {"type": "boolean"},
            "expected_assignment_version": {"type": "integer", "minimum": 1},
            "supersedes": strings,
        }, ["kind", "subject", "body", "scope"]),
        "messages.poll": ({
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "lease_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 86400,
                              "description": "Effective lease duration; low-level default is 60 seconds."},
            "reconcile": {"type": "boolean", "default": True},
            "active_work": {"type": "object", "properties": {
                "work_id": text, "assignment_version": {"type": "integer", "minimum": 1}},
                "additionalProperties": False},
        }, []),
        "messages.ack": ({
            "message_id": text, "state": {"enum": ["acknowledged", "resolved"]},
            "lease_id": {"type": "string", "description": "Required while leased; omit after acknowledgment."},
            "receipt_ref": text, "work_outcome_ref": text,
        }, ["message_id", "state"]),
        "messages.get": ({"message_id": text}, ["message_id"]),
        "messages.list": ({
            **page, "state": {"enum": ["queued", "leased", "acknowledged", "resolved", "expired", "rejected", "superseded"]},
            "work_id": text, "scope": text,
            "view": {"const": "pending_metadata", "description": "Own pending summary: pass only view and limit."},
        }, []),
        "assignments.assign": (assignment, ["work_id", "assignee", "scope", "summary"]),
        "assignments.reassign": ({**assignment, "expected_version": {"type": "integer", "minimum": 1}},
                                 ["work_id", "assignee", "scope", "summary", "expected_version"]),
        "assignments.list": ({**page, "work_id": text, "scope": text}, []),
        "discoveries.publish": ({"title": text, "body": text, "scope": text, "topics": strings,
                                  "artifacts": strings, "work_id": text, "expires_at": text},
                                 ["title", "body", "scope"]),
        "discoveries.search": ({"query": {"type": "string"}, "scope": text,
                                 "topics": strings, "limit": page["limit"]}, []),
        "agents.register": ({"agent_id": text, "runtime": text, "machine": text, "display_name": text,
                             "instance_id": text, "session_id": text, "thread_id": text,
                             "capabilities": {"type": "array"}}, ["agent_id", "runtime", "machine"]),
        "agents.heartbeat": ({"instance_id": text, "status": text}, []),
        "agents.list": ({**page, "runtime": text, "machine": text, "status": text,
                         "agent_id": {**text, "description": "Exact registry lookup; independent of the page window."}}, []),
    }
    properties, required = contracts.get(operation, ({}, []))
    schema = {
        "type": "object",
        "properties": {
            "client_config": {"type": "string", "description": "Your own native session connection."},
            "request_id": {"type": "string", "description": "Stable only for an identical retry."},
            **properties,
        },
        "required": ["client_config", *required],
        "additionalProperties": True,
    }
    if operation == "messages.send":
        schema["anyOf"] = [{"required": ["to"]}, {
            "required": ["to_current_owner", "work_id", "expected_assignment_version"],
            "properties": {"to_current_owner": {"const": True}},
        }]
    return schema


def _json_line(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _write_line(output: TextIO, value: Any) -> None:
    output.write(_json_line(value) + "\n")
    output.flush()


def _safe_error(exc: BaseException, *, request_id: str | None = None) -> dict[str, Any]:
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None)
    status = getattr(exc, "status", None)
    if not isinstance(code, str) or not code:
        code = "internal_error" if not isinstance(exc, (ClientError, HubError)) else "request_error"
    if not isinstance(message, str) or not message:
        message = "Internal service error" if code == "internal_error" else "Request rejected"
    error: dict[str, Any] = {"code": code, "message": message}
    if isinstance(status, int) and status:
        error["status"] = status
    if request_id:
        error["request_id"] = request_id
    return error


def _read_params(path: str, input_stream: TextIO = sys.stdin) -> dict[str, Any]:
    if path == "-":
        text = input_stream.read()
    else:
        try:
            text = Path(path).expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ClientError("params_unavailable", "Unable to read params file") from exc
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ClientError("invalid_params", "Params file must contain valid JSON") from exc
    if not isinstance(value, dict):
        raise ClientError("invalid_params", "Params file must contain a JSON object")
    return value


def _resolve_enroll_state(args: argparse.Namespace) -> Path:
    if args.state_dir:
        return Path(args.state_dir).expanduser()
    if args.config:
        config = load_service_config(args.config)
        configured = config.get("state_dir")
        if isinstance(configured, str) and configured:
            return Path(configured).expanduser()
        return Path(args.config).expanduser().parent
    return Path(args.credential_file).expanduser().parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="borg-inbox")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="initialize a local inbox service")
    init_parser.add_argument("--state-dir", required=True)
    init_parser.add_argument("--owner-actor", default=DEFAULT_OWNER_ACTOR)
    init_parser.add_argument("--approval-ref", default="local-init")
    init_parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)

    enroll_parser = subparsers.add_parser("enroll", help="locally enroll an agent")
    enroll_parser.add_argument("--state-dir")
    enroll_parser.add_argument("--config")
    enroll_parser.add_argument("--agent", required=True)
    enroll_parser.add_argument("--runtime", required=True)
    enroll_parser.add_argument("--machine", required=True)
    enroll_parser.add_argument("--credential-file", required=True)
    enroll_parser.add_argument("--display-name")
    enroll_parser.add_argument("--capability", action="append", dest="capabilities")
    enroll_parser.add_argument("--owner-actor")

    call_parser = subparsers.add_parser("call", help="call one authenticated operation")
    call_parser.add_argument("--config", required=True)
    call_parser.add_argument("--operation", required=True)
    call_parser.add_argument("--params-file", required=True)
    call_parser.add_argument("--request-id")
    call_parser.add_argument("--flush-attempts", type=int, default=3)

    serve_parser = subparsers.add_parser("serve", help="serve a local inbox over HTTP")
    serve_parser.add_argument("--state-dir", required=True)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve_parser.add_argument("--browser-config", help="private operator browser gateway configuration")

    stdio_parser = subparsers.add_parser("stdio", help="run JSON-lines/MCP stdio bridge")
    stdio_parser.add_argument("--config", required=True)
    stdio_parser.add_argument("--flush-attempts", type=int, default=3)

    return parser


def _tool_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "description": (
                "Inbox operation, including synchronous authenticated " + ", ".join(sorted(BROWSER_OPERATIONS))
                + ". Browser calls never queue or automatically retry. Open carries work_id/attempt_id; "
                  "subsequent calls carry lease_id/generation. Actor is derived from Bearer authentication."
            )},
            "params": {"type": "object", "additionalProperties": True},
            "request_id": {"type": "string"},
            "client_config": {"type": "string", "description": "Use the connection returned by your native inbox context or inbox_checkpoint."},
        },
        "required": ["operation", "params"],
        "additionalProperties": False,
    }


def _mcp_tools() -> list[dict[str, Any]]:
    tools = [
        {
            "name": "inbox_call",
            "description": (
                "Call an inbox operation as your own session. Pass client_config from inbox_checkpoint or native context. "
                "Prefer typed common tools; never infer a recipient. Browser calls stay synchronous and never queue or retry."
            ),
            "inputSchema": _tool_schema(),
        }
    ]
    tools.append({"name": "inbox_checkpoint", "description": (
                  "Register your stable session and collect its inbox without changing your current objective. Use returned "
                  "client_config on subsequent calls; reconcile newer instructions first."),
                  "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"},
                    "runtime": {"type": "string"}, "machine": {"type": "string"}},
                    "required": ["session_id", "runtime", "machine"], "additionalProperties": True}})
    tools.append({"name": "inbox_policy",
                  "description": "Read the owner's verified common operating rules with version and SHA256. Continue your current objective while applying them.",
                  "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}})
    tools.append({
        "name": "inbox_fleet_context",
        "description": (
            "Fetch one bounded passive fleet snapshot on demand. It requires the existing Store authority, never accepts "
            "a source path or command, and preserves the current work when unavailable."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "client_config": {"type": "string"},
                "scope": {"const": "/"},
            },
            "additionalProperties": False,
        },
    })
    descriptions = {
        "messages.send": "Send a durable work message. Use verified recipient IDs, or current-owner routing with work/version. Supersession is explicit for passive status only.",
        "messages.poll": "Lease your pending deliveries. Use reconcile=true and incorporate newer instructions first. A current lease is required for acknowledgment.",
        "messages.ack": "Record incorporation (acknowledged) or later delivery completion (resolved). Example: message_id, state='acknowledged', lease_id=current lease. Work acceptance needs its own evidence.",
        "messages.get": "Read a message and current delivery/authority evidence by its ID.",
        "messages.list": "Read without leasing. Follow page.next_cursor for full coverage. Own pending summary example: view='pending_metadata', limit=10; no other business fields.",
        "assignments.assign": "Create the first binding assignment of ownership. Use a verified assignee and explicit work, scope and summary.",
        "assignments.reassign": "Atomically transfer work using its current expected_version. Checkpoint displaced work first.",
        "assignments.list": "Read authorized work ownership. Follow page.next_cursor until complete; pages recheck current authority.",
        "agents.list": "Read the agent registry one page at a time (limit<=100, newest registration first). Follow page.next_cursor until page.has_more is false for a full census; use exact runtime/machine/status/agent_id filters for recipient resolution.",
        "discoveries.publish": "Publish a reusable finding with work/source evidence; it does not mark the work accepted.",
        "discoveries.search": "Find source-linked findings relevant to the current work; verify recalled claims.",
    }
    for operation in MCP_TOOL_OPERATIONS:
        tools.append(
            {
                "name": operation,
                "description": descriptions.get(operation, f"Call {operation} using your own native client_config."),
                "inputSchema": _operation_schema(operation),
            }
        )
    return tools


def _mcp_success(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _mcp_error(request_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _mcp_tool_result(result: Any) -> dict[str, Any]:
    try:
        text = _json_line(result)
    except (TypeError, ValueError):
        text = json.dumps({"value": str(result)}, ensure_ascii=False)
    structured = result if isinstance(result, dict) else {"value": result}
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": structured,
        "isError": False,
    }


def _mcp_tool_error(exc: BaseException) -> dict[str, Any]:
    error = _safe_error(exc)
    return {
        "content": [{"type": "text", "text": _json_line({"error": error})}],
        "isError": True,
    }


def _session_client(client: InboxClient, selected: Any) -> InboxClient:
    parent_path = getattr(client, "config_path", None)
    if parent_path is None:
        if selected is not None:
            raise ClientError("invalid_session_config", "This client has no registered session configuration root")
        return client
    actor = client.config.get("agent_id") or client.config.get("principal") or ""
    root = parent_path.parent / ".native" / hashlib.sha256(actor.encode()).hexdigest()[:16]
    if selected is None:
        if root.exists():
            raise ClientError("session_config_required", "Pass client_config from your native context or inbox_checkpoint. Parent work must explicitly select the parent configuration.")
        return client
    if not isinstance(selected, str):
        raise ClientError("invalid_session_config", "client_config must be a path")
    path = Path(selected).expanduser().resolve()
    if path == parent_path:
        return client
    try:
        path.relative_to(root.resolve())
        if path.stat().st_size > MAX_REQUEST_BYTES:
            raise ValueError("oversize")
        config = json.loads(path.read_text())
        credential = Path(config["credential_file"]).resolve()
        credential.relative_to(root.resolve())
        if config.get("endpoint", "").rstrip("/") != client.endpoint.rstrip("/"):
            raise ValueError("foreign endpoint")
        # The selected child only controls its own credential and adjacent outbox.
        return InboxClient(client.endpoint, credential)
    except (ValueError, OSError, KeyError, TypeError):
        raise ClientError("invalid_session_config", "Only this parent's issued session connections may be selected")


def _handle_mcp(request: Mapping[str, Any], client: InboxClient) -> dict[str, Any] | None:
    request_id = request.get("id")
    if request.get("jsonrpc") != "2.0":
        return _mcp_error(request_id, -32600, "JSON-RPC 2.0 is required")
    method = request.get("method")
    if not isinstance(method, str):
        return _mcp_error(request_id, -32600, "Invalid JSON-RPC request")
    params = request.get("params", {})
    if not isinstance(params, dict):
        return _mcp_error(request_id, -32602, "Params must be an object")

    if method == "notifications/initialized" or method == "notifications/cancelled":
        return None
    if method == "ping":
        return None if "id" not in request else _mcp_success(request_id, {})
    if method == "initialize":
        if "id" not in request:
            return None
        policy = read_policy()
        return _mcp_success(
            request_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "borg-coordination-inbox", "version": "1"},
                "instructions": _mcp_instructions(policy),
            },
        )
    if method == "tools/list":
        if "id" not in request:
            return None
        return _mcp_success(request_id, {"tools": _mcp_tools()})
    if method == "tools/call":
        if "id" not in request:
            return None
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return _mcp_error(request_id, -32602, "tools/call requires name and object arguments")
        if name == "inbox_policy":
            if arguments:
                return _mcp_error(request_id, -32602, "inbox_policy takes no arguments")
            return _mcp_success(request_id, _mcp_tool_result(read_policy(include_body=True)))
        if name == "inbox_checkpoint":
            try:
                if not getattr(client, "config_path", None) or any(not isinstance(arguments.get(key), str) or not arguments[key] for key in ("session_id", "runtime", "machine")):
                    raise ClientError("invalid_checkpoint", "A configured client, session_id, runtime and machine are required")
                # Share the installed hook's deadline, including config reads
                # and outbox locks; an MCP session must not bypass that bound.
                completed = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve().parents[1] / "bin/inbox-native"),
                     "--config", str(client.config_path), "--runtime", arguments["runtime"],
                     "--machine", arguments["machine"], "--phase", "checkpoint"],
                    input=_json_line(arguments), text=True, capture_output=True, timeout=8)
                if completed.returncode or completed.stderr or len(completed.stdout.encode()) > MAX_REQUEST_BYTES:
                    raise ValueError("Native checkpoint failed")
                envelope = json.loads(completed.stdout)
                context = envelope.get("hookSpecificOutput", {}).get("additionalContext") or envelope.get("additionalContext")
                result = json.loads(context)
                if not isinstance(result, dict) or not result:
                    raise ValueError("Native checkpoint unavailable")
                return _mcp_success(request_id, _mcp_tool_result(result))
            except Exception:
                return _mcp_success(request_id, _mcp_tool_error(ClientError("checkpoint_unavailable", "Inbox checkpoint is unavailable; continue your existing task and retry at a normal checkpoint")))
        if name == "inbox_fleet_context":
            allowed = {"client_config", "scope"}
            if any(key not in allowed for key in arguments) or arguments.get("scope", "/") != "/":
                return _mcp_error(request_id, -32602, "inbox_fleet_context accepts only scope / and client_config")
            operation = "fleet.context"
            business_params = {"scope": "/"}
            business_request_id = None
        elif name == "inbox_call":
            operation = arguments.get("operation")
            business_params = arguments.get("params", {})
            business_request_id = arguments.get("request_id")
            if is_browser_operation(operation) and set(arguments) - {"operation", "params", "request_id", "client_config"}:
                return _mcp_success(request_id, _mcp_tool_error(ClientError("invalid_request", "Unsupported browser envelope fields")))
            if not isinstance(operation, str) or not isinstance(business_params, dict):
                return _mcp_success(request_id, _mcp_tool_error(ClientError("invalid_params", "operation and params are required")))
        elif name in MCP_TOOL_OPERATIONS:
            operation = name
            business_request_id = arguments.get("request_id")
            business_params = {
                key: value for key, value in arguments.items() if key not in ("request_id", "client_config")
            }
        else:
            return _mcp_error(request_id, -32602, "Unknown inbox tool")
        try:
            selected_client = _session_client(client, arguments.get("client_config"))
            result = selected_client.call(operation, business_params, business_request_id)
            return _mcp_success(request_id, _mcp_tool_result(result))
        except ClientError as exc:
            return _mcp_success(request_id, _mcp_tool_error(exc))
        except Exception:
            return _mcp_success(request_id, _mcp_tool_error(ClientError("internal_error", "Internal service error")))
    if "id" not in request:
        return None
    return _mcp_error(request_id, -32601, "Method not found")


def _handle_direct(request: Mapping[str, Any], client: InboxClient) -> dict[str, Any]:
    operation = request.get("operation")
    params = request.get("params", {})
    request_id = request.get("request_id")
    try:
        if is_browser_operation(operation) and set(request) - {"operation", "params", "request_id"}:
            raise ClientError("invalid_request", "Unsupported browser envelope fields")
        result = client.call(operation, params, request_id)  # type: ignore[arg-type]
        response: dict[str, Any] = {"ok": True, "result": result}
        if isinstance(request_id, str) and request_id:
            response["request_id"] = request_id
        return response
    except ClientError as exc:
        return {"ok": False, "error": _safe_error(exc, request_id=request_id if isinstance(request_id, str) else None)}
    except Exception:
        return {"ok": False, "error": {"code": "internal_error", "message": "Internal service error"}}


def run_stdio(
    client: InboxClient,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> int:
    """Serve newline-delimited direct requests and the small MCP protocol."""

    input_stream = sys.stdin if input_stream is None else input_stream
    output_stream = sys.stdout if output_stream is None else output_stream
    for line in input_stream:
        if not line.strip():
            continue
        if len(line.encode("utf-8")) > MAX_REQUEST_BYTES:
            _write_line(output_stream, _mcp_error(None, -32600, "Request exceeds 128 KiB"))
            continue
        try:
            request = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            _write_line(output_stream, _mcp_error(None, -32700, "Invalid JSON"))
            continue
        if not isinstance(request, dict):
            _write_line(output_stream, _mcp_error(None, -32600, "Invalid JSON-RPC request"))
            continue
        if "method" in request or request.get("jsonrpc") == "2.0":
            response = _handle_mcp(request, client)
            if response is not None:
                _write_line(output_stream, response)
        elif "operation" in request:
            _write_line(output_stream, _handle_direct(request, client))
        else:
            _write_line(output_stream, {"ok": False, "error": {"code": "invalid_request", "message": "operation is required"}})
    return 0


def _run_command(args: argparse.Namespace) -> int:
    if args.command == "init":
        result = initialize_state(
            args.state_dir,
            owner_actor=args.owner_actor,
            approval_ref=args.approval_ref,
            endpoint=args.endpoint,
        )
        _write_line(sys.stdout, result)
        return 0

    if args.command == "enroll":
        state_dir = _resolve_enroll_state(args)
        result = enroll_agent(
            state_dir,
            args.agent,
            args.runtime,
            args.machine,
            args.credential_file,
            display_name=args.display_name,
            capabilities=args.capabilities,
            owner_actor=args.owner_actor,
        )
        _write_line(sys.stdout, result)
        return 0

    if args.command == "call":
        params = _read_params(args.params_file)
        client = InboxClient.from_config(
            args.config,
            max_flush_attempts=args.flush_attempts,
        )
        result = client.call(args.operation, params, args.request_id)
        _write_line(sys.stdout, result)
        return 0

    if args.command == "stdio":
        client = InboxClient.from_config(
            args.config,
            max_flush_attempts=args.flush_attempts,
        )
        return run_stdio(client)

    if args.command == "serve":
        factory = None
        if args.browser_config:
            from fleet_browser.integration import gateway_factory
            factory = gateway_factory(args.browser_config)
        service = HubService(state_dir=args.state_dir, browser_gateway_factory=factory)
        server = service.make_server(args.host, args.port)
        previous_term = None
        if factory is not None:
            def stop_browser_service(signum, frame):
                raise KeyboardInterrupt
            previous_term = signal.signal(signal.SIGTERM, stop_browser_service)
        _write_line(
            sys.stdout,
            {
                "status": "serving",
                "host": server.server_address[0],
                "port": server.server_address[1],
            },
        )
        try:
            server.serve_forever()
        finally:
            try:
                service.close_browser_gateway()
            finally:
                server.server_close()
                if previous_term is not None:
                    signal.signal(signal.SIGTERM, previous_term)
        return 0

    raise ClientError("invalid_command", "Unknown command")


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return _run_command(args)
    except (ClientError, HubError, OSError, ValueError) as exc:
        print(_json_line({"ok": False, "error": _safe_error(exc)}), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "MCP_PROTOCOL_VERSION",
    "MCP_TOOL_OPERATIONS",
    "build_parser",
    "main",
    "run_stdio",
]
