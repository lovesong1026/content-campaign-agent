import copy
from decimal import Decimal

from content_campaign_agent.schemas import ToolCall, dumps


def call(name, **args):
    return ToolCall("extra-" + name, name, dumps({"decision_note": "边界测试", **args}))


async def ready(make_agent, task):
    agent = make_agent()
    await agent.run_task(task)
    state = await agent.storage.load(task["task_id"])
    state["status"] = "running"
    state["saved"] = None
    return agent, state


async def test_stale_estimate_and_latest_ledger(make_agent, task):
    agent, state = await ready(make_agent, task)
    old = state["current_estimate_id"]
    generation = call(
        "generate_script",
        audience="消费者",
        channel="短视频",
        items=[{"asset_ids": ["basic-spring"], "production_tier": "economy"}],
    )
    await agent.executor.execute(state, generation)
    before = state["tool_calls"]
    blocked = await agent.executor.execute(
        state, call("save_plan", plan_ref=state["current_plan_ref"], estimate_id=old)
    )
    assert blocked["error"]["code"] == "STALE_ESTIMATE"
    assert state["tool_calls"] == before
    assert blocked["fee"] == "0.00"


async def test_save_uses_latest_cost_and_gates_before_charge(make_agent, task):
    agent, state = await ready(make_agent, task)
    state["input"]["budget"] = "5.00"
    state["tool_cost"] = "5.00"
    result = await agent.executor.execute(
        state,
        call(
            "save_plan",
            plan_ref=state["current_plan_ref"],
            estimate_id=state["current_estimate_id"],
        ),
    )
    assert result["error"]["code"] == "PLAN_OVER_BUDGET"
    assert state["tool_cost"] == "5.00"
    assert result["duration_seconds"] == 0


async def test_asset_dependency_and_nonexistent_plan(make_agent, task):
    agent, state = await ready(make_agent, task)
    initial = copy.deepcopy(state["ledger"])
    result = await agent.executor.execute(
        state,
        call(
            "generate_script",
            audience="用户",
            channel="平台",
            items=[{"asset_ids": ["invented"], "production_tier": "economy"}],
        ),
    )
    assert result["status"] == "blocked"
    assert state["ledger"] == initial
    result = await agent.executor.execute(state, call("estimate_cost", plan_ref="invented"))
    assert result["error"]["code"] == "PLAN_NOT_FOUND"


async def test_ledger_is_sum_of_actual_calls(make_agent, task):
    agent = make_agent()
    result = await agent.run_task(task)
    assert sum(Decimal(row["fee"]) for row in result["ledger"]) == Decimal(
        result["costs"]["tool_cost"]
    )
    assert sum(row["duration_seconds"] for row in result["ledger"]) == result["elapsed_seconds"]


async def test_deadline_gate_does_not_consume_fee_or_time(make_agent, task):
    agent, state = await ready(make_agent, task)
    state["input"]["deadline"] = state["simulated_now"]
    before = (state["tool_calls"], state["tool_cost"], state["elapsed_seconds"])
    result = await agent.executor.execute(state, call("search_materials", query="春季"))
    assert result["error"]["code"] == "DEADLINE_BLOCKED"
    assert before == (state["tool_calls"], state["tool_cost"], state["elapsed_seconds"])
