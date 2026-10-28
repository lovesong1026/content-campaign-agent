import asyncio
import copy
import json
from decimal import Decimal

import pytest

from content_campaign_agent.model import OfflineMockClient
from content_campaign_agent.schemas import Decision, ModelError, Scenario


class StopAfterFailure(OfflineMockClient):
    async def decide(self, context, messages, record):
        if any(
            message["role"] == "tool" and json.loads(message["content"])["status"] == "failed"
            for message in messages
        ):
            return Decision(content="本轮工具失败，停止执行")
        return await super().decide(context, messages, record)


async def failed_task(make_agent, task, name="generate_script"):
    agent = make_agent(
        StopAfterFailure(),
        scenario=Scenario(fixed_start="2030-01-01T10:00:00+08:00", failures={name: [1]}),
    )
    result = await agent.run_task(task)
    assert result["status"] == "incomplete"
    return agent, copy.deepcopy(result)


@pytest.mark.parametrize(
    "name,remaining",
    [
        ("search_materials", ["search_materials", "generate_script", "estimate_cost", "save_plan"]),
        ("generate_script", ["generate_script", "estimate_cost", "save_plan"]),
        ("estimate_cost", ["estimate_cost", "save_plan"]),
        ("save_plan", ["save_plan"]),
    ],
)
async def test_resume_after_restart_only_executes_remaining_tools(
    make_agent, task, name, remaining
):
    original, first = await failed_task(make_agent, task, name)
    unchanged = await make_agent().run_task(task)
    assert unchanged["reused"] and unchanged["status"] == "incomplete"
    assert unchanged["ledger"] == first["ledger"]
    restarted = make_agent()
    with restarted.storage.writer():
        await restarted.start()
        result = await restarted.resume_task(task["task_id"])
    assert result["status"] == "succeeded" and result["resumed"]
    assert result["run_id"] == first["run_id"]
    assert result["resume_count"] == 1
    assert result["resume_history"][0]["previous_reason"] == first["reason"]
    assert result["decision_rounds"] == first["decision_rounds"] + len(remaining)
    assert result["steps"][: len(first["steps"])] == first["steps"]
    assert result["ledger"][: len(first["ledger"])] == first["ledger"]
    assert result["todo_list"][: len(first["todo_list"])] == first["todo_list"]
    assert [row["tool_name"] for row in result["ledger"][len(first["ledger"]) :]] == remaining
    assert sum(row["status"] == "failed" for row in result["steps"]) == 1
    assert len({row["call_id"] for row in result["steps"]}) == len(result["steps"])
    assert len({row["todo_id"] for row in result["todo_list"]}) == len(result["todo_list"])
    assert result["started_at"] == first["started_at"]
    assert result["elapsed_seconds"] == sum(row["duration_seconds"] for row in result["ledger"])
    assert Decimal(result["costs"]["tool_cost"]) == sum(
        Decimal(row["fee"]) for row in result["ledger"]
    )
    directory = original.storage.directory(task["task_id"])
    assert len(list((directory / "saved").glob("*.json"))) == 1
    before = (directory / "task.json").read_bytes()
    repeated = await make_agent().resume_task(task["task_id"])
    assert repeated["reused"] and not repeated["resumed"]
    assert repeated["resume_count"] == 1
    assert (directory / "task.json").read_bytes() == before


async def test_resumed_model_receives_actual_success_and_error_observations(make_agent, task):
    _, first = await failed_task(make_agent, task)
    observed = []

    class InspectResume(OfflineMockClient):
        async def decide(self, context, messages, record):
            if not observed:
                assert context["previous_steps"] == first["steps"]
                assert context["previous_steps"][0]["observation"]["data"]["materials"]
                assert context["previous_steps"][-1]["observation"]["error"]["retryable"]
                assert context["resume"]["previous_reason"] == first["reason"]
                assert context["tool_cost"] == "3.00"
                assert context["simulated_now"] == first["simulated_now"]
                assert context["remaining_decisions"] == 12 - first["decision_rounds"] - 1
                observed.append(context)
            return await super().decide(context, messages, record)

    result = await make_agent(InspectResume()).resume_task(task["task_id"])
    assert observed and result["status"] == "succeeded"
    assert result["costs"]["total_cost"] == "7.00"


