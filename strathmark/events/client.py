"""Local trusted process protocol; callers own OS access and human authentication."""

from .contracts import EventError, Principal, capabilities, closed


def dispatch(ops, principal, request):
    closed(
        request, {"operation"}, {"event_id", "request_id", "expected_revision", "editor", "payload"}
    )
    operation = request["operation"]
    if operation == "capabilities":
        return capabilities()
    if operation == "create":
        return ops.create(principal, request["request_id"], request["payload"], request["editor"])
    if operation == "read":
        return ops.read(principal, request["event_id"])
    if operation == "export":
        return ops.export(principal, request["event_id"])
    return ops.execute(
        principal,
        request["event_id"],
        request["request_id"],
        request["expected_revision"],
        request["editor"],
        operation,
        request.get("payload", {}),
    )


def local_request(ops, envelope):
    closed(envelope, {"principal", "request"})
    closed(envelope["principal"], {"actor_id", "shows", "role"})
    raw = envelope["principal"]
    if not isinstance(raw["shows"], list):
        raise EventError("invalid_show_scope")
    principal = Principal(raw["actor_id"], frozenset(raw["shows"]), raw["role"])
    return dispatch(ops, principal, envelope["request"])
