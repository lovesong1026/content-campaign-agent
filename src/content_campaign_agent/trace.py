from datetime import UTC, datetime


def event(state, kind, **data):
    entry = {
        "event_id": f"{state['run_id']}:{len(state['events']) + 1}",
        "sequence": len(state["events"]) + 1,
        "timestamp": datetime.now(UTC).isoformat(),
        "round": state["decision_rounds"],
        "kind": kind,
        **data,
    }
    state["events"].append(entry)
    return entry