async def test_resume_after_planner_error_reuses_search(make_agent, task):
    class PlannerFailure(OfflineMockClient):
        async def decide(self, context, messages, record):
            if context["searched"]:
                raise ModelError("MODEL_TIMEOUT", "测试规划超时")
            return await super().decide(context, messages, record)

    first = await make_agent(PlannerFailure()).run_task(task)
    assert first["reason"] == "MODEL_TIMEOUT" and first["tool_calls"] == 1
    result = await make_agent().resume_task(task["task_id"])
    assert result["status"] == "succeeded" and result["tool_calls"] == 4
    assert result["costs"]["total_cost"] == "5.00"
    assert result["decision_rounds"] == 5


async def test_resume_interrupted_generation_keeps_unknown_attempt_and_charge(make_agent, task):
    entered = asyncio.Event()

    class WaitingScript(OfflineMockClient):
        async def generate_script(self, request, record):
            entered.set()
            await asyncio.Event().wait()

    agent = make_agent(WaitingScript())
    running = asyncio.create_task(agent.run_task(task))
    await asyncio.wait_for(entered.wait(), timeout=2)
    snapshot = await agent.storage.load(task["task_id"])
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    await agent.storage.save(snapshot)
    restarted = make_agent()
    with restarted.storage.writer():
        await restarted.start()
        result = await restarted.resume_task(task["task_id"])
    assert result["status"] == "succeeded"
    assert result["resume_history"][0]["previous_reason"] == "EXECUTION_INTERRUPTED"
    assert result["steps"][1]["status"] == "failed"
    assert result["steps"][1]["outcome_unknown"]
    assert result["ledger"][1]["outcome"] == "unknown"
    assert result["costs"]["tool_cost"] == "6.00"
    assert result["elapsed_seconds"] == 840
    assert [row["tool_name"] for row in result["ledger"]].count("search_materials") == 1


async def test_resume_uses_original_materials_pricing_durations_and_scenario(make_agent, task):
    _, first = await failed_task(make_agent, task)
    restarted = make_agent(scenario=Scenario(empty_materials=True))
    restarted.tools.data["pricing"]["production"]["economy"] = "90.00"
    restarted.tools.data["pricing"]["version"] = "different-version"
    restarted.tools.data["tools"]["generate_script"]["fee"] = "80.00"
    restarted.tools.data["tools"]["generate_script"]["duration_seconds"] = 9999
    restarted.tools.data["materials"] = []
    result = await restarted.resume_task(task["task_id"])
    assert result["status"] == "succeeded" and result["costs"]["total_cost"] == "7.00"
    assert result["elapsed_seconds"] == 840 and result["started_at"] == first["started_at"]
    state = await restarted.storage.load(task["task_id"])
    assert state["tool_ordinals"]["generate_script"] == 2
    assert state["data_snapshot"]["pricing"]["version"] == "pricing-v1"


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"budget": "5"}, "BUDGET_INFEASIBLE"),
        ({"deadline": "2030-01-01T10:10:00+08:00"}, "DEADLINE_INFEASIBLE"),
    ],
)
async def test_resume_does_not_reset_budget_or_clock(make_agent, task, change, reason):
    _, first = await failed_task(make_agent, {**task, **change})
    result = await make_agent().resume_task(task["task_id"])
    assert result["reason"] == reason and result["saved"] is None
    assert result["ledger"] == first["ledger"]
    assert result["simulated_now"] == first["simulated_now"]
    assert result["costs"]["tool_cost"] == "3.00"
    assert result["decision_rounds"] == first["decision_rounds"] + 1


async def test_resume_does_not_reset_execution_limits(make_agent, task):
    task = {**task, "budget": "100"}
    first = await make_agent(OfflineMockClient("loop")).run_task(task)
    result = await make_agent().resume_task(task["task_id"])
    assert result["reason"] == "EXECUTION_LIMIT_REACHED" and result["saved"] is None
    assert result["decision_rounds"] == result["tool_calls"] == 12
    assert result["ledger"] == first["ledger"]


