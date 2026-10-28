"""Advisory plans: models propose future actions; executor evidence owns progress."""

import copy

from .schemas import TOOL_NAMES
from .trace import event

ACTIVE = {"pending", "running"}


def sync_pending(state):
    state["pending_plan"] = [
        {key: item[key] for key in ("todo_id", "tool_name", "status")}
        for item in state["todo_list"]
        if item["status"] in ACTIVE
    ]


def new_item(state, name):
    item = {
        "todo_id": f"todo-{len(state['todo_list']) + 1}",
        "tool_name": name,
        "status": "pending",
        "call_id": None,
        "created_round": state["decision_rounds"],
    }
    state["todo_list"].append(item)
    return item


def revision(state, reason, note, call_id=None):
    row = {
        "revision": len(state["plan_revisions"]) + 1,
        "round": state["decision_rounds"],
        "reason": reason,
        "note": note,
        "source_call_id": call_id,
        "source_observation": copy.deepcopy(state.get("todo_replan")),
        "items": copy.deepcopy(state["todo_list"]),
    }
    state["plan_revisions"].append(row)
    event(state, "plan_revised", **copy.deepcopy(row))


def initialize(state):
    state.update(todo_list=[], plan_revisions=[], todo_replan=None)
    for name in TOOL_NAMES:
        new_item(state, name)
    sync_pending(state)
    revision(state, "INITIAL_PLAN", "初始计划；完成状态由实际工具结果确认")


def resume(state, names):
    state.setdefault("todo_list", [])
    state.setdefault("plan_revisions", [])
    previous = state["resume_history"][-1]
    failed = next(
        (row for row in reversed(state["steps"]) if row["status"] in ("failed", "blocked")),
        None,
    )
    state["todo_replan"] = {
        "reason": previous["previous_reason"],
        "source_call_id": failed["call_id"] if failed else None,
        "round": state["decision_rounds"],
    }
    for name in names:
        new_item(state, name)
    sync_pending(state)
    revision(state, "TASK_RESUMED", "复用已确认结果继续规划，历史尝试和费用保留")


def select_item(state, call):
    """Bind an attempt to a pending item, never reopen a historical attempt."""
    items = state["todo_list"]
    existing = next((item for item in items if item["call_id"] == call.id), None)
    if existing:
        return existing, False
    pending = [item for item in items if item["status"] == "pending"]
    selected = next((item for item in pending if item["tool_name"] == call.name), None)
    changed = selected is None or (pending and pending[0] is not selected)
    selected = selected or new_item(state, call.name)
    selected["call_id"] = call.id
    # Keep historical rows, then put the chosen action before the pending suffix.
    state["todo_list"] = (
        [item for item in items if item is not selected and item["status"] != "pending"]
        + [selected]
        + [item for item in items if item is not selected and item["status"] == "pending"]
    )
    return selected, bool(changed)


def start(state, call, args):
    item, changed = select_item(state, call)
    item.update(status="running", decision_note=args.decision_note)
    proposal = args.remaining_steps
    if proposal is not None:
        pending = [row for row in state["todo_list"] if row["status"] == "pending"]
        if proposal != [row["tool_name"] for row in pending]:
            changed = True
            future = []
            for name in proposal:
                matched = next((row for row in pending if row["tool_name"] == name), None)
                if matched:
                    pending.remove(matched)
                future.append(matched or new_item(state, name))
            for removed in pending:
                removed.update(status="skipped", reason="PLAN_REVISED")
            state["todo_list"] = [
                row for row in state["todo_list"] if row["status"] != "pending"
            ] + future
    if changed or state.get("todo_replan"):
        revision(
            state,
            "MODEL_REPLAN" if state.get("todo_replan") else "MODEL_PLAN_UPDATE",
            args.decision_note,
            call.id,
        )
        state["todo_replan"] = None
    sync_pending(state)
    event(state, "todo_updated", item=copy.deepcopy(item))
    return item["todo_id"]


def observe(state, call, status, data, error, replan):
    # A skipped batch member has not been chosen again by the model. Keep its
    # pending intention intact until a later decision or task finalization.
    if status == "skipped" or call.name not in TOOL_NAMES:
        return None
    item, changed = select_item(state, call)
    item.update(status=status, result_round=state["decision_rounds"])
    if error:
        item["error_code"] = error["code"]
    if data and isinstance(data, dict):
        for key in ("plan_ref", "estimate_id", "within_budget"):
            if key in data:
                item[key] = data[key]
    if changed:
        revision(state, "ACTION_BLOCKED", "未执行的动作被门禁阻止", call.id)
    if replan:
        code = (error or {}).get("code")
        if not code:
            code = {
                "search_materials": "MATERIALS_INSUFFICIENT",
                "estimate_cost": "PLAN_OVER_BUDGET"
                if not data.get("within_budget")
                else "DEADLINE_INFEASIBLE",
            }.get(call.name, "REPLAN_REQUIRED")
        state["todo_replan"] = {
            "reason": code,
            "source_call_id": call.id,
            "round": state["decision_rounds"],
        }
    sync_pending(state)
    event(state, "todo_updated", item=copy.deepcopy(item), replan=state.get("todo_replan"))
    return item["todo_id"]


def close(state, reason):
    if "todo_list" not in state:  # Legacy snapshots are not retroactively invented.
        for item in state["pending_plan"]:
            item["status"] = "skipped"
        return
    for item in state["todo_list"]:
        if item["status"] == "pending":
            item.update(status="skipped", reason=reason)
        elif item["status"] == "running":
            item.update(status="failed", reason=reason, outcome_unknown=True)
    sync_pending(state)
    revision(state, "TASK_FINISHED", reason)
    state["todo_replan"] = None


def context(state):
    return {
        "todo_list": copy.deepcopy(state["todo_list"]),
        "plan_revision": len(state["plan_revisions"]),
        "plan_changes": copy.deepcopy(
            [
                {key: row[key] for key in ("revision", "reason", "note", "source_observation")}
                for row in state["plan_revisions"]
            ]
        ),
        "todo_replan": copy.deepcopy(state.get("todo_replan")),
    }
