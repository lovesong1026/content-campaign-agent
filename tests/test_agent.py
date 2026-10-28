import asyncio
import json
from decimal import Decimal

import pytest

from content_campaign_agent.model import OfflineMockClient
from content_campaign_agent.schemas import Decision, ModelError, Scenario, ToolCall, dumps


async def test_normal_and_repeat_survive_restart(make_agent, task):
    agent = make_agent()
    result = await agent.run_task(task)
    assert result["status"] == "succeeded"
    assert result["costs"]["total_cost"] == "5.00"
    assert result["tool_calls"] == 4
    assert result["elapsed_seconds"] == 540
    saved = agent.storage.directory("test") / result["saved"]["path"]
    assert json.loads(saved.read_text())["plan"]["items"][0]["body"]
    restarted = make_agent()
    repeated = await restarted.run_task(task)
    assert repeated["reused"] and repeated["run_id"] == result["run_id"]
    assert repeated["ledger"] == result["ledger"]
    assert len(list(saved.parent.glob("*.json"))) == 1
    conflict = await restarted.run_task({**task, "budget": "21"})
    assert conflict["reason"] == "TASK_INPUT_CONFLICT"
    independent = await restarted.run_task({**task, "task_id": "different"})
    assert independent["run_id"] != result["run_id"]


@pytest.mark.parametrize("missing", ["theme", "budget", "deadline"])
async def test_missing_fields_without_credentials(make_agent, task, missing):
    task.pop(missing)
    result = await make_agent().run_task(task)
    assert result["status"] == "needs_input"
    assert missing in result["input_errors"]
    assert result["decision_rounds"] == result["tool_calls"] == 0
    assert result["costs"]["total_cost"] is None


@pytest.mark.parametrize("value", ["-1", "0.001", "NaN", "Infinity", True])
async def test_invalid_budget(make_agent, task, value):
    result = await make_agent().run_task({**task, "budget": value})
    assert result["status"] == "needs_input"


async def test_overbudget_replans_preserving_old_artifacts(make_agent, task):
    agent = make_agent(OfflineMockClient("premium_first"))
    result = await agent.run_task({**task, "budget": "10"})
    assert result["status"] == "succeeded"
    assert result["costs"]["tool_cost"] == "7.00"
    assert result["costs"]["total_cost"] == "8.00"
    state = await agent.storage.load(task["task_id"])
    assert len(state["plans"]) == len(state["estimates"]) == 2
    estimates = list(state["estimates"].values())
    assert estimates[0]["total_at_estimate"] == "12.00"
    assert not estimates[0]["within_budget"]
    assert all(step["status"] == "succeeded" for step in result["steps"])


async def test_tool_failure_preserves_success_and_charges(make_agent, task):
    scenario = Scenario(fixed_start="2030-01-01T10:00:00+08:00", failures={"generate_script": [1]})
    result = await make_agent(scenario=scenario).run_task(task)
    assert result["status"] == "succeeded"
    assert result["steps"][0]["status"] == "succeeded"
    assert result["steps"][1]["status"] == "failed"
    assert result["steps"][1]["observation"]["error"]["code"] == "INJECTED_TOOL_FAILURE"
    assert result["costs"]["tool_cost"] == "6.00"
    assert result["elapsed_seconds"] == 840


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"budget": "3"}, "BUDGET_INFEASIBLE"),
        ({"deadline": "2030-01-01T10:08:00+08:00"}, "DEADLINE_INFEASIBLE"),
        ({"deadline": "2030-01-01T10:00:00+08:00"}, "DEADLINE_EXCEEDED"),
    ],
)
async def test_infeasible(make_agent, task, change, reason):
    result = await make_agent().run_task({**task, **change})
    assert result["status"] == "incomplete" and result["reason"] == reason
    assert result["saved"] is None