async def test_missing_invalid_input_and_mode_mismatch_do_not_resume(make_agent, task):
    missing = await make_agent().resume_task("missing")
    assert missing["reason"] == "TASK_NOT_FOUND" and not missing["resumed"]
    agent = make_agent()
    await agent.run_task({**task, "budget": None})
    before = (agent.storage.directory(task["task_id"]) / "task.json").read_bytes()
    invalid = await make_agent().resume_task(task["task_id"])
    assert invalid["resume_error"] == "INPUT_REQUIRED" and not invalid["resumed"]
    assert (agent.storage.directory(task["task_id"]) / "task.json").read_bytes() == before
    other = {**task, "task_id": "mode-test"}
    await failed_task(make_agent, other)
    model = OfflineMockClient()
    model.mode = "real"
    mismatch = await make_agent(model).resume_task(other["task_id"])
    assert mismatch["resume_error"] == "RESUME_MODE_MISMATCH"
    assert mismatch["resume_count"] == 0 and not mismatch["resumed"]


async def test_concurrent_resume_only_executes_once(make_agent, task):
    await failed_task(make_agent, task)
    agent = make_agent()
    results = await asyncio.gather(
        agent.resume_task(task["task_id"]), agent.resume_task(task["task_id"])
    )
    assert sum(row["resumed"] for row in results) == 1
    state = await agent.storage.load(task["task_id"])
    assert state["status"] == "succeeded" and state["resume_count"] == 1
    assert state["tool_calls"] == 5 and state["tool_cost"] == "6.00"
    assert sum(row["kind"] == "task_resumed" for row in state["events"]) == 1


async def test_legacy_snapshot_can_resume_without_inventing_historical_todos(make_agent, task):
    original, first = await failed_task(make_agent, task)
    state = await original.storage.load(task["task_id"])
    for key in ("todo_list", "plan_revisions", "todo_replan"):
        state.pop(key)
    await original.storage.save(state)
    result = await make_agent().resume_task(task["task_id"])
    assert result["status"] == "succeeded"
    assert result["steps"][: len(first["steps"])] == first["steps"]
    assert result["plan_revisions"][0]["reason"] == "TASK_RESUMED"
    assert all(row["created_round"] >= first["decision_rounds"] for row in result["todo_list"])


async def orphaned_save(make_agent, task):
    agent = make_agent()
    save = agent.storage.save

    async def fail_after_artifact(state):
        if state["saved"] is not None:
            raise OSError("测试保存文件已落盘，但终态快照写入失败")
        await save(state)

    agent.storage.save = fail_after_artifact
    result = await agent.run_task(task)
    assert result["reason"] == "STORAGE_ERROR"
    directory = agent.storage.directory(task["task_id"])
    path = next((directory / "saved").glob("*.json"))
    return path


async def test_resume_reconciles_durable_save_without_replaying_or_charging(make_agent, task):
    path = await orphaned_save(make_agent, task)
    before = path.read_bytes()
    modified = path.stat().st_mtime_ns

    class NeverDecide(OfflineMockClient):
        async def decide(self, context, messages, record):
            raise AssertionError("已经落盘的保存不应再次请求模型")

    agent = make_agent(NeverDecide())
    await agent.start()
    result = await agent.resume_task(task["task_id"])
    assert result["status"] == "succeeded" and result["resumed"]
    assert result["tool_calls"] == result["decision_rounds"] == 4
    assert result["costs"]["total_cost"] == "5.00" and result["elapsed_seconds"] == 540
    assert result["steps"][-1]["status"] == "succeeded"
    assert not result["steps"][-1].get("outcome_unknown")
    assert result["ledger"][-1]["outcome"] == "succeeded"
    assert path.read_bytes() == before and path.stat().st_mtime_ns == modified
    state = await agent.storage.load(task["task_id"])
    assert any(row["kind"] == "save_reconciled" for row in state["events"])


@pytest.mark.parametrize("contents", ["{broken", "null", '{"total_cost":"0.00"}'])
async def test_resume_never_overwrites_conflicting_save(make_agent, task, contents):
    path = await orphaned_save(make_agent, task)
    path.write_text(contents)
    agent = make_agent()
    await agent.start()
    result = await agent.resume_task(task["task_id"])
    assert result["status"] == "incomplete" and result["reason"] == "SAVED_RESULT_CONFLICT"
    assert result["tool_calls"] == result["decision_rounds"] == 4
    assert result["saved"] is None
    assert path.read_text() == contents


