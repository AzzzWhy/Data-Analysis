"""Dependency-free execution envelope checks shared by Agent and GUI."""


def tool_result_failed(payload, *, _depth: int = 0) -> bool:
    """Inspect execution envelopes, never arbitrary table rows or statistics.

    A dataset may legitimately have columns named ``error``, ``ok`` or
    ``success``. Only known result wrappers are traversed; numeric error
    measurements, warnings and an empty error field do not denote failure.
    Callers separately decide whether a non-dict result is a valid contract.
    """
    if not isinstance(payload, dict) or _depth > 8:
        return False
    if payload.get("success") is False or payload.get("ok") is False or payload.get("isError") is True:
        return True
    error = payload.get("error")
    if ((isinstance(error, str) and bool(error.strip())) or error is True
            or (isinstance(error, dict) and any(error.get(key) for key in ("message", "code", "type")))
            or (isinstance(error, list) and any(isinstance(item, str) and item.strip() for item in error))):
        return True
    for key in ("result", "detail"):
        if tool_result_failed(payload.get(key), _depth=_depth + 1):
            return True
    for key in ("results", "steps", "jobs"):
        items = payload.get(key)
        if isinstance(items, list) and any(tool_result_failed(item, _depth=_depth + 1) for item in items):
            return True
    # A queue summary contains job envelopes. An analytics summary contains
    # user-named columns, so do not recursively inspect the summary itself.
    summary = payload.get("summary")
    if isinstance(summary, dict) and isinstance(summary.get("jobs"), list):
        return any(tool_result_failed(item, _depth=_depth + 1) for item in summary["jobs"])
    return False