async def test_no_materials(make_agent, task):
    result = await make_agent(
        scenario=Scenario(empty_materials=True, fixed_start="2030-01-01T10:00:00+08:00")
    ).run_task(task)
    assert result["reason"] == "MATERIALS_INSUFFICIENT"
    assert result["tool_calls"] == 1


async def test_execution_limits(make_agent, task):
    result = await make_agent(OfflineMockClient("loop")).run_task({**task, "budget": "100"})
    assert result["reason"] == "EXECUTION_LIMIT_REACHED"
    assert result["decision_rounds"] == result["tool_calls"] == 12


async def test_recovery_retains_started_charge(make_agent, task):
    agent = make_agent()
    await agent.run_task(task)
    state = await agent.storage.load("test")
    state["status"] = "running"
    state["steps"][-1]["status"] = "running"
    await agent.storage.save(state)
    restarted = make_agent()
    with restarted.storage.writer():
        await restarted.start()
    result = await restarted.run_task(task)
    assert result["reason"] == "EXECUTION_INTERRUPTED"
    assert result["costs"]["tool_cost"] == "4.00"
    assert result["steps"][-1]["outcome_unknown"]


class FaultModel(OfflineMockClient):
    async def decide(self, context, messages, record):
        raise ModelError("MODEL_TIMEOUT", "测试超时")


async def test_model_failure_not_switched_to_mock(make_agent, task):
    result = await make_agent(FaultModel()).run_task(task)
    assert result["reason"] == "MODEL_TIMEOUT"
    assert result["tool_calls"] == 0


class StopModel(OfflineMockClient):
    async def decide(self, context, messages, record):
        return Decision(content="已完成，请相信我。")


async def test_model_cannot_claim_success_without_save(make_agent, task):
    result = await make_agent(StopModel()).run_task(task)
    assert result["status"] == "incomplete"
    assert result["saved"] is None


class InvalidModel(OfflineMockClient):
    async def decide(self, context, messages, record):
        return Decision([ToolCall(f"bad-{context['remaining_decisions']}", "unknown", dumps({}))])


async def test_invalid_actions_still_bounded(make_agent, task):
    result = await make_agent(InvalidModel()).run_task(task)
    assert result["decision_rounds"] == 12 and result["tool_calls"] == 0
    assert result["reason"] == "EXECUTION_LIMIT_REACHED"
    assert all(step["status"] == "blocked" for step in result["steps"])


async def test_exact_budget_and_deadline(make_agent, task):
    result = await make_agent().run_task(
        {**task, "budget": "5.00", "deadline": "2030-01-01T10:09:00+08:00"}
    )
    assert result["status"] == "succeeded"
    assert Decimal(result["costs"]["total_cost"]) == Decimal("5")
    assert result["gaps"]["minimum_time_gap_seconds"] == 0


async def test_failed_save_is_not_success(make_agent, task):
    scenario = Scenario(
        fixed_start="2030-01-01T10:00:00+08:00", failures={"save_plan": list(range(1, 13))}
    )
    result = await make_agent(scenario=scenario).run_task(task)
    assert result["status"] == "incomplete" and result["saved"] is None
    assert any(
        step["tool_name"] == "save_plan" and step["status"] == "failed" for step in result["steps"]
    )


async def test_cancel_during_tool_keeps_charge_and_unknown_outcome(make_agent, task):
    entered = asyncio.Event()

    class WaitingScript(OfflineMockClient):
        async def generate_script(self, request, record):
            entered.set()
            await asyncio.Event().wait()

    agent = make_agent(WaitingScript())
    running = asyncio.create_task(agent.run_task(task))
    await asyncio.wait_for(entered.wait(), timeout=2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    state = await agent.storage.load(task["task_id"])
    assert state["reason"] == "CANCELLED"
    assert state["tool_cost"] == "3.00"
    assert state["steps"][-1]["status"] == "failed"
    assert state["ledger"][-1]["outcome"] == "unknown"
    assert state["saved"] is None