async def test_resume_unknown_save_without_artifact_retries_as_new_attempt(make_agent, task):
    agent = make_agent()

    async def fail_before_artifact(args, state):
        raise OSError("测试保存结果尚未落盘")

    agent.tools.save = fail_before_artifact
    result = await agent.run_task(task)
    assert result["reason"] == "STORAGE_ERROR"
    restarted = make_agent()
    await restarted.start()
    result = await restarted.resume_task(task["task_id"])
    assert result["status"] == "succeeded" and result["tool_calls"] == 5
    assert result["steps"][-2]["outcome_unknown"]
    assert result["ledger"][-2]["outcome"] == "unknown"
    assert result["steps"][-1]["status"] == "succeeded"
    assert result["costs"]["total_cost"] == "5.00" and result["elapsed_seconds"] == 600


@pytest.mark.parametrize("name", ["search_materials", "generate_script", "estimate_cost"])
async def test_result_export_failure_does_not_replay_committed_success(make_agent, task, name):
    agent = make_agent()
    artifact = agent.storage.artifact

    async def fail_after_result_export(state, relative, data):
        await artifact(state, relative, data)
        if relative.startswith("results/") and data["tool_name"] == name:
            raise OSError("测试结果导出后进程中断")

    agent.storage.artifact = fail_after_result_export
    first = await agent.run_task(task)
    assert first["reason"] == "STORAGE_ERROR"
    checkpoint = await agent.storage.load(task["task_id"])
    assert checkpoint["steps"][-1]["status"] == "succeeded"
    assert checkpoint["ledger"][-1]["outcome"] == "succeeded"
    restarted = make_agent()
    await restarted.start()
    result = await restarted.resume_task(task["task_id"])
    assert result["status"] == "succeeded" and result["tool_calls"] == 4
    assert result["costs"]["total_cost"] == "5.00" and result["elapsed_seconds"] == 540
    assert all(row["status"] == "succeeded" for row in result["steps"])
    assert [row["tool_name"] for row in result["ledger"]].count(name) == 1


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("partial_write", [False, True])
async def test_resume_registration_failure_does_not_leave_phantom_running_task(
    make_agent, task, cached, partial_write
):
    original, first = await failed_task(make_agent, task)
    agent = original if cached else make_agent()
    save = agent.storage.save

    async def fail_registration(state):
        if partial_write:
            await save(state)
        raise OSError("测试续跑启动记录持久化失败")

    agent.storage.save = fail_registration
    with pytest.raises(OSError):
        await agent.resume_task(task["task_id"])
    unchanged = await agent.run_task(task)
    assert unchanged["status"] == "incomplete" and unchanged["resume_count"] == 0
    assert unchanged["ledger"] == first["ledger"]
    assert unchanged["todo_list"] == first["todo_list"]
    agent.storage.save = save
    result = await agent.resume_task(task["task_id"])
    assert result["status"] == "succeeded" and result["resume_count"] == 1
    assert result["tool_calls"] == 5 and result["costs"]["total_cost"] == "7.00"


async def test_multiple_resumes_keep_all_attempts_and_cumulative_limits(make_agent, task):
    scenario = Scenario(
        fixed_start="2030-01-01T10:00:00+08:00",
        failures={"generate_script": [1], "estimate_cost": [1]},
    )
    first = await make_agent(StopAfterFailure(), scenario=scenario).run_task(task)
    second = await make_agent(StopAfterFailure()).resume_task(task["task_id"])
    assert first["status"] == second["status"] == "incomplete"
    result = await make_agent().resume_task(task["task_id"])
    assert result["status"] == "succeeded" and result["resume_count"] == 2
    assert result["decision_rounds"] == 8 and result["tool_calls"] == 6
    assert result["costs"]["total_cost"] == "8.00" and result["elapsed_seconds"] == 900
    assert result["ledger"][: len(second["ledger"])] == second["ledger"]
    assert sum(row["status"] == "failed" for row in result["steps"]) == 2
    assert [row["tool_name"] for row in result["ledger"]].count("search_materials") == 1
    assert [row["decision_rounds"] for row in result["resume_history"]] == [3, 6]
