"""Optional estate-model contract for an owner-supplied integration."""

OPERATIONS = {
    "context": set(),
    "capabilities": set(),
    "topology": {"limit", "offset", "kind"},
    "entity": {"id"},
    "history": {"entity", "metrics", "start", "end", "step"},
    "changes": {"since", "limit"},
}


def validate_params(params, error):
    action = params.get("action")
    if not isinstance(action, str) or action not in OPERATIONS:
        raise error("invalid_request", "Unknown estate action", 400)
    if set(params) - (OPERATIONS[action] | {"action"}):
        raise error("invalid_request", "Unexpected estate parameter", 400)
    for key, value in params.items():
        if key in {"limit", "offset", "step"}:
            bound = {
                "limit": (1, 200 if action == "topology" else 100),
                "offset": (0, 100000),
                "step": (60, 86400),
            }[key]
            if type(value) is not int or not bound[0] <= value <= bound[1]:
                raise error("invalid_request", "Estate numeric bound exceeded", 400)
        elif key == "metrics":
            if (
                not isinstance(value, list)
                or not 1 <= len(value) <= 4
                or any(not isinstance(item, str) or not 1 <= len(item) <= 100 for item in value)
            ):
                raise error("invalid_request", "Request one to four named metrics", 400)
        elif key == "kind" and value is None:
            pass
        elif not isinstance(value, str) or not 1 <= len(value) <= 256:
            raise error("invalid_request", "Invalid estate text parameter", 400)


def shared_reader(_ownership_reader):
    """Estate integration is absent unless the embedding owner injects a factory."""

    raise RuntimeError("estate_integration_not_configured")
