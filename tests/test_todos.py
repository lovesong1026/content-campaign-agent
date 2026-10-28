import asyncio
import copy
import json

import pytest

from content_campaign_agent.model import OfflineMockClient
from content_campaign_agent.schemas import Decision, Scenario, ToolCall, dumps
from content_campaign_agent.storage import Storage


@pytest.mark.parametrize("proposal", ["omitted", "null"])
async def test_context_has_real_progress_and_omitted_plan_is_preserved(make_agent, task, proposal):
    class NoProposal(OfflineMockClient):
        def call(self, name, **args):
            if proposal == "null":
                args["remaining_steps"] = None
            return Decision([ToolCall("call-" + name, name, dumps(args))])

        async def decide(self, context, messages, record):
            if context["searched"] and not context["current_plan"]:
                assert context["todo_list"][0]["status"] == "succeeded"
                assert [
                    row["tool_name"] for row in context["todo_list"] if row["status"] == "pending"
                ] == ["generate_script", "estimate_cost", "save_plan"]
                assert context["plan_revision"] == 1
            return await super().decide(context, messages, record)

    result = await make_agent(NoProposal()).run_task(task)
    assert result["status"] == "succeeded" and result["costs"]["total_cost"] == "5.00"
    assert all(row["status"] == "succeeded" for row in result["todo_list"])
    assert result["pending_plan"] == []
    assert [row["reason"] for row in result["plan_revisions"]] == ["INITIAL_PLAN", "TASK_FINISHED"]


async def test_failure_keeps_attempt_and_replans_from_observation(make_agent, task):
    agent = make_agent(
        scenario=Scenario(
            fixed_start="2030-01-01T10:00:00+08:00", failures={"generate_script": [1]}
        )
    )
    result = await agent.run_task(task)
    scripts = [row for row in result["todo_list"] if row["tool_name"] == "generate_script"]
    assert [row["status"] for row in scripts] == ["failed", "succeeded"]
    assert scripts[0]["todo_id"] != scripts[1]["todo_id"]
    assert scripts[0]["error_code"] == "INJECTED_TOOL_FAILURE"
    change = next(row for row in result["plan_revisions"] if row["reason"] == "MODEL_REPLAN")
    assert change["source_observation"]["source_call_id"] == scripts[0]["call_id"]
    assert all(row["status"] == "pending" for row in result["plan_revisions"][0]["items"])
    assert result["costs"]["tool_cost"] == "6.00" and result["tool_calls"] == 5
    assert {step["todo_id"] for step in result["steps"]} == {
        row["todo_id"] for row in result["todo_list"]
    }


async def test_overbudget_retains_successful_estimate_and_adds_rewrite(make_agent, task):
    agent = make_agent(OfflineMockClient("premium_first"))
    result = await agent.run_task({**task, "budget": "10"})
    rows = result["todo_list"]
    estimates = [row for row in rows if row["tool_name"] == "estimate_cost"]
    assert [row["within_budget"] for row in estimates] == [False, True]
    assert all(row["status"] == "succeeded" for row in estimates)
    change = next(row for row in result["plan_revisions"] if row["reason"] == "MODEL_REPLAN")
    assert change["source_observation"]["reason"] == "PLAN_OVER_BUDGET"
    assert [
        row["tool_name"] for row in change["items"] if row["status"] in ("running", "pending")
    ] == ["generate_script", "estimate_cost", "save_plan"]
    assert len({row["todo_id"] for row in rows}) == len(rows) == 6
    assert result["costs"]["total_cost"] == "8.00"


async def test_explicit_empty_plan_cancels_future_but_never_marks_it_completed(make_agent, task):
    class ClearFuture(OfflineMockClient):
        async def decide(self, context, messages, record):
            decision = await super().decide(context, messages, record)
            if not context["searched"]:
                args = json.loads(decision.calls[0].arguments)
                args["remaining_steps"] = []
                decision.calls[0].arguments = dumps(args)
            return decision

    result = await make_agent(ClearFuture()).run_task(task)
    cancelled = [row for row in result["todo_list"] if row.get("reason") == "PLAN_REVISED"]
    assert len(cancelled) == 3 and all(row["status"] == "skipped" for row in cancelled)
    assert all(row["call_id"] is None for row in cancelled)
    assert result["status"] == "succeeded" and result["tool_calls"] == 4
    assert sum(row["status"] == "succeeded" for row in result["todo_list"]) == 4


