import asyncio

from content_campaign_agent.model import OfflineMockClient
from content_campaign_agent.schemas import Decision, Scenario, ToolCall, dumps


class YieldingModel(OfflineMockClient):
    async def decide(self, context, messages, record):
        await asyncio.sleep(0)
        return await super().decide(context, messages, record)


async def test_batch_isolation_limit_duplicate_and_conflict(make_agent, task):
    agent = make_agent(YieldingModel(), max_concurrent_tasks=2)
    rows = [{**task, "task_id": f"task-{n}"} for n in range(5)]
    rows += [
        rows[0].copy(),
        {**rows[0], "budget": "9"},
        {**task, "task_id": "missing", "budget": None},
    ]
    batch = await agent.run_batch(rows)
    results = batch["results"]
    assert batch["peak_active_tasks"] == 2
    assert all(r["status"] == "succeeded" for r in results[:6])
    assert results[5]["reused"] and results[0]["run_id"] == results[5]["run_id"]
    assert results[6]["reason"] == "TASK_INPUT_CONFLICT"
    assert results[7]["status"] == "needs_input"
    assert len({r["trace_path"] for r in results[:5]}) == 5


class MultiModel(OfflineMockClient):
    async def decide(self, context, messages, record):
        if not context["searched"]:
            return Decision(
                [
                    ToolCall(
                        "first",
                        "search_materials",
                        dumps({"query": "春季", "decision_note": "检索"}),
                    ),
                    ToolCall(
                        "second",
                        "search_materials",
                        dumps({"query": "新品", "decision_note": "补充"}),
                    ),
                ]
            )
        observations = [m for m in messages if m["role"] == "tool"]
        assert {m["tool_call_id"] for m in observations[:2]} == {"first", "second"}
        return await super().decide(context, messages, record)


async def test_multi_call_serial_and_observation_barrier(make_agent, task):
    good = await make_agent(MultiModel()).run_task(task)
    assert good["status"] == "succeeded" and good["tool_calls"] == 5
    scenario = Scenario(fixed_start="2030-01-01T10:00:00+08:00", failures={"search_materials": [1]})
    failed = await make_agent(MultiModel(), scenario=scenario).run_task(
        {**task, "task_id": "failure"}
    )
    assert failed["steps"][0]["status"] == "failed"
    assert failed["steps"][1]["status"] == "skipped"
    assert failed["steps"][1]["observation"]["error"]["code"] == "REPLAN_REQUIRED"


def test_process_writer_lock(make_agent):
    first = make_agent()
    second = make_agent()
    with first.storage.writer():
        try:
            with second.storage.writer():
                raise AssertionError("second writer acquired the same directory")
        except RuntimeError as error:
            assert str(error) == "OUTPUT_DIRECTORY_BUSY"


class ManyCalls(OfflineMockClient):
    async def decide(self, context, messages, record):
        return Decision(
            [
                ToolCall(
                    f"call-{n}",
                    "search_materials",
                    dumps({"query": "春季", "decision_note": "额度边界"}),
                )
                for n in range(12)
            ]
        )


async def test_one_response_cannot_bypass_global_tool_limit(make_agent, task):
    result = await make_agent(ManyCalls()).run_task({**task, "budget": "100"})
    assert result["decision_rounds"] == 1
    assert result["tool_calls"] == 12
    assert result["reason"] == "EXECUTION_LIMIT_REACHED"


class SaveThenExtra(OfflineMockClient):
    async def decide(self, context, messages, record):
        if context["current_estimate"] and context["current_estimate"]["within_budget"]:
            normal = await super().decide(context, messages, record)
            normal.calls.append(
                ToolCall(
                    "after-save",
                    "search_materials",
                    dumps({"query": "春季", "decision_note": "应跳过"}),
                )
            )
            return normal
        return await super().decide(context, messages, record)


async def test_no_charging_after_saved_in_same_response(make_agent, task):
    result = await make_agent(SaveThenExtra()).run_task(task)
    assert result["status"] == "succeeded" and result["tool_calls"] == 4
    assert result["steps"][-1]["status"] == "skipped"
    assert result["steps"][-1]["observation"]["error"]["code"] == "TASK_ALREADY_SAVED"


class OversizedCalls(OfflineMockClient):
    async def decide(self, context, messages, record):
        return Decision(
            [
                ToolCall(
                    f"large-{context['remaining_decisions']}-{n}",
                    "search_materials",
                    dumps({"query": "春季", "decision_note": "批次边界"}),
                )
                for n in range(13)
            ]
        )


async def test_oversized_batches_do_not_execute_or_charge(make_agent, task):
    result = await make_agent(OversizedCalls()).run_task(task)
    assert result["tool_calls"] == 0 and result["decision_rounds"] == 12
    assert result["costs"]["tool_cost"] == "0.00"
    assert all(
        step["observation"]["error"]["code"] == "BATCH_TOO_LARGE" for step in result["steps"]
    )