async def test_model_cannot_forge_todo_completion(make_agent, task):
    class ForgedStatus(OfflineMockClient):
        async def decide(self, context, messages, record):
            if context["remaining_decisions"] == 11:
                return Decision(
                    [
                        ToolCall(
                            "forged",
                            "search_materials",
                            dumps(
                                {"query": "春季", "decision_note": "forged", "status": "succeeded"}
                            ),
                        )
                    ]
                )
            return Decision(content="完成了")

    result = await make_agent(ForgedStatus()).run_task(task)
    assert result["status"] == "incomplete" and result["saved"] is None
    assert result["tool_calls"] == 0 and result["costs"]["tool_cost"] == "0.00"
    assert not any(row["status"] == "succeeded" for row in result["todo_list"])
    assert result["todo_list"][0]["error_code"] == "INVALID_TOOL_ARGUMENTS"


async def test_skipped_batch_call_leaves_future_intention_pending(make_agent, task):
    class Batched(OfflineMockClient):
        async def decide(self, context, messages, record):
            if context["remaining_decisions"] == 11:
                first = self.call("search_materials", query="春季", decision_note="检索")
                first.calls.append(ToolCall("later", "generate_script", "{}"))
                return first
            if context["tool_cost"] == "1.00" and not context["searched"]:
                assert (
                    next(
                        row for row in context["todo_list"] if row["tool_name"] == "generate_script"
                    )["status"]
                    == "pending"
                )
            return await super().decide(context, messages, record)

    result = await make_agent(
        Batched(),
        scenario=Scenario(
            fixed_start="2030-01-01T10:00:00+08:00", failures={"search_materials": [1]}
        ),
    ).run_task(task)
    skipped = next(step for step in result["steps"] if step["call_id"] == "later")
    assert skipped["status"] == "skipped" and skipped["todo_id"] is None
    assert result["status"] == "succeeded" and result["tool_calls"] == 5


async def test_cancellation_and_recovery_close_unfinished_todos(make_agent, task, tmp_path):
    entered = asyncio.Event()

    class Waiting(OfflineMockClient):
        async def generate_script(self, request, record):
            entered.set()
            await asyncio.Event().wait()

    agent = make_agent(Waiting())
    running = asyncio.create_task(agent.run_task(task))
    await asyncio.wait_for(entered.wait(), timeout=2)
    snapshot = await agent.storage.load(task["task_id"])
    recovery = Storage(tmp_path / "recovery")
    await recovery.save(copy.deepcopy(snapshot))
    await recovery.recover_interrupted()
    restored = await recovery.load(task["task_id"])
    assert restored["reason"] == "EXECUTION_INTERRUPTED"
    interrupted = next(
        row for row in restored["todo_list"] if row["tool_name"] == "generate_script"
    )
    assert interrupted["status"] == "failed" and interrupted["outcome_unknown"]
    assert restored["pending_plan"] == []
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    cancelled = await agent.storage.load(task["task_id"])
    assert cancelled["reason"] == "CANCELLED"
    assert not any(row["status"] in ("pending", "running") for row in cancelled["todo_list"])
    assert cancelled["tool_cost"] == "3.00"


async def test_repeat_and_legacy_snapshots_are_not_rewritten(make_agent, task):
    agent = make_agent()
    first = await agent.run_task(task)
    repeated = await make_agent().run_task(task)
    assert repeated["reused"] and repeated["plan_revisions"] == first["plan_revisions"]
    state = await agent.storage.load(task["task_id"])
    state.pop("todo_list")
    state.pop("plan_revisions")
    state.pop("todo_replan")
    await agent.storage.save(state)
    before = (agent.storage.directory(task["task_id"]) / "task.json").read_bytes()
    legacy = await make_agent().run_task(task)
    assert legacy["reused"] and legacy["todo_tracking"] == "not_recorded"
    assert legacy["todo_list"] == legacy["plan_revisions"] == []
    assert before == (agent.storage.directory(task["task_id"]) / "task.json").read_bytes()
